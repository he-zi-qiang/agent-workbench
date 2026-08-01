"""The export node's path through the gateway, and what a second attempt does.

The ledger here is a stateful fake with the real one's semantics: one row per
key, an existing row is returned rather than replaced, and a settled row is not
re-opened. That is what makes the central claim testable without a database --
drive the same export twice and count the artifacts.

The ledger's own contract against real PostgreSQL is tested in
``tests/persistence/test_tool_executions.py``. What is under test here is the
node's side: that it goes through the gateway at all, and that it recovers from
a settled row instead of exporting again.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory.artifact_store import InMemoryArtifactStore
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.tools import ExportArtifactTool, StaticToolRegistry
from agent_workbench.adapters.tools.export_artifact import TOOL_NAME
from agent_workbench.adapters.tools.task_export import (
    ExportRefusedError,
    ExportUnrecoverableError,
    GatewayReportExport,
)
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.tool_executions import (
    ToolExecutionIntent,
    ToolExecutionRecord,
    ToolOperationConflictError,
)
from agent_workbench.runtime.tool_gateway import ToolGateway

NOW = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
TENANT = "tenant_a"
OWNER = "user_1"

# The envelope a v1 Task is submitted under: this tool, at this ceiling, with
# the human already consulted at the graph boundary.
ENVELOPE = AuthorizationEnvelope(
    allowed_tools=(TOOL_NAME,),
    max_tool_risk="write",
    approval_required_risks=(),
)


class _Ledger:
    """One row per key, with the real adapter's settle-once semantics."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], ToolExecutionRecord] = {}

    async def record_intent(self, intent: ToolExecutionIntent) -> ToolExecutionRecord:
        key = (intent.task_id, intent.operation_key)
        existing = self.rows.get(key)
        if existing is not None:
            if existing.canonical_request_hash != intent.canonical_request_hash:
                raise ToolOperationConflictError(
                    task_id=intent.task_id,
                    operation_key=intent.operation_key,
                    recorded_hash=existing.canonical_request_hash,
                    attempted_hash=intent.canonical_request_hash,
                )
            return existing
        record = ToolExecutionRecord(
            execution_id=f"texec_{len(self.rows) + 1}",
            task_id=intent.task_id,
            operation_key=intent.operation_key,
            tool_name=intent.tool_name,
            canonical_request_hash=intent.canonical_request_hash,
            status="intended",
            lease_epoch=intent.lease_epoch,
            agent_run_id=intent.agent_run_id,
            tool_call_id=intent.tool_call_id,
            policy_identity=intent.policy_identity,
            intended_at=NOW,
        )
        self.rows[key] = record
        return record

    async def record_result(
        self,
        *,
        task_id: str,
        operation_key: str,
        lease_epoch: int,
        succeeded: bool,
        detail: str | None = None,
    ) -> ToolExecutionRecord:
        record = self.rows[(task_id, operation_key)].model_copy(
            update={
                "status": "succeeded" if succeeded else "failed",
                "outcome_detail": detail,
                "settled_at": NOW,
            }
        )
        self.rows[(task_id, operation_key)] = record
        return record

    async def mark_for_reconciliation(
        self, *, task_id: str, operation_key: str, lease_epoch: int, detail: str
    ) -> ToolExecutionRecord:
        record = self.rows[(task_id, operation_key)].model_copy(
            update={
                "status": "needs_reconciliation",
                "outcome_detail": detail,
                "settled_at": NOW,
            }
        )
        self.rows[(task_id, operation_key)] = record
        return record

    async def get(
        self, *, task_id: str, operation_key: str
    ) -> ToolExecutionRecord | None:
        return self.rows.get((task_id, operation_key))


def _context(**overrides: object) -> ExecutionContext:
    base: dict[str, object] = {
        # The scope the tool declares. The envelope says which tools this work
        # may reach; the scopes say whether this person may reach them, and the
        # policy engine requires both.
        "principal": PrincipalContext(
            tenant_id=TENANT, principal_id=OWNER, scopes=("artifact:export",)
        ),
        "envelope": ENVELOPE,
        "agent_run_id": "run_1",
        "policy_identity": "rev_1:ffff",
        "task_id": "task_1",
        "lease_epoch": 3,
    }
    base.update(overrides)
    return ExecutionContext.model_validate(base)


def _sink() -> ScopedEventSink:
    return ScopedEventSink(
        InMemoryEventLog(), EventScope(stream_id="stream_1", run_id="run_1")
    )


def _exporter(store: InMemoryArtifactStore, ledger: _Ledger) -> GatewayReportExport:
    registry = StaticToolRegistry([ExportArtifactTool(artifacts=store).binding()])
    gateway = ToolGateway(
        registry=registry,
        policy=EnvelopePolicyEngine(registry=registry),
        ledger=ledger,  # type: ignore[arg-type]
    )
    return GatewayReportExport(gateway=gateway, ledger=ledger)  # type: ignore[arg-type]


async def _draft(store: InMemoryArtifactStore, body: bytes = b"body") -> str:
    reference = await store.put(
        tenant_id=TENANT,
        owner_id=OWNER,
        kind="report",
        media_type="text/markdown",
        content=body,
    )
    return reference.artifact_id


async def _export(
    exporter: GatewayReportExport, draft_ref: str, **overrides: object
) -> str:
    return await exporter.export(
        draft_ref=draft_ref,
        approval_id="apr_1",
        execution=_context(**overrides),
        sink=_sink(),
        cancellation=NullCancellationToken(),
    )


def test_the_export_goes_through_the_gateway_and_returns_its_artifact() -> None:
    async def scenario() -> None:
        store, ledger = InMemoryArtifactStore(), _Ledger()
        exporter = _exporter(store, ledger)
        draft_ref = await _draft(store)

        artifact_id = await _export(exporter, draft_ref)

        assert artifact_id.startswith("art_")
        row = await ledger.get(task_id="task_1", operation_key="export:task_1")
        assert row is not None
        assert row.status == "succeeded"
        # The row names what was made, which is what the recovery below reads.
        assert row.outcome_detail == artifact_id

    asyncio.run(scenario())


def test_a_second_attempt_recovers_the_first_export_instead_of_repeating_it() -> None:
    """The claim the whole ledger exists for, stated as a count.

    This is the crash window: a Worker exports, the ledger settles, and the
    checkpoint carrying the reference is never written. The Task is reclaimed
    and the node runs again. One report must exist afterwards, and the second
    attempt must name it.
    """

    async def scenario() -> None:
        store, ledger = InMemoryArtifactStore(), _Ledger()
        exporter = _exporter(store, ledger)
        draft_ref = await _draft(store)

        first = await _export(exporter, draft_ref)
        second = await _export(exporter, draft_ref)

        assert first == second
        # Two artifacts exist in total: the draft, and the one report.
        assert len(store._objects) == 2  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_a_settled_export_that_named_nothing_is_a_persons_problem() -> None:
    """Refusing to guess is the point.

    The alternative is exporting a second report because the row was unhelpful,
    which is the exact duplicate the ledger was holding the line against.
    """

    async def scenario() -> None:
        store, ledger = InMemoryArtifactStore(), _Ledger()
        exporter = _exporter(store, ledger)
        draft_ref = await _draft(store)
        await _export(exporter, draft_ref)
        key = ("task_1", "export:task_1")
        ledger.rows[key] = ledger.rows[key].model_copy(update={"outcome_detail": None})

        with pytest.raises(ExportUnrecoverableError):
            await _export(exporter, draft_ref)

        assert len(store._objects) == 2  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_exporting_a_different_draft_under_the_same_task_is_refused() -> None:
    """One key, one request. A changed draft has to collide, not slip past."""

    async def scenario() -> None:
        store, ledger = InMemoryArtifactStore(), _Ledger()
        exporter = _exporter(store, ledger)
        first_draft = await _draft(store, b"first")
        other_draft = await _draft(store, b"second")
        await _export(exporter, first_draft)

        with pytest.raises(ExportRefusedError):
            await _export(exporter, other_draft)

        # Draft, other draft, and exactly one report.
        assert len(store._objects) == 3  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_an_envelope_that_does_not_name_the_tool_exports_nothing() -> None:
    """Fail closed, and fail loudly.

    An approved Task that silently settles with no report is the failure this
    node exists to prevent, so the refusal has to reach the caller as one.
    """

    async def scenario() -> None:
        store, ledger = InMemoryArtifactStore(), _Ledger()
        exporter = _exporter(store, ledger)
        draft_ref = await _draft(store)

        with pytest.raises(ExportRefusedError):
            await _export(exporter, draft_ref, envelope=AuthorizationEnvelope())

        assert len(store._objects) == 1  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_an_envelope_requiring_tool_approval_exports_nothing() -> None:
    """v1's human sits at the graph boundary; there is no tool-level facility.

    Kept as a test rather than a comment because it is the reason ADR-015 sets
    ``approval_required_risks`` to empty: with ``write`` in it, the gateway's
    only answer is a refusal nothing can lift.
    """

    async def scenario() -> None:
        store, ledger = InMemoryArtifactStore(), _Ledger()
        exporter = _exporter(store, ledger)
        draft_ref = await _draft(store)

        with pytest.raises(ExportRefusedError):
            await _export(
                exporter,
                draft_ref,
                envelope=ENVELOPE.model_copy(
                    update={"approval_required_risks": ("write",)}
                ),
            )

        assert len(store._objects) == 1  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_a_run_without_a_lease_epoch_exports_nothing() -> None:
    """The ledger fences on the epoch; without one there is nothing to fence."""

    async def scenario() -> None:
        store, ledger = InMemoryArtifactStore(), _Ledger()
        exporter = _exporter(store, ledger)
        draft_ref = await _draft(store)

        with pytest.raises(ExportRefusedError):
            await _export(exporter, draft_ref, lease_epoch=None)

        assert ledger.rows == {}
        assert len(store._objects) == 1  # type: ignore[attr-defined]

    asyncio.run(scenario())
