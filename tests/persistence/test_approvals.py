"""Deciding an approval, and the two races that decide who wins.

The decision is one transaction that records the answer, requeues the Task,
names the approval as the reason, and writes the durable event. Either all four
land or none do, and a decision arriving for a Task that is no longer waiting is
refused -- because the Task may have been cancelled while a human was thinking,
and reopening it would resurrect work somebody stopped.

Real PostgreSQL only: what is under test is which transaction loses.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from agent_workbench.adapters.persistence import (
    PostgresApprovalStore,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import approvals, events
from agent_workbench.ports.approvals import (
    ApprovalNotDecidableError,
    ApprovalStore,
)
from agent_workbench.ports.task_registry import TaskSubmission

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(
    scenario: Callable[
        [Any, PostgresTaskRegistry, PostgresApprovalStore], Awaitable[Any]
    ],
) -> Any:
    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE approvals, task_runs, events, event_streams, "
                        "qdrant_index_generations CASCADE"
                    )
                )
            return await scenario(
                engine, PostgresTaskRegistry(engine), PostgresApprovalStore(engine)
            )
        finally:
            await engine.dispose()

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
        "submitted_principal_scopes": [],
    }
    base.update(overrides)
    return TaskSubmission.model_validate(base)


async def _waiting(registry: PostgresTaskRegistry, store: PostgresApprovalStore) -> Any:
    """A Task paused at an approval, which is the only state a decision applies to."""

    task = await registry.submit(_submission())
    claim = await registry.claim_next("worker_1", lease_seconds=60)
    assert claim is not None
    await registry.await_approval(claim.lease)
    approval = await store.request(
        task_id=task.task_id,
        graph_node_operation_id="approval:export",
        tenant_id="tenant_a",
        owner_id="user_1",
    )
    return task, approval


# --------------------------------------------------------------------------


def test_the_store_satisfies_the_framework_neutral_port() -> None:
    dsn = _dsn()
    engine = create_query_engine(dsn, application_name="agent-workbench-tests")
    try:
        assert isinstance(PostgresApprovalStore(engine), ApprovalStore)
    finally:
        asyncio.run(engine.dispose())


def test_asking_twice_for_one_operation_opens_one_approval() -> None:
    """A node re-entered after a crash asks the same question, not a second one."""

    async def scenario(engine: Any, registry: Any, store: Any) -> tuple[str, str, int]:
        task, first = await _waiting(registry, store)
        second = await store.request(
            task_id=task.task_id,
            graph_node_operation_id="approval:export",
            tenant_id="tenant_a",
            owner_id="user_1",
        )
        async with engine.connect() as connection:
            rows = len((await connection.execute(select(approvals))).all())
        return first.approval_id, second.approval_id, rows

    first, second, rows = _run(scenario)

    assert first == second
    assert rows == 1


def test_a_new_approval_starts_pending_with_nobody_attached() -> None:
    async def scenario(engine: Any, registry: Any, store: Any) -> tuple[Any, ...]:
        _, approval = await _waiting(registry, store)
        return approval.status, approval.decision_version, approval.decided_by

    assert _run(scenario) == ("pending", 0, None)


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_either_decision_requeues_the_task_and_names_the_approval(
    decision: str,
) -> None:
    """A rejection is a path through the graph, not the absence of one.

    Both outcomes requeue, and the resume reference is what lets the Worker tell
    a decision from an ordinary retry without inspecting the graph.
    """

    async def scenario(engine: Any, registry: Any, store: Any) -> tuple[Any, ...]:
        task, approval = await _waiting(registry, store)
        decided = await store.decide(
            approval.approval_id,
            decision=decision,
            decision_version=1,
            decided_by="reviewer_1",
        )
        requeued = await registry.get(task.task_id)
        assert requeued is not None
        return (
            decided.status,
            decided.decision_version,
            decided.decided_by,
            requeued.status,
            requeued.resume_kind,
            requeued.resume_approval_id == approval.approval_id,
        )

    assert _run(scenario) == (decision, 1, "reviewer_1", "queued", "approval", True)


def test_a_decision_is_recorded_on_the_task_timeline() -> None:
    async def scenario(engine: Any, registry: Any, store: Any) -> list[str]:
        _, approval = await _waiting(registry, store)
        await store.decide(
            approval.approval_id,
            decision="approved",
            decision_version=1,
            decided_by="reviewer_1",
        )
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(events.c.event_type).order_by(events.c.sequence)
                )
            ).all()
        return [row.event_type for row in rows]

    kinds = _run(scenario)

    assert kinds[0] == "TaskSubmitted"
    assert "TaskApprovalDecided" in kinds


def test_replaying_the_same_decision_requeues_once() -> None:
    """A double-clicked button is one decision, and leaves one event."""

    async def scenario(engine: Any, registry: Any, store: Any) -> tuple[int, int]:
        _, approval = await _waiting(registry, store)
        for _ in range(3):
            await store.decide(
                approval.approval_id,
                decision="approved",
                decision_version=1,
                decided_by="reviewer_1",
            )
        async with engine.connect() as connection:
            decided = len((await connection.execute(select(approvals))).all())
            timeline = len(
                (
                    await connection.execute(
                        select(events).where(
                            events.c.event_type == "TaskApprovalDecided"
                        )
                    )
                ).all()
            )
        return decided, timeline

    assert _run(scenario) == (1, 1)


def test_a_decision_for_a_task_that_is_not_waiting_is_refused() -> None:
    """The Task row decides, not arrival time."""

    async def scenario(engine: Any, registry: Any, store: Any) -> tuple[Any, ...]:
        task = await registry.submit(_submission())
        approval = await store.request(
            task_id=task.task_id,
            graph_node_operation_id="approval:export",
            tenant_id="tenant_a",
            owner_id="user_1",
        )
        # Still queued: nothing paused at an approval.
        with pytest.raises(ApprovalNotDecidableError) as captured:
            await store.decide(
                approval.approval_id,
                decision="approved",
                decision_version=1,
                decided_by="reviewer_1",
            )
        stored = await registry.get(task.task_id)
        assert stored is not None
        return captured.value.task_status, stored.status, stored.resume_kind

    assert _run(scenario) == ("queued", "queued", None)


def test_a_late_approval_cannot_reopen_a_cancelled_task() -> None:
    """Somebody stopped this work while a human was thinking about it."""

    async def scenario(engine: Any, registry: Any, store: Any) -> tuple[Any, ...]:
        task, approval = await _waiting(registry, store)
        await registry.cancel(task.task_id, reason="the owner asked")
        with pytest.raises(ApprovalNotDecidableError) as captured:
            await store.decide(
                approval.approval_id,
                decision="approved",
                decision_version=1,
                decided_by="reviewer_1",
            )
        stored = await registry.get(task.task_id)
        assert stored is not None
        return captured.value.task_status, stored.status

    assert _run(scenario) == ("cancelled", "cancelled")


def test_a_cancel_committing_first_wins_the_barrier() -> None:
    """Cancel and approve racing: exactly one legal transition.

    The cancellation is held uncommitted so the decision blocks on the Task row
    -- the fixed lock order means they serialise rather than deadlock -- and then
    finds a terminal Task and refuses. The Task must not end up queued.
    """

    async def scenario(engine: Any, registry: Any, store: Any) -> tuple[str, str, Any]:
        task, approval = await _waiting(registry, store)

        holder = await engine.connect()
        transaction = await holder.begin()
        await holder.execute(
            text(
                "UPDATE task_runs SET status = 'cancelled', "
                "status_detail = 'the owner asked', lease_owner = NULL, "
                "lease_until = NULL, heartbeat_at = NULL WHERE task_id = :t"
            ),
            {"t": task.task_id},
        )
        deciding = asyncio.create_task(_decide_or_error(store, approval.approval_id))
        await asyncio.sleep(0.3)
        await transaction.commit()
        await holder.close()
        outcome = await deciding

        stored = await registry.get(task.task_id)
        assert stored is not None
        return type(outcome).__name__, stored.status, stored.resume_kind

    error, status, resume = _run(scenario)

    assert error == "ApprovalNotDecidableError"
    assert status == "cancelled"
    assert resume is None


def test_an_approval_committing_first_makes_the_cancel_lose() -> None:
    """The other direction: once requeued, the Task is no longer cancellable
    from ``waiting_approval``, so a cancel that arrives after must go through
    the queued path rather than silently undoing the decision."""

    async def scenario(engine: Any, registry: Any, store: Any) -> tuple[Any, ...]:
        task, approval = await _waiting(registry, store)
        await store.decide(
            approval.approval_id,
            decision="approved",
            decision_version=1,
            decided_by="reviewer_1",
        )
        # Cancelling is still legal from `queued`, but the decision it followed
        # is recorded and stays recorded.
        await registry.cancel(task.task_id, reason="changed my mind")
        stored = await registry.get(task.task_id)
        decided = await store.get(approval.approval_id)
        assert stored is not None and decided is not None
        return stored.status, stored.resume_kind, decided.status

    assert _run(scenario) == ("cancelled", "approval", "approved")


def test_a_stale_decision_version_cannot_overwrite_a_newer_one() -> None:
    """Two reviewers deciding together: the newer version stands."""

    async def scenario(engine: Any, registry: Any, store: Any) -> tuple[Any, ...]:
        _, approval = await _waiting(registry, store)
        await store.decide(
            approval.approval_id,
            decision="rejected",
            decision_version=2,
            decided_by="reviewer_2",
        )
        replayed = await store.decide(
            approval.approval_id,
            decision="approved",
            decision_version=1,
            decided_by="reviewer_1",
        )
        return replayed.status, replayed.decision_version, replayed.decided_by

    assert _run(scenario) == ("rejected", 2, "reviewer_2")


def test_a_decision_version_below_one_is_refused() -> None:
    async def scenario(engine: Any, registry: Any, store: Any) -> None:
        _, approval = await _waiting(registry, store)
        with pytest.raises(ValueError):
            await store.decide(
                approval.approval_id,
                decision="approved",
                decision_version=0,
                decided_by="reviewer_1",
            )

    _run(scenario)


def test_deciding_an_approval_that_does_not_exist_says_so() -> None:
    async def scenario(engine: Any, registry: Any, store: Any) -> Any:
        with pytest.raises(ApprovalNotDecidableError) as captured:
            await store.decide(
                "approval_absent",
                decision="approved",
                decision_version=1,
                decided_by="reviewer_1",
            )
        return captured.value.approval_status

    assert _run(scenario) is None


@pytest.mark.parametrize(
    "row",
    [
        {"status": "approved", "decision_version": 1},
        {"status": "approved", "decision_version": 1, "decided_by": "reviewer_1"},
        {"status": "approved", "decision_version": 0, "decided_by": "reviewer_1"},
        {"status": "pending", "decision_version": 1},
    ],
)
def test_a_decision_cannot_be_recorded_without_who_and_when(
    row: dict[str, Any],
) -> None:
    """A decided approval names its decider and its version, or it is not one.

    Enforced by the database rather than by this adapter, because an audit trail
    that depends on one writer remembering is not an audit trail. The reverse
    also holds: a pending approval carrying a decider would be a decision
    nobody made.
    """

    async def scenario(engine: Any, registry: Any, store: Any) -> None:
        task, _ = await _waiting(registry, store)
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    approvals.insert().values(
                        approval_id="approval_direct",
                        task_id=task.task_id,
                        graph_node_operation_id="approval:other",
                        tenant_id="tenant_a",
                        owner_id="user_1",
                        **row,
                    )
                )

    _run(scenario)


async def _decide_or_error(store: Any, approval_id: str) -> object:
    try:
        return await store.decide(
            approval_id,
            decision="approved",
            decision_version=1,
            decided_by="reviewer_1",
        )
    except ApprovalNotDecidableError as error:
        return error
