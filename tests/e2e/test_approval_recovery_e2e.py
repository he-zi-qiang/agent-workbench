"""E2E 3 of 3: pause for a human, lose the process, approve, export exactly once.

The baseline names three fixed demonstrations and this is the third: "导出审批前
暂停 → 杀死进程 → 批准 → 其他 Worker 恢复 → 只生成一个产物". The Definition of Done
states the same property as "审批后的副作用在恢复测试中只发生一次".

``tests/persistence/test_task_approval_recovery.py`` already proves the control
flow: the graph interrupts, one Worker exits, a Worker built from a fresh engine
finishes the requeued Task. What it could not prove is the sentence above,
because its export node appended a task id to a list. There was nothing else it
could do -- until this project had an export node, the only thing "exported"
could mean was "a function ran".

So this file runs the real one: the real ``export_artifact`` tool, through the
real ``ToolGateway``, against the real ``tool_executions`` ledger and a real
artifact store. "Only one artifact" is then a count of files, and "only once" is
a count of rows, rather than a counter the test itself maintains.

The agent nodes are deterministic stand-ins. That is the point rather than a
compromise: what is under test is the side-effect protocol across a process
boundary, and a provider in the middle would add a variable and a bill without
adding evidence.

Real PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.artifacts.local import METADATA_SUFFIX
from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.langgraph import (
    LangGraphTaskWorkflow,
    PostgresCheckpointSaver,
    build_approval_node,
)
from agent_workbench.adapters.langgraph.workflow import build_v1_graph
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.persistence import (
    PostgresApprovalStore,
    PostgresEventLog,
    PostgresTaskRegistry,
    PostgresToolExecutionLedger,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import tool_executions
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.tools import ExportArtifactTool, StaticToolRegistry
from agent_workbench.adapters.tools.task_export import GatewayReportExport
from agent_workbench.bootstrap.projections import TASK_V1_AUTHORIZATION_ENVELOPE
from agent_workbench.domain.policies import ExecutionContext, PrincipalContext
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.task_registry import TaskRun, TaskSubmission
from agent_workbench.runtime import ToolGateway
from agent_workbench.workers.task import TaskWorker
from agent_workbench.workflows.approval import APPROVAL_OPERATION_ID, TaskApprovalGate

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = (
    "approvals, task_runs, events, event_streams, workflow_checkpoints, "
    "workflow_checkpoint_blobs, workflow_checkpoint_writes, tool_executions"
)

VERSIONS = ("v1",)
BUILDERS = {"v1": build_v1_graph}

TENANT = "tenant_a"
OWNER = "user_1"
DRAFT_BODY = b"Reciprocal rank fusion runs inside the database, not the app."
#: The scope ``export_artifact`` declares. Without it the policy engine refuses
#: the call, which is a different test and one the tool's own suite covers.
SCOPES = ("artifact:export",)


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _engine() -> Any:
    return create_query_engine(_dsn(), application_name="agent-workbench-tests")


def _run(scenario: Callable[[Path], Awaitable[Any]], root: Path) -> Any:
    _dsn()

    async def execute() -> Any:
        engine = _engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        finally:
            await engine.dispose()
        return await scenario(root)

    return asyncio.run(execute())


def _submission() -> TaskSubmission:
    return TaskSubmission.model_validate(
        {
            "tenant_id": TENANT,
            "owner_id": OWNER,
            "thread_id": "thr_1",
            "graph_version": "v1",
            "input_ref": "input_1",
            "input_fingerprint": hashlib.sha256(b"input_1").hexdigest(),
            "submission_dedup_key": "dedup_1",
            "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
            "run_semantics_revision": "1.2:v1.3:abc0123456789def",
            "submitted_policy_revision": "policy-1",
            "submitted_policy_fingerprint": "f" * 16,
            # The envelope the API submits every v1 Task with. Using the real
            # one is what makes this test able to fail when that ceiling moves.
            "submitted_authorization_envelope": (
                TASK_V1_AUTHORIZATION_ENVELOPE.model_dump(mode="json")
            ),
            "submitted_principal_scopes": SCOPES,
        }
    )


async def _draft(store: LocalArtifactStore) -> str:
    reference = await store.put(
        tenant_id=TENANT,
        owner_id=OWNER,
        kind="report",
        media_type="text/markdown",
        content=DRAFT_BODY,
    )
    return reference.artifact_id


def _state_loader(draft_ref: str) -> Callable[[TaskRun], Awaitable[TaskState]]:
    """A Task already carrying a reviewed, approvable draft.

    The nodes before the gate are not what this demonstration is about, and
    running them would put a model in the middle of a test about a ledger.
    """

    async def load(task: TaskRun) -> TaskState:
        return TaskState.model_validate(
            {
                "task_id": task.task_id,
                "objective": "Compare retrieval strategies.",
                "plan": (
                    TaskStep(
                        step_id="step_1",
                        sequence=1,
                        objective="Gather internal notes.",
                    ),
                ),
                "draft_ref": draft_ref,
                "review_result": ReviewResult(
                    decision="pass",
                    reviewed_draft_ref=draft_ref,
                    revision_number=0,
                    summary="Grounded and complete.",
                    score=93,
                ),
            }
        )

    return load


def _export_context(task_id: str, lease_epoch: int) -> ExecutionContext:
    return ExecutionContext(
        principal=PrincipalContext(tenant_id=TENANT, principal_id=OWNER, scopes=SCOPES),
        envelope=TASK_V1_AUTHORIZATION_ENVELOPE,
        agent_run_id="run_export",
        policy_identity="policy-1:ffffffffffffffff",
        task_id=task_id,
        lease_epoch=lease_epoch,
    )


def _handlers(engine: Any, store: LocalArtifactStore, draft_ref: str) -> dict[str, Any]:
    """Deterministic nodes, a real interrupting gate, and a real export."""

    ledger = PostgresToolExecutionLedger(engine)
    registry = StaticToolRegistry([ExportArtifactTool(artifacts=store).binding()])
    exporter = GatewayReportExport(
        gateway=ToolGateway(
            registry=registry,
            policy=EnvelopePolicyEngine(registry=registry),
            ledger=ledger,
        ),
        ledger=ledger,
    )

    async def passthrough(state: TaskState) -> dict[str, Any]:
        del state
        return {}

    async def export(state: TaskState) -> dict[str, Any]:
        assert state.approval_id is not None
        task = await PostgresTaskRegistry(engine).get(state.task_id)
        assert task is not None
        artifact_id = await exporter.export(
            draft_ref=draft_ref,
            approval_id=state.approval_id,
            # The live epoch, read from the Registry at the moment of the
            # write. A Worker that lost the Task cannot get past the fence.
            execution=_export_context(state.task_id, task.lease_epoch),
            sink=ScopedEventSink(
                InMemoryEventLog(),
                EventScope(stream_id=task.thread_id, run_id="run_export"),
            ),
            cancellation=NullCancellationToken(),
        )
        return {"export_ref": artifact_id}

    return {
        "understand": passthrough,
        "research_internal": passthrough,
        "research_external": passthrough,
        "synthesize": passthrough,
        "critic": passthrough,
        "export": export,
        "approval": build_approval_node(
            TaskApprovalGate(
                approvals=PostgresApprovalStore(engine),
                registry=PostgresTaskRegistry(engine),
            )
        ),
    }


def _worker(
    engine: Any, store: LocalArtifactStore, draft_ref: str, *, approvals: Any = None
) -> TaskWorker:
    """A whole Worker on one engine. Discarding it is a process ending."""

    return TaskWorker(
        registry=PostgresTaskRegistry(engine, events=PostgresEventLog(engine)),
        approvals=approvals,
        workflow=LangGraphTaskWorkflow(
            handlers=_handlers(engine, store, draft_ref),
            checkpointer=PostgresCheckpointSaver(engine),
            builders=BUILDERS,
        ),
        load_state=_state_loader(draft_ref),
        buildable_versions=VERSIONS,
    )


async def _artifacts(root: Path) -> list[str]:
    """Every artifact this store holds, counted on disk.

    On disk rather than through the store's own API: the claim is that one
    object exists, and asking the thing that wrote it how many it wrote is a
    weaker question than looking.

    Counted by metadata file. The store writes bytes and a sidecar per
    artifact, so counting files counts everything twice -- which passes an
    "exactly one" assertion for the wrong reason exactly as often as it fails
    one.
    """

    return sorted(path.stem for path in root.rglob(f"*{METADATA_SUFFIX}"))


async def _ledger_rows(engine: Any) -> list[Any]:
    async with engine.connect() as connection:
        result = await connection.execute(
            select(
                tool_executions.c.operation_key,
                tool_executions.c.status,
                tool_executions.c.outcome_detail,
            )
        )
        return list(result.all())


# --------------------------------------------------------------------------
# The demonstration
# --------------------------------------------------------------------------


def test_a_paused_task_survives_its_worker_and_exports_exactly_once(
    tmp_path: Path,
) -> None:
    """The third fixed demonstration, in one run.

    Pause on a human. Discard the process that paused -- its engine, its
    checkpointer, its workflow, its handler closures. Approve. Let a Worker
    that never saw the first one finish the job. One report exists at the end,
    and the ledger says so once.
    """

    async def scenario(root: Path) -> Any:
        store = LocalArtifactStore(root)
        draft_ref = await _draft(store)

        # --- the first process ---------------------------------------------
        first = _engine()
        try:
            task = await PostgresTaskRegistry(
                first, events=PostgresEventLog(first)
            ).submit(_submission())
            paused = await _worker(
                first,
                store,
                draft_ref,
                approvals=PostgresApprovalStore(first),
            ).run_once()
            assert paused is not None
            parked = await PostgresTaskRegistry(first).get(task.task_id)
            assert parked is not None
            approval_id = (
                await PostgresApprovalStore(first).request(
                    task_id=task.task_id,
                    graph_node_operation_id=APPROVAL_OPERATION_ID,
                    tenant_id=TENANT,
                    owner_id=OWNER,
                )
            ).approval_id
        finally:
            # The process ends here. Nothing below reuses anything above.
            await first.dispose()

        during_pause = await _artifacts(root)

        # --- a human, on a connection of their own ---------------------------
        deciding = _engine()
        try:
            await PostgresApprovalStore(
                deciding, events=PostgresEventLog(deciding)
            ).decide(
                approval_id,
                decision="approved",
                decision_version=1,
                decided_by=OWNER,
            )
        finally:
            await deciding.dispose()

        # --- a second process, which never saw the first ---------------------
        second = _engine()
        try:
            resumed = await _worker(
                second,
                store,
                draft_ref,
                approvals=PostgresApprovalStore(second),
            ).run_once()
            assert resumed is not None
            settled = await PostgresTaskRegistry(second).get(task.task_id)
            rows = await _ledger_rows(second)
        finally:
            await second.dispose()

        return {
            "parked": parked.status,
            "during_pause": during_pause,
            "settled": settled,
            "rows": rows,
            "files": await _artifacts(root),
            "draft_ref": draft_ref,
            "task_id": task.task_id,
        }

    result = _run(scenario, tmp_path)

    # It stopped for a person, and nothing was exported while it waited.
    assert result["parked"] == "waiting_approval"
    assert len(result["during_pause"]) == 1, "only the draft exists before approval"

    # A different process carried it to a terminal state.
    settled = result["settled"]
    assert settled is not None
    assert settled.status == "succeeded"

    # Exactly one report, and the ledger accounts for it exactly once.
    assert len(result["files"]) == 2, "the draft, and one report"
    assert len(result["rows"]) == 1
    operation_key, status, detail = result["rows"][0]
    assert operation_key == f"export:{result['task_id']}"
    assert status == "succeeded"
    # The row names what it made, which is what a resume reads to avoid
    # exporting a second time.
    assert detail is not None and detail != result["draft_ref"]


def test_the_report_is_the_approved_draft(tmp_path: Path) -> None:
    """A demonstration that produced *an* artifact would pass without this.

    The export has to be of the draft the human approved, not of something the
    export node assembled on its own.
    """

    async def scenario(root: Path) -> Any:
        store = LocalArtifactStore(root)
        draft_ref = await _draft(store)

        engine = _engine()
        try:
            task = await PostgresTaskRegistry(
                engine, events=PostgresEventLog(engine)
            ).submit(_submission())
            await _worker(
                engine, store, draft_ref, approvals=PostgresApprovalStore(engine)
            ).run_once()
            approval_id = (
                await PostgresApprovalStore(engine).request(
                    task_id=task.task_id,
                    graph_node_operation_id=APPROVAL_OPERATION_ID,
                    tenant_id=TENANT,
                    owner_id=OWNER,
                )
            ).approval_id
            await PostgresApprovalStore(engine, events=PostgresEventLog(engine)).decide(
                approval_id,
                decision="approved",
                decision_version=1,
                decided_by=OWNER,
            )
            await _worker(
                engine, store, draft_ref, approvals=PostgresApprovalStore(engine)
            ).run_once()
            rows = await _ledger_rows(engine)
        finally:
            await engine.dispose()

        exported = rows[0][2]
        return await store.get(
            tenant_id=TENANT, artifact_id=exported, principal_id=OWNER
        ), approval_id

    body, approval_id = _run(scenario, tmp_path)

    assert DRAFT_BODY in body
    # The header names the approval that authorised it, so a report found on
    # disk is traceable without the database.
    assert approval_id.encode() in body


def test_a_rejected_approval_exports_nothing(tmp_path: Path) -> None:
    """The other half, and the one where a missing assertion is expensive.

    A rejected decision must leave the artifact store exactly as it was. The
    ledger must hold no row either: nothing was intended, so nothing should be
    recorded as having been.
    """

    async def scenario(root: Path) -> Any:
        store = LocalArtifactStore(root)
        draft_ref = await _draft(store)

        engine = _engine()
        try:
            task = await PostgresTaskRegistry(
                engine, events=PostgresEventLog(engine)
            ).submit(_submission())
            await _worker(
                engine, store, draft_ref, approvals=PostgresApprovalStore(engine)
            ).run_once()
            approval_id = (
                await PostgresApprovalStore(engine).request(
                    task_id=task.task_id,
                    graph_node_operation_id=APPROVAL_OPERATION_ID,
                    tenant_id=TENANT,
                    owner_id=OWNER,
                )
            ).approval_id
            await PostgresApprovalStore(engine, events=PostgresEventLog(engine)).decide(
                approval_id,
                decision="rejected",
                decision_version=1,
                decided_by=OWNER,
            )
            await _worker(
                engine, store, draft_ref, approvals=PostgresApprovalStore(engine)
            ).run_once()
            settled = await PostgresTaskRegistry(engine).get(task.task_id)
            async with engine.connect() as connection:
                recorded = await connection.execute(
                    select(func.count()).select_from(tool_executions)
                )
                rows = recorded.scalar_one()
        finally:
            await engine.dispose()

        return settled, rows, await _artifacts(root)

    settled, rows, files = _run(scenario, tmp_path)

    assert settled is not None
    assert settled.status == "failed"
    assert settled.status_detail is not None
    assert "rejected" in settled.status_detail
    assert rows == 0, "nothing was intended, so nothing is recorded"
    assert len(files) == 1, "only the draft"
