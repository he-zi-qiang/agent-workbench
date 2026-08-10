"""A Task that stops for a human, and a *different* Worker that finishes it.

This is the whole HITL loop over real PostgreSQL: the graph interrupts, the
Worker releases its lease and its lock and exits, the decision lands through the
ledger's transaction, and a Worker built from a fresh engine -- one that never
saw the first run -- claims the requeued Task and carries it to a terminal
status. Nothing is held in memory between the two halves, which is the only way
to tell "recovered from durable state" from "the objects were still there".

Both answers are exercised, because they are different paths and only one of
them is allowed to export.

Real PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select, text

from agent_workbench.adapters.langgraph import (
    LangGraphTaskWorkflow,
    PostgresCheckpointSaver,
    build_approval_node,
)
from agent_workbench.adapters.langgraph.workflow import GRAPH_DEFINITIONS
from agent_workbench.adapters.persistence import (
    PostgresApprovalStore,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import approvals as approvals_table
from agent_workbench.adapters.persistence.models import events as events_table
from agent_workbench.adapters.persistence.models import (
    workflow_checkpoint_writes as writes_table,
)
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep
from agent_workbench.ports.approvals import ApprovalNotDecidableError
from agent_workbench.ports.task_registry import TaskRun, TaskSubmission
from agent_workbench.workers.task import TaskWorker
from agent_workbench.workflows.approval import APPROVAL_OPERATION_ID, TaskApprovalGate

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = (
    "approvals, task_runs, events, event_streams, workflow_checkpoints, "
    "workflow_checkpoint_blobs, workflow_checkpoint_writes"
)

VERSIONS = ("v1",)
GRAPHS = {"v1": GRAPH_DEFINITIONS["v1"]}

#: Every export this test file has ever performed, across every Worker. It is a
#: module-level list rather than a per-Worker one on purpose: the point of a
#: rejected approval is that *no* process exports, and a counter owned by one
#: Worker could not see a second one doing it.
EXPORTS: list[str] = []


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _engine() -> Any:
    return create_query_engine(_dsn(), application_name="agent-workbench-tests")


def _run(scenario: Callable[[], Awaitable[Any]]) -> Any:
    _dsn()
    EXPORTS.clear()

    async def execute() -> Any:
        engine = _engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        finally:
            await engine.dispose()
        return await scenario()

    return asyncio.run(execute())


def _submission(**overrides: Any) -> TaskSubmission:
    base: dict[str, Any] = {
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
        "thread_id": "thr_1",
        "graph_version": "v1",
        "input_ref": "input_1",
        "input_fingerprint": hashlib.sha256(b"input_1").hexdigest(),
        "submission_dedup_key": "dedup_1",
        "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
        "run_semantics_revision": "1.2:v1.3:abc0123456789def",
        "submitted_policy_revision": "policy-1",
        "submitted_policy_fingerprint": "f" * 16,
        "submitted_authorization_envelope": {},
    }
    base.update(overrides)
    return TaskSubmission.model_validate(base)


async def _load_state(task: TaskRun) -> TaskState:
    return TaskState.model_validate(
        {
            "task_id": task.task_id,
            "objective": "Compare retrieval strategies.",
            "plan": (
                TaskStep(
                    step_id="step_1", sequence=1, objective="Gather internal notes."
                ),
            ),
        }
    )


def _handlers(engine: Any) -> dict[str, Any]:
    """The v1 graph with the real interrupting approval node."""

    async def understand(state: TaskState) -> dict[str, Any]:
        return {"agent_outcome_refs": ("run_understand",)}

    async def internal(state: TaskState) -> dict[str, Any]:
        return {"evidence_refs": ("ev_internal",)}

    async def external(state: TaskState) -> dict[str, Any]:
        return {"evidence_refs": ("ev_external",)}

    async def synthesize(state: TaskState) -> dict[str, Any]:
        return {"draft_ref": "draft_1", "review_result": None}

    async def critic(state: TaskState) -> dict[str, Any]:
        return {
            "review_result": ReviewResult(
                decision="pass",
                reviewed_draft_ref="draft_1",
                revision_number=state.revision_count,
                summary="Grounded in the evidence.",
                score=90,
            ).model_dump()
        }

    async def export(state: TaskState) -> dict[str, Any]:
        EXPORTS.append(state.task_id)
        return {}

    return {
        "understand": understand,
        "research_internal": internal,
        "research_external": external,
        "synthesize": synthesize,
        "critic": critic,
        "export": export,
        "approval": build_approval_node(
            TaskApprovalGate(
                approvals=PostgresApprovalStore(engine),
                registry=PostgresTaskRegistry(engine),
            )
        ),
    }


def _worker(engine: Any, *, approvals: Any | None = None) -> TaskWorker:
    """A whole Worker on one engine. Discarding it is a process ending."""

    return TaskWorker(
        registry=PostgresTaskRegistry(engine),
        approvals=approvals,
        workflow=LangGraphTaskWorkflow(
            handlers=_handlers(engine),
            checkpointer=PostgresCheckpointSaver(engine),
            graphs=GRAPHS,
        ),
        load_state=_load_state,
        buildable_versions=VERSIONS,
    )


async def _stop_at_the_approval() -> tuple[str, Any]:
    """Run one Worker until the Task parks on a human, then let it go."""

    engine = _engine()
    try:
        task = await PostgresTaskRegistry(engine).submit(_submission())
        outcome = await _worker(
            engine, approvals=PostgresApprovalStore(engine)
        ).run_once()
        assert outcome is not None
        return task.task_id, [decision.action for decision in outcome.decisions]
    finally:
        await engine.dispose()


async def _approval_id(engine: Any, task_id: str) -> str:
    """The one approval the graph opened, read the way a client would find it."""

    async with engine.connect() as connection:
        found = (
            await connection.execute(
                select(approvals_table.c.approval_id).where(
                    approvals_table.c.task_id == task_id
                )
            )
        ).scalar_one()
    return str(found)


# --------------------------------------------------------------------------
# Stopping
# --------------------------------------------------------------------------


def test_an_interrupted_task_parks_on_the_human_and_releases_its_lease() -> None:
    """Nothing is executing while a person thinks, and nothing was exported."""

    async def scenario() -> tuple[Any, ...]:
        task_id, actions = await _stop_at_the_approval()
        engine = _engine()
        try:
            stored = await PostgresTaskRegistry(engine).get(task_id)
            assert stored is not None
            async with engine.connect() as connection:
                pending = (
                    (
                        await connection.execute(
                            select(approvals_table).where(
                                approvals_table.c.task_id == task_id
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
        finally:
            await engine.dispose()
        return (
            actions,
            stored.status,
            stored.lease_owner,
            stored.lease_until,
            [(row["status"], row["graph_node_operation_id"]) for row in pending],
            list(EXPORTS),
        )

    actions, status, owner, until, pending, exports = _run(scenario)

    assert actions == ["start", "wait_for_approval"]
    assert status == "waiting_approval"
    # Released, both of them: an approval is not something to hold a lease over.
    assert owner is None
    assert until is None
    assert pending == [("pending", APPROVAL_OPERATION_ID)]
    assert exports == []


def test_a_worker_with_no_ledger_parks_rather_than_resuming_blindly() -> None:
    """The control group for "the ledger decides".

    A Worker that cannot ask whether a human answered must not guess. It parks
    the Task again -- a standstill, and the safe one -- instead of resuming a
    graph whose gate nobody has passed.
    """

    async def scenario() -> tuple[Any, ...]:
        task_id, _ = await _stop_at_the_approval()
        engine = _engine()
        try:
            approvals = PostgresApprovalStore(engine)
            approval_id = await _approval_id(engine, task_id)
            await approvals.decide(
                approval_id,
                decision="approved",
                decision_version=1,
                decided_by="reviewer_1",
            )
            # Decided, and requeued -- but this Worker has no ledger to read it.
            outcome = await _worker(engine, approvals=None).run_once()
            assert outcome is not None
            return (
                [decision.action for decision in outcome.decisions],
                outcome.final_status,
                list(EXPORTS),
            )
        finally:
            await engine.dispose()

    actions, status, exports = _run(scenario)

    assert actions == ["wait_for_approval"]
    assert status == "waiting_approval"
    assert exports == []


# --------------------------------------------------------------------------
# Waking
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "expected_status", "expected_exports"),
    [
        ("approved", "succeeded", 1),
        # Same Task, same graph, same second Worker. One different answer, and
        # the export must not have run.
        ("rejected", "failed", 0),
    ],
)
def test_a_second_worker_finishes_the_task_the_decision_requeued(
    decision: str, expected_status: str, expected_exports: int
) -> None:
    async def scenario() -> tuple[Any, ...]:
        task_id, _ = await _stop_at_the_approval()

        # A fresh engine for the decision, and another for the second Worker.
        # Nothing from the first run survives except what PostgreSQL holds.
        deciding = _engine()
        try:
            approval_id = await _approval_id(deciding, task_id)
            await PostgresApprovalStore(deciding).decide(
                approval_id,
                decision=decision,
                decision_version=1,
                decided_by="reviewer_1",
            )
            requeued = await PostgresTaskRegistry(deciding).get(task_id)
            assert requeued is not None
        finally:
            await deciding.dispose()

        second = _engine()
        try:
            outcome = await _worker(
                second, approvals=PostgresApprovalStore(second)
            ).run_once()
            assert outcome is not None
            final = await PostgresTaskRegistry(second).get(task_id)
            assert final is not None
        finally:
            await second.dispose()

        return (
            requeued.status,
            requeued.resume_kind,
            requeued.resume_approval_id == approval_id,
            [step.action for step in outcome.decisions],
            final.status,
            final.status_detail,
            len(EXPORTS),
        )

    (
        requeued_status,
        resume_kind,
        names_the_approval,
        actions,
        status,
        detail,
        exports,
    ) = _run(scenario)

    assert (requeued_status, resume_kind, names_the_approval) == (
        "queued",
        "approval",
        True,
    )
    assert actions == ["resume_with_approval", f"settle_{expected_status}"]
    assert status == expected_status
    assert exports == expected_exports
    # A rejection is a recorded reason, not an empty pending list read as success.
    assert (detail is not None) is (decision == "rejected")


def test_a_late_approval_cannot_export_after_the_task_was_cancelled() -> None:
    """Somebody stopped the work while a human was thinking about it.

    The decision transaction refuses, so nothing is requeued and no Worker ever
    reaches the export -- which is the property that matters, because the export
    is the Task's one externally visible write.
    """

    async def scenario() -> tuple[Any, ...]:
        task_id, _ = await _stop_at_the_approval()
        engine = _engine()
        try:
            registry = PostgresTaskRegistry(engine)
            await registry.cancel(task_id, reason="the owner asked")
            approval_id = await _approval_id(engine, task_id)
            with pytest.raises(ApprovalNotDecidableError):
                await PostgresApprovalStore(engine).decide(
                    approval_id,
                    decision="approved",
                    decision_version=1,
                    decided_by="reviewer_1",
                )
            # Nothing to claim; the Worker finds no queued Task at all.
            outcome = await _worker(
                engine, approvals=PostgresApprovalStore(engine)
            ).run_once()
            stored = await registry.get(task_id)
            assert stored is not None
            return stored.status, outcome, list(EXPORTS)
        finally:
            await engine.dispose()

    status, outcome, exports = _run(scenario)

    assert status == "cancelled"
    assert outcome is None
    assert exports == []


def test_the_checkpoint_records_which_decision_version_woke_the_thread() -> None:
    """The wake-up is durable, even though the node does not read it.

    LangGraph persists a resume value in the thread's ``__resume__`` writes, so
    the checkpoint answers "which decision restarted this graph" without anyone
    consulting the ledger. That is the only observable the version has -- the
    node deliberately ignores the payload -- and without this assertion the
    version could be any number at all and every outcome would be identical.
    """

    async def scenario() -> tuple[Any, ...]:
        task_id, _ = await _stop_at_the_approval()
        engine = _engine()
        try:
            approval_id = await _approval_id(engine, task_id)
            await PostgresApprovalStore(engine).decide(
                approval_id,
                decision="rejected",
                decision_version=4,
                decided_by="reviewer_1",
            )
            await _worker(engine, approvals=PostgresApprovalStore(engine)).run_once()
            saver = PostgresCheckpointSaver(engine)
            async with engine.connect() as connection:
                rows = (
                    (
                        await connection.execute(
                            select(writes_table).where(
                                writes_table.c.channel == "__resume__"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            resumed = [
                saver.serde.loads_typed((row["payload_type"], bytes(row["payload"])))
                for row in rows
            ]
        finally:
            await engine.dispose()
        return approval_id, resumed

    approval_id, resumed = _run(scenario)

    assert resumed, "the resume value must be persisted with the thread"
    # Stored either bare or wrapped in the task's list of resumes, depending on
    # which write it is; both must name the same decision.
    flattened = [
        item
        for value in resumed
        for item in (value if isinstance(value, list) else [value])
    ]
    assert flattened
    for value in flattened:
        assert value == {"approval_id": approval_id, "decision_version": 4}


def test_the_decision_and_the_resume_are_both_on_the_task_timeline() -> None:
    """A reader of the Task's own stream can see why it moved."""

    async def scenario() -> list[str]:
        task_id, _ = await _stop_at_the_approval()
        engine = _engine()
        try:
            approval_id = await _approval_id(engine, task_id)
            await PostgresApprovalStore(engine).decide(
                approval_id,
                decision="approved",
                decision_version=1,
                decided_by="reviewer_1",
            )
            await _worker(engine, approvals=PostgresApprovalStore(engine)).run_once()
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        select(events_table.c.event_type).order_by(
                            events_table.c.sequence
                        )
                    )
                ).all()
        finally:
            await engine.dispose()
        return [row.event_type for row in rows]

    kinds = _run(scenario)

    assert "TaskAwaitingApproval" in kinds
    assert "TaskApprovalDecided" in kinds
    assert kinds.index("TaskAwaitingApproval") < kinds.index("TaskApprovalDecided")
    assert kinds[-1] == "TaskSucceeded"
