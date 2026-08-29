"""The gateway: nothing runs until its final arguments have passed both checks."""

from __future__ import annotations

import asyncio
import gc
import time
from collections.abc import Callable
from itertools import count
from typing import NoReturn

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.domain.errors import ErrorInfo, UnknownToolError
from agent_workbench.domain.events import (
    EventEnvelope,
    PermissionRequested,
    ToolApprovalDecided,
    ToolCompleted,
    ToolProposed,
)
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PolicyDecision,
    PrincipalContext,
)
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import (
    ToolCall,
    ToolResult,
    ToolSpec,
    argument_digest,
)
from agent_workbench.ports.cancellation import (
    CancellationSource,
    NullCancellationToken,
)
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.hooks import HookOutcome
from agent_workbench.ports.tools import ToolBinding, ToolInvocation
from agent_workbench.runtime.hook_bus import HookBus
from agent_workbench.runtime.schema_validation import UnsupportedToolSchema
from agent_workbench.runtime.tool_executor import ToolExecutor
from agent_workbench.runtime.tool_gateway import PreparedCall, ToolGateway

SCOPE = EventScope(stream_id="stream_1", run_id="run_1")
CONTEXT = ExecutionContext(
    principal=PrincipalContext(principal_id="user_1", tenant_id="tenant_a"),
    envelope=AuthorizationEnvelope(allowed_tools=("search",)),
    agent_run_id="run_1",
    policy_identity="policy-test:0000000000000000",
)

SEARCH_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    "required": ["query"],
    "additionalProperties": False,
}


def _ticking(step: float = 0.004) -> Callable[[], float]:
    counter = count()

    def reading() -> float:
        return next(counter) * step

    return reading


class _Search:
    """A recording tool with a schema worth validating."""

    def __init__(
        self,
        *,
        schema: JsonObject | None = None,
        workspace_writes: tuple[str, ...] = (),
        truncated: bool = False,
    ) -> None:
        self.calls: list[ToolCall] = []
        self._workspace_writes = workspace_writes
        self._truncated = truncated
        spec = ToolSpec(
            name="search",
            description="Search the local corpus.",
            input_schema=schema if schema is not None else SEARCH_SCHEMA,
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=5,
        )
        self.binding = ToolBinding(spec=spec, handler=self._handler)

    async def _handler(self, invocation: ToolInvocation) -> ToolResult:
        self.calls.append(invocation.call)
        return ToolResult.succeeded(
            invocation.call,
            content="1 hit",
            workspace_writes=self._workspace_writes,
            truncated=self._truncated,
        )


class _ScriptedPolicy:
    """A policy engine that answers from a fixed list of decisions."""

    def __init__(self, *decisions: PolicyDecision) -> None:
        self._decisions = list(decisions)
        self.seen: list[ToolCall] = []

    async def decide(
        self,
        call: ToolCall,
        context: ExecutionContext,
    ) -> PolicyDecision:
        self.seen.append(call)
        if self._decisions:
            return self._decisions.pop(0)
        return PolicyDecision.allow("exhausted_script")


def _call(**arguments: object) -> ToolCall:
    return ToolCall(
        tool_call_id="toolu_1",
        tool_name="search",
        arguments=dict(arguments),  # pyright: ignore[reportArgumentType]
    )


class _Harness:
    def __init__(
        self,
        tool: _Search | None = None,
        policy: _ScriptedPolicy | None = None,
        **gateway_kwargs: object,
    ) -> None:
        self.tool = tool if tool is not None else _Search()
        registry = StaticToolRegistry([self.tool.binding])
        self.policy = (
            policy if policy is not None else EnvelopePolicyEngine(registry=registry)
        )
        self.log = InMemoryEventLog()
        self.sink = ScopedEventSink(log=self.log, scope=SCOPE)
        self.gateway = ToolGateway(
            registry=registry,
            policy=self.policy,
            executor=ToolExecutor(monotonic=_ticking()),
            **gateway_kwargs,  # pyright: ignore[reportArgumentType]
        )

    async def run(self, call: ToolCall) -> ToolResult:
        """Take one call through every gateway phase, as the loop would."""

        await self.gateway.propose(call, sink=self.sink)
        prepared = await self.gateway.prepare(
            call,
            context=CONTEXT,
            sink=self.sink,
        )
        if isinstance(prepared, ToolResult):
            return prepared
        authorized = await self.gateway.authorize(
            prepared,
            context=CONTEXT,
            sink=self.sink,
        )
        if isinstance(authorized, ToolResult):
            return authorized
        return await self.gateway.invoke(
            authorized,
            context=CONTEXT,
            cancellation=NullCancellationToken(),
            sink=self.sink,
        )

    async def events(self) -> list[EventEnvelope]:
        return list(await self.log.read(SCOPE.stream_id))


def _execute(harness: _Harness, call: ToolCall) -> tuple[ToolResult, list[str]]:
    async def scenario() -> tuple[ToolResult, list[str]]:
        # Bounded so that a gateway which stopped bounding its own waits fails
        # here instead of parking the suite. A test that can only hang cannot
        # be told apart from a machine that is stuck.
        result = await asyncio.wait_for(harness.run(call), timeout=30.0)
        return result, [event.event_type for event in await harness.events()]

    return asyncio.run(scenario())


def test_an_unenforceable_schema_stops_assembly() -> None:
    """A tool the validator cannot check must not become a registered tool."""

    tool = _Search(schema={"type": "object", "oneOf": []})
    registry = StaticToolRegistry([tool.binding])

    with pytest.raises(UnsupportedToolSchema, match="tool search"):
        ToolGateway(registry=registry, policy=EnvelopePolicyEngine(registry=registry))


def test_a_valid_call_runs_and_is_recorded() -> None:
    harness = _Harness()

    result, events = _execute(harness, _call(query="fusion", top_k=3))

    assert result.status == "ok"
    assert len(harness.tool.calls) == 1
    assert events == [
        "ToolProposed",
        "PermissionResolved",
        "ToolStarted",
        "ToolCompleted",
    ]


def test_an_unknown_tool_is_answered_without_a_permission_decision() -> None:
    harness = _Harness()
    call = ToolCall(tool_call_id="toolu_1", tool_name="not_registered")

    result, events = _execute(harness, call)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "unknown_tool"
    assert events == ["ToolProposed", "ToolFailed"]


def test_invalid_arguments_never_reach_the_handler() -> None:
    harness = _Harness()

    result, events = _execute(harness, _call(top_k=3))

    assert harness.tool.calls == []
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"
    assert "arguments.query" in result.error.message
    assert events == ["ToolProposed", "ToolFailed"]


def test_arguments_over_the_size_ceiling_are_refused() -> None:
    harness = _Harness(max_argument_bytes=64)

    result, _ = _execute(harness, _call(query="x" * 200))

    assert harness.tool.calls == []
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"
    assert "byte ceiling" in result.error.message


def test_a_denied_call_never_reaches_the_handler() -> None:
    harness = _Harness(policy=_ScriptedPolicy(PolicyDecision.deny("not_today")))

    result, events = _execute(harness, _call(query="fusion"))

    assert harness.tool.calls == []
    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert "not_today" in result.error.message
    assert events == ["ToolProposed", "PermissionResolved", "ToolFailed"]


def test_rewritten_arguments_are_revalidated_and_reauthorized() -> None:
    """A rewrite that skipped either check would be a way past both.

    The schema is the tool's own contract and the policy is the deployment's:
    here the call is valid at 9, and the policy narrows it to 5 within the
    schema's ceiling of 10.
    """

    policy = _ScriptedPolicy(
        PolicyDecision.allow_modified("clamped_top_k", {"query": "fusion", "top_k": 5}),
        PolicyDecision.allow("within_limits"),
    )
    harness = _Harness(policy=policy)

    result, events = _execute(harness, _call(query="fusion", top_k=9))

    assert result.status == "ok"
    # The handler ran once, on the clamped arguments, after a second decision.
    assert len(harness.tool.calls) == 1
    assert harness.tool.calls[0].arguments == {"query": "fusion", "top_k": 5}
    assert len(policy.seen) == 2
    assert events.count("PermissionResolved") == 2


def test_a_rewrite_into_invalid_arguments_is_refused() -> None:
    policy = _ScriptedPolicy(
        PolicyDecision.allow_modified("broken_rewrite", {"query": 42}),
    )
    harness = _Harness(policy=policy)

    result, _ = _execute(harness, _call(query="fusion"))

    assert harness.tool.calls == []
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"


def test_a_rewrite_keeps_the_original_tool_call_id() -> None:
    policy = _ScriptedPolicy(
        PolicyDecision.allow_modified("clamped", {"query": "fusion", "top_k": 1}),
        PolicyDecision.allow("ok"),
    )
    harness = _Harness(policy=policy)

    result, _ = _execute(harness, _call(query="fusion", top_k=9))

    assert result.tool_call_id == "toolu_1"
    assert harness.tool.calls[0].tool_call_id == "toolu_1"


def test_an_engine_that_keeps_rewriting_is_refused() -> None:
    policy = _ScriptedPolicy(
        *[
            PolicyDecision.allow_modified("again", {"query": f"round {index}"})
            for index in range(10)
        ]
    )
    harness = _Harness(policy=policy, max_policy_rounds=3)

    result, _ = _execute(harness, _call(query="fusion"))

    assert harness.tool.calls == []
    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert "kept rewriting" in result.error.message
    assert len(policy.seen) == 3


def test_a_refusal_answers_the_call_it_was_given() -> None:
    harness = _Harness()

    async def scenario() -> ToolResult:
        return await harness.gateway.refuse(
            _call(query="fusion"),
            ErrorInfo(code="cancelled", message="the run was cancelled"),
            sink=harness.sink,
        )

    result = asyncio.run(scenario())

    assert result.tool_call_id == "toolu_1"
    assert result.status == "error"


def test_advertising_an_unregistered_tool_raises() -> None:
    harness = _Harness()

    with pytest.raises(UnknownToolError, match="does not register"):
        harness.gateway.advertise(["search", "missing"])

    assert harness.gateway.advertise(["search"])[0].name == "search"


def test_the_proposal_is_recorded_before_anything_judges_the_call() -> None:
    """Even a call that is about to be refused leaves an audit record."""

    harness = _Harness()

    _, events = _execute(harness, _call())

    assert events[0] == "ToolProposed"


def test_a_prepared_call_carries_its_binding() -> None:
    harness = _Harness()

    async def scenario() -> PreparedCall | ToolResult:
        return await harness.gateway.prepare(
            _call(query="x"),
            context=CONTEXT,
            sink=harness.sink,
        )

    prepared = asyncio.run(scenario())

    assert isinstance(prepared, PreparedCall)
    assert prepared.binding.spec.name == "search"


class _Hook:
    """A hook that returns a fixed outcome and records what it saw."""

    def __init__(self, name: str, outcome: HookOutcome) -> None:
        self.name = name
        self.seen: list[ToolCall] = []
        self._outcome = outcome

    async def before_tool(
        self,
        call: ToolCall,
        context: ExecutionContext,
    ) -> HookOutcome:
        self.seen.append(call)
        return self._outcome


def test_a_hook_rewrite_is_revalidated_and_then_authorized() -> None:
    hook = _Hook("clamp", HookOutcome.rewrite({"query": "fusion", "top_k": 5}))
    policy = _ScriptedPolicy(PolicyDecision.allow("within_limits"))
    harness = _Harness(policy=policy, hooks=HookBus([hook]))

    result, events = _execute(harness, _call(query="fusion", top_k=9))

    assert result.status == "ok"
    # The handler ran on what the hook produced, and the policy decided on it.
    assert harness.tool.calls[0].arguments == {"query": "fusion", "top_k": 5}
    assert policy.seen[0].arguments == {"query": "fusion", "top_k": 5}
    assert events.count("PermissionResolved") == 1


def test_a_hook_rewrite_into_invalid_arguments_is_refused() -> None:
    """A hook that could edit past the check would be a way around it."""

    hook = _Hook("broken", HookOutcome.rewrite({"query": "fusion", "top_k": 99}))
    policy = _ScriptedPolicy()
    harness = _Harness(policy=policy, hooks=HookBus([hook]))

    result, _ = _execute(harness, _call(query="fusion"))

    assert harness.tool.calls == []
    assert policy.seen == []
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"


def test_a_blocking_hook_stops_the_call_before_any_decision() -> None:
    hook = _Hook("guard", HookOutcome.block("query mentions a forbidden path"))
    policy = _ScriptedPolicy()
    harness = _Harness(policy=policy, hooks=HookBus([hook]))

    result, events = _execute(harness, _call(query="fusion"))

    assert harness.tool.calls == []
    assert policy.seen == []
    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert "blocked by hook guard" in result.error.message
    assert events == ["ToolProposed", "ToolFailed"]


def test_hooks_never_see_a_call_that_failed_validation() -> None:
    """Hooks shape valid input; they are not a second parser for broken input."""

    hook = _Hook("observer", HookOutcome.unchanged())
    harness = _Harness(hooks=HookBus([hook]))

    result, _ = _execute(harness, _call(top_k=3))

    assert hook.seen == []
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"


def test_hooks_never_see_an_unknown_tool() -> None:
    hook = _Hook("observer", HookOutcome.unchanged())
    harness = _Harness(hooks=HookBus([hook]))

    _execute(harness, ToolCall(tool_call_id="toolu_1", tool_name="nope"))

    assert hook.seen == []


def test_a_decision_requiring_approval_does_not_reach_the_handler() -> None:
    """P0-2. The gateway is where "allow, pending approval" stops."""

    policy = _ScriptedPolicy(
        PolicyDecision.allow("write_needs_review", requires_approval=True)
    )
    harness = _Harness(policy=policy)

    result, events = _execute(harness, _call(query="fusion"))

    assert harness.tool.calls == []
    assert result.error is not None
    assert result.error.code == "approval_required"
    assert events == [
        "ToolProposed",
        "PermissionResolved",
        "PermissionRequested",
        "ToolFailed",
    ]


def test_a_rewrite_cannot_smuggle_a_call_past_its_approval_requirement() -> None:
    """The check sits before both allow branches, so a rewrite cannot skip it."""

    policy = _ScriptedPolicy(
        PolicyDecision.allow_modified(
            "clamped",
            {"query": "fusion", "top_k": 5},
            requires_approval=True,
        )
    )
    harness = _Harness(policy=policy)

    result, _ = _execute(harness, _call(query="fusion", top_k=9))

    assert harness.tool.calls == []
    assert result.error is not None
    assert result.error.code == "approval_required"


def test_a_policy_rewrite_is_held_to_the_argument_ceiling() -> None:
    """P1-6. The byte limit was something a policy could opt out of.

    Hook rewrites already went back through the whole check; policy rewrites
    re-ran only the schema. A schema that says ``query`` is a string says
    nothing about ten thousand of them.
    """

    policy = _ScriptedPolicy(
        PolicyDecision.allow_modified("expand", {"query": "x" * 10_000}),
        PolicyDecision.allow("would_permit_it"),
    )
    harness = _Harness(policy=policy, max_argument_bytes=64)

    result, _ = _execute(harness, _call(query="small"))

    assert harness.tool.calls == []
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"


def test_the_ceiling_refusal_does_not_quote_the_arguments() -> None:
    """Sizes go in the message; contents never do."""

    policy = _ScriptedPolicy(
        PolicyDecision.allow_modified("expand", {"query": "secret" * 2_000}),
        PolicyDecision.allow("would_permit_it"),
    )
    harness = _Harness(policy=policy, max_argument_bytes=64)

    result, _ = _execute(harness, _call(query="small"))

    assert result.error is not None
    assert "secret" not in result.error.message


def test_a_rewrite_inside_the_ceiling_still_runs() -> None:
    """The control: the check is the size, not the fact of a rewrite."""

    policy = _ScriptedPolicy(
        PolicyDecision.allow_modified("clamp", {"query": "fusion", "top_k": 5}),
        PolicyDecision.allow("permitted"),
    )
    harness = _Harness(policy=policy, max_argument_bytes=64)

    result, _ = _execute(harness, _call(query="fusion", top_k=9))

    assert result.error is None
    assert harness.tool.calls[0].arguments["top_k"] == 5


class _Hanging:
    """A policy engine that never answers."""

    async def decide(self, call: ToolCall, context: ExecutionContext) -> NoReturn:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _Raising:
    """A policy engine whose backend failure carries a secret in its message."""

    async def decide(self, call: ToolCall, context: ExecutionContext) -> NoReturn:
        raise RuntimeError("policy backend down: dsn=postgres://u:sk-ant-canary@h/db")


def test_a_policy_engine_that_hangs_is_refused_not_awaited() -> None:
    """P1-7. Deployment-supplied code sits on the path of every call."""

    harness = _Harness(policy=_Hanging(), policy_timeout_seconds=0.05)

    result, events = _execute(harness, _call(query="fusion"))

    assert harness.tool.calls == []
    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert events == ["ToolProposed", "ToolFailed"]


def test_a_policy_engine_that_raises_becomes_a_refusal() -> None:
    """P1-7. The caller was promised a terminal outcome, not an exception.

    It used to escape ``authorize`` entirely, so a run reported neither
    success nor failure -- it reported a traceback.
    """

    harness = _Harness(policy=_Raising())

    result, _ = _execute(harness, _call(query="fusion"))

    assert harness.tool.calls == []
    assert result.error is not None
    assert result.error.code == "policy_denied"


def test_only_the_exception_type_crosses_the_policy_boundary() -> None:
    """A backend's message is not vetted, and has carried a DSN."""

    harness = _Harness(policy=_Raising())

    result, _ = _execute(harness, _call(query="fusion"))

    assert result.error is not None
    assert "RuntimeError" in result.error.message
    assert "sk-ant-canary" not in result.error.message
    assert "postgres://" not in result.error.message


def test_the_run_deadline_bounds_the_policy_engine() -> None:
    """The run's remaining time wins when it is the smaller of the two."""

    harness = _Harness(policy=_Hanging(), policy_timeout_seconds=30.0)

    async def scenario() -> ToolResult:
        await harness.gateway.propose(_call(query="fusion"), sink=harness.sink)
        prepared = await harness.gateway.prepare(
            _call(query="fusion"), context=CONTEXT, sink=harness.sink
        )
        assert not isinstance(prepared, ToolResult)
        return await harness.gateway.authorize(  # pyright: ignore[reportReturnType]
            prepared,
            context=CONTEXT,
            sink=harness.sink,
            remaining_run_seconds=0.05,
        )

    result = asyncio.run(scenario())

    assert isinstance(result, ToolResult)
    assert result.error is not None
    assert result.error.code == "policy_denied"


def test_a_run_with_no_time_left_does_not_start_a_policy_call() -> None:
    """Nothing to spend, so nothing is started."""

    policy = _ScriptedPolicy(PolicyDecision.allow("would_permit_it"))
    harness = _Harness(policy=policy)

    async def scenario() -> ToolResult:
        prepared = await harness.gateway.prepare(
            _call(query="fusion"), context=CONTEXT, sink=harness.sink
        )
        assert not isinstance(prepared, ToolResult)
        return await harness.gateway.authorize(  # pyright: ignore[reportReturnType]
            prepared,
            context=CONTEXT,
            sink=harness.sink,
            remaining_run_seconds=0.0,
        )

    result = asyncio.run(scenario())

    assert isinstance(result, ToolResult)
    assert policy.seen == []


def test_a_policy_that_answers_in_time_is_unaffected() -> None:
    """The control: the bound is a bound, not a refusal."""

    harness = _Harness(policy=_ScriptedPolicy(PolicyDecision.allow("permitted")))

    result, _ = _execute(harness, _call(query="fusion"))

    assert result.error is None
    assert len(harness.tool.calls) == 1


# --- holding a call for a human ----------------------------------------------
#
# Without a gate the gateway refuses, and the two tests above the policy-bound
# section pin that unchanged: they are the control for everything here, and the
# behaviour every deployment that supplies no gate keeps. With a gate the call
# is held, and what has to be true is that no way of failing to get an answer
# can be mistaken for getting one.


class _Gate:
    """An approval gate that answers from a script, or never answers."""

    def __init__(
        self,
        answer: tuple[str, str] | None = ("approve_once", "human"),
        *,
        raises: Exception | None = None,
    ) -> None:
        self._answer = answer
        self._raises = raises
        self.requests: list[dict[str, object]] = []

    async def request(self, **kwargs: object) -> tuple[str, str]:
        self.requests.append(kwargs)
        if self._raises is not None:
            raise self._raises
        if self._answer is None:
            # Parked, the way a question nobody has looked at yet is parked.
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        return self._answer


def _needs_approval(reason: str = "write_needs_review") -> _ScriptedPolicy:
    return _ScriptedPolicy(PolicyDecision.allow(reason, requires_approval=True))


def _payload[Payload](events: list[EventEnvelope], kind: type[Payload]) -> Payload:
    """The one payload of this kind, narrowed so its fields can be read."""

    for envelope in events:
        if isinstance(envelope.payload, kind):
            return envelope.payload
    raise AssertionError(f"no {kind.__name__} in {[e.event_type for e in events]}")


def events_of(harness: _Harness) -> list[EventEnvelope]:
    """The stored envelopes, for the assertions that need a payload."""

    async def read() -> list[EventEnvelope]:
        return await harness.events()

    return asyncio.run(read())


def test_an_approved_call_runs_with_the_arguments_the_model_proposed() -> None:
    """The whole point: the handler is reached, and reached unchanged.

    The sequence is asserted whole rather than by containment. Where the two
    new events land is the claim -- the request and the pause precede the
    decision, and all three precede the first byte of work.
    """

    gate = _Gate(("approve_once", "human"))
    harness = _Harness(policy=_needs_approval(), approvals=gate)

    result, events = _execute(harness, _call(query="fusion", top_k=3))

    assert result.status == "ok"
    assert events == [
        "ToolProposed",
        "PermissionResolved",
        "PermissionRequested",
        "RunPaused",
        "ToolApprovalDecided",
        "ToolStarted",
        "ToolCompleted",
    ]
    assert len(harness.tool.calls) == 1
    assert harness.tool.calls[0].arguments == {"query": "fusion", "top_k": 3}


def test_the_gate_is_asked_about_this_call_and_not_about_the_tool() -> None:
    """``approve_for_session`` is unsafe to implement without the digest.

    The policy engine decides approval from the tool's declared risk and never
    reads the arguments, so a gate that could only see a tool name would have
    to let one approved call stand for every later one.
    """

    gate = _Gate()
    harness = _Harness(policy=_needs_approval(), approvals=gate)

    _execute(harness, _call(query="fusion"))

    (asked,) = gate.requests
    assert asked["tool_name"] == "search"
    assert asked["tool_call_id"] == "toolu_1"
    assert asked["argument_digest"] == argument_digest({"query": "fusion"})
    assert asked["approval_id"]


def test_a_standing_rule_still_says_so_on_the_record() -> None:
    """A rule answering on a human's behalf is not the human answering."""

    gate = _Gate(("approve_for_session", "session_rule"))
    harness = _Harness(policy=_needs_approval(), approvals=gate)

    result, events = _execute(harness, _call(query="fusion"))

    decided = _payload(events_of(harness), ToolApprovalDecided)

    assert result.status == "ok"
    assert len(harness.tool.calls) == 1
    assert events[-3:] == ["ToolApprovalDecided", "ToolStarted", "ToolCompleted"]
    assert decided.decision == "approve_for_session"
    assert decided.decided_by == "session_rule"


def test_a_denied_call_is_answered_not_raised() -> None:
    """One ToolResult per id is the invariant a refusal must not break."""

    gate = _Gate(("deny", "human"))
    harness = _Harness(policy=_needs_approval(), approvals=gate)

    result, events = _execute(harness, _call(query="fusion"))

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert result.tool_call_id == "toolu_1"
    assert len(harness.tool.calls) == 0
    assert events[-2:] == ["ToolApprovalDecided", "ToolFailed"]
    decided = _payload(events_of(harness), ToolApprovalDecided)
    assert decided.decided_by == "human"


def test_nobody_answering_is_recorded_as_nobody_answering() -> None:
    """Distinct from a denial, and the refusal alone cannot carry that."""

    gate = _Gate(None)
    harness = _Harness(
        policy=_needs_approval(),
        approvals=gate,
        approval_timeout_seconds=0.05,
    )

    result, _ = _execute(harness, _call(query="fusion"))
    decided = _payload(events_of(harness), ToolApprovalDecided)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert len(harness.tool.calls) == 0
    assert decided.decided_by == "timeout"


def test_a_gate_that_raises_is_a_refusal_that_says_which_gate_broke() -> None:
    """Not a timeout: an operator told "timeout" goes looking for a slow human."""

    gate = _Gate(raises=RuntimeError("postgres://user:pw@host/db is unreachable"))
    harness = _Harness(policy=_needs_approval(), approvals=gate)

    result, _ = _execute(harness, _call(query="fusion"))
    decided = _payload(events_of(harness), ToolApprovalDecided)

    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert decided.decided_by == "gate_failed"
    # Only the type name crosses, the same as for a policy engine that raises.
    assert "RuntimeError" in result.error.message
    assert "postgres://" not in result.error.message


def test_the_run_deadline_shortens_the_wait_that_is_actually_taken() -> None:
    """Two assertions, and the first is the one with something to lose.

    Telling the gate the run's number while enforcing the gateway's own
    hundred seconds would satisfy a test that only inspected the argument --
    and would be exactly the inversion that leaves a run held long past its
    deadline. So the wait is timed. The second assertion is the argument,
    because a gate that shows a countdown or answers ``timeout`` itself is
    working from it, and the two must not disagree in public.
    """

    gate = _Gate(None)
    harness = _Harness(
        policy=_needs_approval(),
        approvals=gate,
        approval_timeout_seconds=100.0,
    )

    result, elapsed = asyncio.run(_authorized_with(harness, remaining_run_seconds=0.05))

    assert elapsed < 5.0
    assert gate.requests[0]["timeout_seconds"] == 0.05
    assert result.error is not None


def test_a_run_with_no_time_left_is_refused_before_anybody_is_asked() -> None:
    """Nobody is asked about a run that is already over.

    The guard that fires is the policy engine's, not the approval wait's --
    the decision that raises an approval requirement is taken under the same
    remaining time, so a run with none never reaches the asking. Asserting
    *which* refusal came back is what keeps this from certifying a bound that
    is never reached.
    """

    gate = _Gate()
    harness = _Harness(policy=_needs_approval(), approvals=gate)

    result, _ = asyncio.run(_authorized_with(harness, remaining_run_seconds=-1.0))

    assert result.error is not None
    assert result.error.message == (
        "the run had no time left to reach a policy decision"
    )
    assert gate.requests == []


async def _authorized_with(
    harness: _Harness,
    *,
    remaining_run_seconds: float,
) -> tuple[ToolResult, float]:
    """Authorize one call with a run deadline, and time it.

    ``_Harness.run`` passes no deadline, and the outer ``wait_for`` is what
    turns "the bound was lost" into a failure. Without it a gateway that
    stopped bounding the wait would park this test on a gate that never
    answers, and a suite that hangs cannot be told apart from a wedged machine.
    """

    call = _call(query="fusion")
    await harness.gateway.propose(call, sink=harness.sink)
    prepared = await harness.gateway.prepare(call, context=CONTEXT, sink=harness.sink)
    assert isinstance(prepared, PreparedCall)
    started = time.monotonic()
    outcome = await asyncio.wait_for(
        harness.gateway.authorize(
            prepared,
            context=CONTEXT,
            sink=harness.sink,
            remaining_run_seconds=remaining_run_seconds,
        ),
        timeout=30.0,
    )
    elapsed = time.monotonic() - started
    assert isinstance(outcome, ToolResult)
    return outcome, elapsed


def test_cancelling_the_run_ends_the_wait_instead_of_outlasting_it() -> None:
    """The reason cancellation had to become something you can await.

    The bound here is a hundred seconds and the assertion is that the call is
    answered in well under one. Polling cannot produce that: the check would
    sit after a wait that has not ended.
    """

    gate = _Gate(None)
    harness = _Harness(
        policy=_needs_approval(),
        approvals=gate,
        approval_timeout_seconds=100.0,
    )
    cancellation = CancellationSource()

    async def scenario() -> ToolResult:
        call = _call(query="fusion")
        await harness.gateway.propose(call, sink=harness.sink)
        prepared = await harness.gateway.prepare(
            call, context=CONTEXT, sink=harness.sink
        )
        assert isinstance(prepared, PreparedCall)

        async def stop() -> None:
            await asyncio.sleep(0.05)
            cancellation.cancel("operator stopped the run")

        stopper = asyncio.ensure_future(stop())
        outcome = await asyncio.wait_for(
            harness.gateway.authorize(
                prepared,
                context=CONTEXT,
                sink=harness.sink,
                cancellation=cancellation,
            ),
            timeout=5.0,
        )
        await stopper
        assert isinstance(outcome, ToolResult)
        return outcome

    result = asyncio.run(scenario())
    decided = _payload(events_of(harness), ToolApprovalDecided)

    assert result.error is not None
    assert result.error.code == "cancelled"
    assert decided.decided_by == "cancelled"
    assert len(harness.tool.calls) == 0


def test_a_call_held_for_a_human_shows_them_what_they_are_permitting() -> None:
    """ADR-019's switch governs a record kept for later; this is a question now.

    The control is the second assertion. Filling this preview must not become
    a way of turning ``record_step_inputs`` on by the back door -- if it were,
    every prompt and every retrieved document would follow the arguments onto
    the stream.
    """

    harness = _Harness(
        policy=_needs_approval(),
        approvals=_Gate(),
        record_step_inputs=False,
    )

    _execute(harness, _call(query="rm -rf /tmp/x"))
    stored = events_of(harness)

    assert "rm -rf /tmp/x" in _payload(stored, PermissionRequested).approval_preview
    assert _payload(stored, ToolProposed).argument_preview == ""


def test_a_produced_filename_survives_a_deployment_that_records_no_previews() -> None:
    """ADR-063's whole reason to exist, and the only place it can be guarded.

    ``record_step_inputs`` is off here, so ``output_preview`` is empty and the
    proposal's ``argument_preview`` is empty with it -- which is exactly the
    deployment where the old way of recovering a produced name (parse the
    preview) recovers nothing. The name still has to be on the event, because a
    name is not content: the same principal can already list the whole
    workspace, so publishing it discloses nothing the preview gate was written
    to withhold.

    The second and third assertions are the control. If this test ever passes
    because somebody moved ``workspace_writes`` under the gate and turned the
    gate on, it is measuring nothing.
    """

    harness = _Harness(
        tool=_Search(workspace_writes=("report.md",)),
        record_step_inputs=False,
    )

    _execute(harness, _call(query="fusion"))
    stored = events_of(harness)

    assert _payload(stored, ToolCompleted).workspace_writes == ("report.md",)
    assert _payload(stored, ToolCompleted).output_preview == ""
    assert _payload(stored, ToolProposed).argument_preview == ""


def test_a_tool_that_cut_its_own_answer_says_so_even_with_previews_off() -> None:
    """The field had no producer at all until this line's counterpart shipped.

    ``ToolCompleted.truncated`` documented "the tool's own clipping" from the
    day it was written and nothing ever set it, so every event ever emitted
    said ``false``. The tool that actually does the clipping is
    ``delegate_agent``, and it marks the cut at the *end* of an 8000-character
    report -- which ``bounded()`` drops at 4096. So the preview a console reads
    is a half report with the marker gone, and this boolean is the only route
    left to the fact.

    Previews are off here on purpose, for the same reason ``workspace_writes``
    is not gated: a boolean saying an answer was cut discloses none of the text
    the gate withholds, and it matters most exactly where nobody can see the
    cut for themselves.
    """

    harness = _Harness(tool=_Search(truncated=True), record_step_inputs=False)

    _execute(harness, _call(query="fusion"))
    stored = events_of(harness)

    assert _payload(stored, ToolCompleted).truncated is True
    assert _payload(stored, ToolCompleted).output_preview == ""


def test_a_tool_that_answered_in_full_reports_no_cut() -> None:
    """The control. A flag that is always true reports nothing."""

    harness = _Harness(record_step_inputs=True)

    _execute(harness, _call(query="fusion"))

    assert _payload(events_of(harness), ToolCompleted).truncated is False


def test_a_call_that_wrote_nothing_says_so_rather_than_guessing() -> None:
    """The control for the field: it reports writes, it does not infer them."""

    harness = _Harness(record_step_inputs=True)

    _execute(harness, _call(query="fusion"))

    assert _payload(events_of(harness), ToolCompleted).workspace_writes == ()


def test_a_deployment_with_no_gate_shows_nothing_because_it_asks_nobody() -> None:
    """The control for the preview: it is written to be read, not to be kept."""

    harness = _Harness(policy=_needs_approval())

    _execute(harness, _call(query="rm -rf /tmp/x"))
    requested = _payload(events_of(harness), PermissionRequested)

    assert requested.approval_preview == ""
    assert requested.approval_id is None


def test_the_human_is_asked_about_the_arguments_that_will_actually_run() -> None:
    """A rewrite lands after the requirement is raised, so asking early asks
    about a call that is not the one dispatched.

    The policy clamps ``top_k`` from 9 to 5 *and* requires approval on the same
    decision. Every assertion here is about 5: the digest the gate was handed,
    the preview a person would have read, and the arguments the handler
    received. Asking on the pre-rewrite call would satisfy none of them, and
    would have shown somebody a nine.
    """

    gate = _Gate()
    policy = _ScriptedPolicy(
        PolicyDecision.allow_modified(
            "clamped",
            {"query": "fusion", "top_k": 5},
            requires_approval=True,
        )
    )
    harness = _Harness(policy=policy, approvals=gate)

    _execute(harness, _call(query="fusion", top_k=9))
    requested = _payload(events_of(harness), PermissionRequested)

    (asked,) = gate.requests
    assert asked["argument_digest"] == argument_digest({"query": "fusion", "top_k": 5})
    assert '"top_k":5' in requested.approval_preview.replace(" ", "")
    assert harness.tool.calls[0].arguments == {"query": "fusion", "top_k": 5}


def test_one_call_asks_one_question_however_many_rounds_it_takes() -> None:
    """Two rounds both requiring approval are still one thing to consent to."""

    gate = _Gate()
    policy = _ScriptedPolicy(
        PolicyDecision.allow_modified(
            "clamped",
            {"query": "fusion", "top_k": 5},
            requires_approval=True,
        ),
        PolicyDecision.allow_modified(
            "clamped_again",
            {"query": "fusion", "top_k": 4},
            requires_approval=True,
        ),
    )
    harness = _Harness(policy=policy, approvals=gate)

    _execute(harness, _call(query="fusion", top_k=9))

    assert len(gate.requests) == 1
    assert harness.tool.calls[0].arguments == {"query": "fusion", "top_k": 4}


def test_a_requirement_raised_once_is_not_dropped_by_a_later_round() -> None:
    """The control for the two tests above: asking late must not become
    asking never.

    Round one requires approval and rewrites; round two is a plain allow that
    says nothing about approval. Silence is not a withdrawal -- and forgetting
    it here is exactly the rewrite carrying a call past its own requirement.
    """

    gate = _Gate(("deny", "human"))
    policy = _ScriptedPolicy(
        PolicyDecision.allow_modified(
            "clamped",
            {"query": "fusion", "top_k": 5},
            requires_approval=True,
        ),
        PolicyDecision.allow("nothing_to_declare"),
    )
    harness = _Harness(policy=policy, approvals=gate)

    result, _ = _execute(harness, _call(query="fusion", top_k=9))

    assert len(gate.requests) == 1
    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert harness.tool.calls == []


@pytest.mark.parametrize(
    "answer",
    [("approved", "human"), ("rejected", "human"), ("approve_once", "operator")],
    ids=["unknown-decision", "the-other-vocabulary", "unknown-decider"],
)
def test_a_gate_answering_off_contract_refuses_rather_than_permits(
    answer: tuple[str, str],
) -> None:
    """Guarding the shape and trusting the words is the worse half of a guard.

    This repository already holds a second approval vocabulary --
    ``domain/task_registry.ApprovalDecision`` is ``"approved"``/``"rejected"``
    -- so the first gate somebody adapts from the existing approvals API
    answers in words this one does not know. ``"rejected"`` is the dangerous
    one: it is not ``"deny"``, so anything keying only on that string reads a
    human's refusal as permission.

    The failure must be a terminal ``ToolResult``. Feeding an unvalidated word
    into a Literal-typed event raises out of ``authorize`` instead, and the
    runtime has no handler there: the whole run unwinds with a traceback and
    the call never gets the answer every id is owed.
    """

    gate = _Gate(answer)
    harness = _Harness(policy=_needs_approval(), approvals=gate)

    result, _ = _execute(harness, _call(query="fusion"))
    decided = _payload(events_of(harness), ToolApprovalDecided)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert decided.decided_by == "gate_failed"
    assert harness.tool.calls == []


def test_a_preview_that_had_to_be_cut_says_so() -> None:
    """A cut with no sign reads as the whole request.

    The tail of a long command is where the redirect and the second path live,
    and a person who cannot tell that it was removed by a length limit reads
    the preview as the entire call. The control is the short case: an argument
    that fits must not be decorated, or every preview looks truncated and the
    mark stops meaning anything.
    """

    long_harness = _Harness(policy=_needs_approval(), approvals=_Gate())
    short_harness = _Harness(policy=_needs_approval(), approvals=_Gate())

    _execute(long_harness, _call(query="x" * 4000))
    _execute(short_harness, _call(query="fusion"))

    cut = _payload(events_of(long_harness), PermissionRequested).approval_preview
    whole = _payload(events_of(short_harness), PermissionRequested).approval_preview

    assert cut.endswith("...[truncated]")
    assert len(cut) == 2048
    assert whole == '{"query":"fusion"}'


class _FailsWhileBeingTornDown:
    """Honours cancellation by cleaning up, and fails at the cleanup.

    Exactly the shape ``InteractiveApprovalGate`` obliges implementations to
    have -- drop the pending question when cancelled -- with the drop itself
    going wrong, which is what a lost connection looks like.
    """

    async def request(self, **kwargs: object) -> tuple[str, str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError(
                "postgres://user:pw@host/db failed to deregister"
            ) from None
        raise AssertionError("unreachable")


def test_an_abandoned_gate_task_does_not_get_its_message_logged_for_it() -> None:
    """Scrubbing the message out of the refusal and letting asyncio print it is
    not scrubbing it.

    A task nobody retrieves the exception from is reported by the event loop's
    default handler, in full. The gateway's rule is that only a gate's
    exception *type name* crosses this boundary, and the loop does not know
    that rule.
    """

    unhandled: list[object] = []
    harness = _Harness(
        policy=_needs_approval(),
        approvals=_FailsWhileBeingTornDown(),
        approval_timeout_seconds=0.05,
    )

    async def scenario() -> ToolResult:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: unhandled.append(context)
        )
        result = await asyncio.wait_for(harness.run(_call(query="fusion")), timeout=30)
        # Let the abandoned task finish and be collected: the loop reports an
        # unretrieved exception at collection, not at cancellation.
        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0)
        return result

    result = asyncio.run(scenario())

    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert unhandled == []


def test_the_word_a_gate_invented_does_not_travel_with_the_refusal() -> None:
    """It reached this process from deployment-supplied code, like a message."""

    harness = _Harness(
        policy=_needs_approval(),
        approvals=_Gate(("approved-by-postgres://user:pw@host/db", "human")),
    )

    result, _ = _execute(harness, _call(query="fusion"))

    assert result.error is not None
    assert "postgres://" not in result.error.message
