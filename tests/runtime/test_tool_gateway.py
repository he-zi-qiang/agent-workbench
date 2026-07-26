"""The gateway: nothing runs until its final arguments have passed both checks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from itertools import count

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.domain.errors import ErrorInfo, UnknownToolError
from agent_workbench.domain.events import EventEnvelope
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PolicyDecision,
    PrincipalContext,
)
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.cancellation import NullCancellationToken
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

    def __init__(self, *, schema: JsonObject | None = None) -> None:
        self.calls: list[ToolCall] = []
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
        return ToolResult.succeeded(invocation.call, content="1 hit")


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
        result = await harness.run(call)
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
