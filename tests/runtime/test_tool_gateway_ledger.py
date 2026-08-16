"""The side-effect protocol, as the gateway performs it.

The ledger's own contract is tested against real PostgreSQL elsewhere. What is
under test here is the *order* the gateway does things in, which is where an
external effect gets dispatched twice or dispatched after it stopped being
allowed:

* record the intent before dispatching, never after;
* re-check authorization between the intent and the irreversible act, because
  tightening is required to take effect at the next authorization boundary and
  this is that boundary;
* answer from the ledger when the operation is already settled, instead of
  performing it again;
* report a definite outcome as knowledge, and an absent one as a question for a
  human.

The ledger here is a fake that records calls in order. That is the point: the
assertion is about sequence, and a real database would only make the sequence
harder to see.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.errors import ErrorInfo, OperationCancelledError
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PolicyDecision,
    PrincipalContext,
)
from agent_workbench.domain.tools import (
    ToolCall,
    ToolResult,
    ToolSpec,
    argument_digest,
)
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.tool_executions import (
    ToolExecutionIntent,
    ToolExecutionNotWritableError,
    ToolExecutionRecord,
    ToolOperationConflictError,
)
from agent_workbench.ports.tools import ToolBinding, ToolInvocation
from agent_workbench.runtime.tool_gateway import PreparedCall, ToolGateway

NOW = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)

WRITE_SPEC = ToolSpec(
    name="export_report",
    description="Publish the finished report to the external destination.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["artifact_id"],
        "properties": {"artifact_id": {"type": "string", "minLength": 1}},
    },
    concurrency="exclusive",
    risk="external",
    idempotency="keyed",
    timeout_seconds=30,
    permission_scopes=("report:publish",),
)

ARGUMENTS: dict[str, Any] = {"artifact_id": "art_1"}
OPERATION_KEY = "export:art_1"


def _operation_key(call: ToolCall, context: ExecutionContext) -> str:
    """A stable business key: the artifact, not the call id."""

    return f"export:{call.arguments['artifact_id']}"


@dataclass
class _Ledger:
    """Records the order it was asked things in."""

    calls: list[str] = field(default_factory=list)
    #: What each settlement said, so a test can assert the ledger was told what
    #: the effect produced and not merely that it succeeded.
    details: list[str | None] = field(default_factory=list)
    record: ToolExecutionRecord | None = None
    on_intent: Exception | None = None
    on_settle: Exception | None = None

    def _default(self, status: str = "intended") -> ToolExecutionRecord:
        return ToolExecutionRecord(
            execution_id="texec_1",
            task_id="task_1",
            operation_key=OPERATION_KEY,
            tool_name=WRITE_SPEC.name,
            canonical_request_hash=argument_digest(ARGUMENTS),
            status=status,  # type: ignore[arg-type]
            lease_epoch=1,
            agent_run_id="run_1",
            tool_call_id="toolu_01",
            policy_identity="policy-1:ffff",
            intended_at=NOW,
            settled_at=None if status == "intended" else NOW,
        )

    async def record_intent(self, intent: ToolExecutionIntent) -> ToolExecutionRecord:
        self.calls.append(f"intent:{intent.operation_key}")
        if self.on_intent is not None:
            raise self.on_intent
        return self.record if self.record is not None else self._default()

    async def record_result(
        self,
        *,
        task_id: str,
        operation_key: str,
        lease_epoch: int,
        succeeded: bool,
        detail: str | None = None,
    ) -> ToolExecutionRecord:
        self.calls.append(f"result:{'ok' if succeeded else 'failed'}")
        self.details.append(detail)
        if self.on_settle is not None:
            raise self.on_settle
        return self._default("succeeded" if succeeded else "failed")

    async def mark_for_reconciliation(
        self, *, task_id: str, operation_key: str, lease_epoch: int, detail: str
    ) -> ToolExecutionRecord:
        self.calls.append("reconcile")
        if self.on_settle is not None:
            raise self.on_settle
        return self._default("needs_reconciliation")

    async def get(
        self, *, task_id: str, operation_key: str
    ) -> ToolExecutionRecord | None:
        return self.record


class _Policy:
    """Allows, unless told to change its mind after the intent is recorded."""

    def __init__(
        self,
        *,
        deny_after: int | None = None,
        require_approval_after: int | None = None,
        rewrite_after: int | None = None,
    ) -> None:
        self.decisions = 0
        self._deny_after = deny_after
        self._require_approval_after = require_approval_after
        self._rewrite_after = rewrite_after

    async def decide(self, call: ToolCall, context: ExecutionContext) -> PolicyDecision:
        self.decisions += 1
        if self._deny_after is not None and self.decisions > self._deny_after:
            return PolicyDecision(effect="deny", reason_code="acl_revoked")
        if (
            self._require_approval_after is not None
            and self.decisions > self._require_approval_after
        ):
            return PolicyDecision(
                effect="allow",
                reason_code="now_needs_review",
                requires_approval=True,
            )
        if self._rewrite_after is not None and self.decisions > self._rewrite_after:
            return PolicyDecision.allow_modified("clamped", {"target": "other"})
        return PolicyDecision(effect="allow", reason_code="allowed")


@dataclass
class _Handler:
    """A stand-in for the outside world."""

    dispatches: list[str] = field(default_factory=list)
    outcome: str = "ok"
    produces: ArtifactRef | None = None

    async def __call__(self, invocation: ToolInvocation) -> ToolResult:
        self.dispatches.append(invocation.call.tool_call_id)
        if self.outcome == "raise":
            raise RuntimeError("the provider rejected the request")
        if self.outcome == "cancel":
            raise OperationCancelledError("the run was cancelled")
        if self.outcome == "error":
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(code="tool_failed", message="the destination refused"),
            )
        return ToolResult.succeeded(
            invocation.call, content="published", artifact=self.produces
        )


def _context(**overrides: Any) -> ExecutionContext:
    base: dict[str, Any] = {
        "principal": PrincipalContext(tenant_id="tenant_a", principal_id="user_1"),
        "envelope": AuthorizationEnvelope(),
        "agent_run_id": "run_1",
        "policy_identity": "policy-1:ffff",
        "task_id": "task_1",
        "lease_epoch": 1,
    }
    base.update(overrides)
    return ExecutionContext.model_validate(base)


def _call() -> ToolCall:
    return ToolCall(
        tool_call_id="toolu_01", tool_name=WRITE_SPEC.name, arguments=ARGUMENTS
    )


def _sink() -> ScopedEventSink:
    return ScopedEventSink(
        InMemoryEventLog(), EventScope(stream_id="stream_1", run_id="run_1")
    )


def _gateway(
    ledger: _Ledger | None, handler: _Handler, *, policy: _Policy | None = None
) -> ToolGateway:
    binding = ToolBinding(
        spec=WRITE_SPEC, handler=handler, operation_key=_operation_key
    )
    return ToolGateway(
        registry=StaticToolRegistry([binding]),
        policy=policy or _Policy(),  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
    )


async def _invoke(gateway: ToolGateway, handler: _Handler, **kwargs: Any) -> ToolResult:
    binding = ToolBinding(
        spec=WRITE_SPEC, handler=handler, operation_key=_operation_key
    )
    return await gateway.invoke(
        PreparedCall(binding=binding, call=_call()),
        context=kwargs.pop("context", _context()),
        cancellation=NullCancellationToken(),
        sink=_sink(),
    )


# --------------------------------------------------------------------------
# The order
# --------------------------------------------------------------------------


def test_the_intent_is_recorded_before_the_effect_is_dispatched() -> None:
    """The whole protocol in one assertion.

    If the dispatch came first, a crash between it and the record would leave
    an effect nothing knows about -- which is the state the ledger exists to
    make impossible.
    """

    ledger, handler = _Ledger(), _Handler()
    gateway = _gateway(ledger, handler)

    result = asyncio.run(_invoke(gateway, handler))

    assert result.status == "ok"
    assert ledger.calls == [f"intent:{OPERATION_KEY}", "result:ok"]
    assert handler.dispatches == ["toolu_01"]


def test_authorization_is_checked_again_between_the_intent_and_the_dispatch() -> None:
    """Tightening takes effect at the next authorization boundary.

    The policy allows once -- at `authorize` -- and denies afterwards, which is
    an ACL revoked while the call queued. The handler must never run, and the
    operation must be recorded as failed rather than left for a human: nothing
    was dispatched, and that is knowledge.
    """

    ledger, handler = _Ledger(), _Handler()
    policy = _Policy(deny_after=0)
    gateway = _gateway(ledger, handler, policy=policy)

    result = asyncio.run(_invoke(gateway, handler))

    assert result.status == "error"
    assert handler.dispatches == []
    assert ledger.calls == [f"intent:{OPERATION_KEY}", "result:failed"]
    assert policy.decisions == 1


def test_approval_required_at_the_second_boundary_is_a_refusal_too() -> None:
    """ "Allow, pending approval" is not "allow" here either.

    This second decision is taken one line from dispatching an external
    effect, and it only checked ``deny``: a policy that answered
    ``allow`` + ``requires_approval`` was read as permission. Unreachable
    while approval was always a refusal, and live the moment it stopped being
    one -- on exactly the tools where approval matters most, since only a
    ledgered binding comes through here.

    Refused rather than asked again. The question was already answered at the
    first boundary; re-opening it would let a policy that flaps put the same
    call in front of a human twice.
    """

    ledger, handler = _Ledger(), _Handler()
    policy = _Policy(require_approval_after=0)
    gateway = _gateway(ledger, handler, policy=policy)

    result = asyncio.run(_invoke(gateway, handler))

    assert result.status == "error"
    assert result.error is not None
    assert "approval was required again" in result.error.message
    assert handler.dispatches == []
    # Settled, not left intended: nothing was dispatched, and that is knowledge.
    assert ledger.calls == [f"intent:{OPERATION_KEY}", "result:failed"]


def test_a_rewrite_at_the_second_boundary_is_a_refusal_and_not_a_dispatch() -> None:
    """ "Not with these arguments" is the one answer this branch used to read as
    "with these arguments".

    Only ``deny`` was checked, so a policy tightening into
    ``allow_with_modified_input`` sent the ORIGINAL, unrewritten call to the
    handler -- on the ledgered path, where the handler performs an external
    effect. The rewrite is not applied instead: the ledger already recorded an
    intent for the arguments in hand, and dispatching different ones would
    leave that row describing a call that never happened.
    """

    ledger, handler = _Ledger(), _Handler()
    policy = _Policy(rewrite_after=0)
    gateway = _gateway(ledger, handler, policy=policy)

    result = asyncio.run(_invoke(gateway, handler))

    assert result.status == "error"
    assert result.error is not None
    assert "no longer permitted with these arguments" in result.error.message
    assert handler.dispatches == []
    assert ledger.calls == [f"intent:{OPERATION_KEY}", "result:failed"]


def test_a_settled_operation_is_answered_from_the_ledger_not_performed_again() -> None:
    """The control group is the first test: an ``intended`` row does dispatch."""

    ledger, handler = _Ledger(), _Handler()
    ledger.record = ledger._default("succeeded")
    gateway = _gateway(ledger, handler)

    result = asyncio.run(_invoke(gateway, handler))

    assert result.status == "error"
    assert result.error is not None
    assert "already succeeded" in result.error.message
    assert result.error.retryable is False
    # Never dispatched, and no second settlement written over the first.
    assert handler.dispatches == []
    assert ledger.calls == [f"intent:{OPERATION_KEY}"]


def test_an_operation_awaiting_a_human_is_not_dispatched_either() -> None:
    ledger, handler = _Ledger(), _Handler()
    ledger.record = ledger._default("needs_reconciliation")
    gateway = _gateway(ledger, handler)

    result = asyncio.run(_invoke(gateway, handler))

    assert result.status == "error"
    assert handler.dispatches == []


# --------------------------------------------------------------------------
# What the outcome is recorded as
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        # The handler answered: that is knowledge, whichever way it went.
        ("ok", "result:ok"),
        ("error", "result:failed"),
        ("raise", "result:failed"),
        # No answer arrived. For an external write that does not mean no
        # effect -- the request may have landed after the deadline passed.
        ("cancel", "reconcile"),
    ],
)
def test_only_an_absent_answer_becomes_a_question_for_a_human(
    outcome: str, expected: str
) -> None:
    ledger, handler = _Ledger(), _Handler(outcome=outcome)
    gateway = _gateway(ledger, handler)

    asyncio.run(_invoke(gateway, handler))

    assert ledger.calls == [f"intent:{OPERATION_KEY}", expected]
    # Every one of these dispatched; what differs is only what came back.
    assert handler.dispatches == ["toolu_01"]


def test_a_timeout_is_recorded_as_unknown_rather_than_failed() -> None:
    """The case the whole distinction exists for.

    A deadline passing says nothing about whether the request landed, so the
    operation goes to a human instead of being retried or written off.
    """

    ledger = _Ledger()

    async def slow(invocation: ToolInvocation) -> ToolResult:
        await asyncio.sleep(5)
        raise AssertionError("the timeout should have fired")

    handler = _Handler()
    binding = ToolBinding(
        spec=WRITE_SPEC.model_copy(update={"timeout_seconds": 1}),
        handler=slow,
        operation_key=_operation_key,
    )
    gateway = ToolGateway(
        registry=StaticToolRegistry([binding]),
        policy=_Policy(),  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
    )

    async def scenario() -> ToolResult:
        return await gateway.invoke(
            PreparedCall(binding=binding, call=_call()),
            context=_context(),
            cancellation=NullCancellationToken(),
            sink=_sink(),
            run_budget_seconds=0.05,
        )

    result = asyncio.run(scenario())

    assert result.status == "error"
    assert ledger.calls == [f"intent:{OPERATION_KEY}", "reconcile"]
    assert handler.dispatches == []


# --------------------------------------------------------------------------
# Refusing rather than dispatching unrecorded
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "context",
    [
        # No Task: nothing to record the operation against.
        _context(task_id=None),
        # No claim: nothing to fence the record with.
        _context(lease_epoch=None),
    ],
)
def test_a_run_that_cannot_record_does_not_dispatch(context: Any) -> None:
    """Refusing costs an operation; dispatching would cost an unaccounted effect."""

    ledger, handler = _Ledger(), _Handler()
    gateway = _gateway(ledger, handler)

    result = asyncio.run(_invoke(gateway, handler, context=context))

    assert result.status == "error"
    assert handler.dispatches == []
    assert ledger.calls == []


def test_a_conflicting_operation_key_refuses_before_dispatch() -> None:
    ledger, handler = _Ledger(), _Handler()
    ledger.on_intent = ToolOperationConflictError(
        task_id="task_1",
        operation_key=OPERATION_KEY,
        recorded_hash="a" * 64,
        attempted_hash="b" * 64,
    )
    gateway = _gateway(ledger, handler)

    result = asyncio.run(_invoke(gateway, handler))

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "invalid_tool_input"
    assert handler.dispatches == []


def test_an_intent_the_lease_cannot_write_refuses_before_dispatch() -> None:
    """A Worker that lost the Task must not dispatch under it."""

    ledger, handler = _Ledger(), _Handler()
    ledger.on_intent = ToolExecutionNotWritableError(
        operation_key=OPERATION_KEY,
        found_status="running",
        found_lease_epoch=2,
        attempted_lease_epoch=1,
    )
    gateway = _gateway(ledger, handler)

    result = asyncio.run(_invoke(gateway, handler))

    assert result.status == "error"
    assert handler.dispatches == []


def test_a_lost_lease_at_settlement_still_answers_the_call() -> None:
    """The effect happened; the report did not land. Both are true.

    The Worker that replaced this one owns the operation and will read the
    truthful ``intended`` row, so losing this write loses nothing -- but the
    model is holding a tool_call_id, and turning a lost write into an exception
    would replace its answer with one nobody promised.
    """

    ledger, handler = _Ledger(), _Handler()
    ledger.on_settle = ToolExecutionNotWritableError(
        operation_key=OPERATION_KEY,
        found_status="running",
        found_lease_epoch=2,
        attempted_lease_epoch=1,
    )
    gateway = _gateway(ledger, handler)

    result = asyncio.run(_invoke(gateway, handler))

    assert result.status == "ok"
    assert handler.dispatches == ["toolu_01"]


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def test_a_process_holding_a_ledgered_tool_without_a_ledger_refuses_to_start() -> None:
    """Not per call: a deployment that cannot honour the protocol should not run."""

    handler = _Handler()
    binding = ToolBinding(
        spec=WRITE_SPEC, handler=handler, operation_key=_operation_key
    )

    with pytest.raises(ValueError, match="no ledger was supplied"):
        ToolGateway(
            registry=StaticToolRegistry([binding]),
            policy=_Policy(),  # type: ignore[arg-type]
        )


def test_a_tool_with_no_operation_key_still_assembles_without_a_ledger() -> None:
    """The control group: only a ledgered tool needs one."""

    read_spec = WRITE_SPEC.model_copy(
        update={
            "name": "read_report",
            "risk": "read",
            "idempotency": "safe",
            "concurrency": "parallel",
            "permission_scopes": (),
        }
    )
    gateway = ToolGateway(
        registry=StaticToolRegistry([ToolBinding(spec=read_spec, handler=_Handler())]),
        policy=_Policy(),  # type: ignore[arg-type]
    )

    assert gateway.advertise(["read_report"])[0].name == "read_report"


def test_a_safe_tool_cannot_carry_an_operation_key() -> None:
    """`safe` says repeat freely; a ledger exists to stop repetition."""

    read_spec = WRITE_SPEC.model_copy(
        update={
            "name": "read_report",
            "risk": "read",
            "idempotency": "safe",
            "concurrency": "parallel",
            "permission_scopes": (),
        }
    )

    with pytest.raises(ValueError, match="safe idempotency"):
        ToolBinding(spec=read_spec, handler=_Handler(), operation_key=_operation_key)


def test_a_successful_effect_records_what_it_produced() -> None:
    """The row has to name the object, not just say an object was made.

    A crash between this settlement and the caller's own checkpoint otherwise
    leaves an operation that provably succeeded and nothing that can reach what
    it produced -- and the only remaining moves are performing the effect a
    second time or handing a person a row that says something exists somewhere.
    """

    ledger, handler = _Ledger(), _Handler()
    handler.produces = ArtifactRef(
        artifact_id="art_report_1",
        tenant_id="tenant_a",
        kind="report",
        media_type="text/markdown",
        size_bytes=11,
        sha256="a" * 64,
    )
    gateway = _gateway(ledger, handler)

    result = asyncio.run(_invoke(gateway, handler))

    assert result.status == "ok"
    assert ledger.details == ["art_report_1"]


def test_an_effect_that_produced_nothing_records_no_detail() -> None:
    """Only an id belongs here. A tool with no artifact has nothing to name."""

    ledger, handler = _Ledger(), _Handler()
    gateway = _gateway(ledger, handler)

    asyncio.run(_invoke(gateway, handler))

    assert ledger.details == [None]
