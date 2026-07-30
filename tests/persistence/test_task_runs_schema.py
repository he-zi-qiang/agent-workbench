"""The Task Registry's row, and the invariants the database enforces on it.

The Registry is the product lifecycle. Its two hard rules are that resubmitting
one key does not start a second Task, and that a Task never records a state a
human has to act on without saying why. Both are constraints rather than
repository conventions, because a repository is one writer and a constraint
holds against all of them.

The status vocabulary exists twice -- as a Python ``Literal`` and as a check
constraint -- so a test asserts the two agree instead of trusting whoever adds
the ninth status to remember both places.

Real PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.adapters.persistence.models import task_runs
from agent_workbench.domain.task_registry import (
    TERMINAL_STATUSES,
    TaskStatus,
)

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

ALL_STATUSES: tuple[TaskStatus, ...] = get_args(TaskStatus)

#: The states whose meaning is "somebody has to look at this", plus the ones
#: that failed. Recording any of them without a reason is a dead end for
#: whoever reads the row next.
EXPLAINED_STATUSES = frozenset(
    {"waiting_migration", "failed", "cancelled", "dead_letter"}
)


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(scenario: Callable[[AsyncEngine], Awaitable[Any]]) -> Any:
    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text("TRUNCATE task_runs"))
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": "task_1",
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
        "thread_id": "thr_1",
        "graph_version": "v1",
        "input_ref": "input_1",
        "input_fingerprint": hashlib.sha256(b"input_1").hexdigest(),
        "submission_dedup_key": "dedup_1",
        "status": "queued",
        "status_detail": None,
        "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
        "run_semantics_revision": "1.2:v1.3:abc0123456789def",
        "submitted_policy_revision": "policy-1",
        "submitted_policy_fingerprint": "f" * 16,
        "submitted_authorization_envelope": {},
        "submitted_principal_scopes": [],
    }
    base.update(overrides)
    # A running Task is never an unowned state.  Keep raw-schema tests honest
    # as the Registry grows the same lease invariant it relies on at runtime.
    if base["status"] == "running":
        base.update(
            lease_owner="worker_schema",
            lease_epoch=1,
            lease_until=datetime.now(UTC) + timedelta(minutes=5),
            heartbeat_at=datetime.now(UTC),
        )
    return base


# --------------------------------------------------------------------------
# The two vocabularies have to be one vocabulary


def test_the_database_accepts_exactly_the_statuses_the_domain_defines() -> None:
    """A status in one place and not the other is a row nobody can write."""

    async def scenario(engine: AsyncEngine) -> list[str]:
        accepted: list[str] = []
        for index, status in enumerate(ALL_STATUSES):
            row = _row(
                task_id=f"task_{index}",
                thread_id=f"thr_{index}",
                submission_dedup_key=f"dedup_{index}",
                status=status,
                status_detail="because" if status in EXPLAINED_STATUSES else None,
            )
            async with engine.begin() as connection:
                await connection.execute(insert(task_runs), [row])
            accepted.append(status)
        return accepted

    assert set(_run(scenario)) == set(ALL_STATUSES)


def test_a_status_the_domain_does_not_define_is_refused() -> None:
    """The database is the guard, not the repository's good manners.

    Two constraints refuse this row, and that overlap is deliberate rather
    than accidental. The lifecycle constraint pairs each status with whether
    it carries a reason, so an unknown status fits neither of its arms and is
    rejected by it as well -- dropping the vocabulary constraint on its own
    changes nothing observable, which a sabotage of exactly that shape
    confirmed. It stays because the day the lifecycle rule is relaxed is the
    day the vocabulary would otherwise stop being enforced, silently.
    """

    async def scenario(engine: AsyncEngine) -> tuple[bool, bool]:
        refused: list[bool] = []
        # Both shapes of the same unknown status: with a reason and without,
        # so neither arm of the lifecycle rule is the only thing being tested.
        for detail in (None, "because"):
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        insert(task_runs),
                        [_row(status="in_progress", status_detail=detail)],
                    )
                refused.append(False)
            except IntegrityError:
                refused.append(True)
        return refused[0], refused[1]

    assert _run(scenario) == (True, True)


# --------------------------------------------------------------------------
# Submitting twice


def test_one_tenant_owner_cannot_start_two_tasks_from_one_submission_key() -> None:
    """The exit condition "a repeated submission key returns the same Task".

    Returning the same Task is the repository's job; making a second one
    impossible is this constraint's.
    """

    async def scenario(engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            await connection.execute(insert(task_runs), [_row()])
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    insert(task_runs),
                    [_row(task_id="task_2", thread_id="thr_2")],
                )

    _run(scenario)


def test_two_owners_may_reuse_one_submission_key() -> None:
    """The key is the caller's, so it is scoped to the caller.

    A global unique key would let one owner's choice of key deny another's
    submission, and would tell them it had.
    """

    async def scenario(engine: AsyncEngine) -> int:
        async with engine.begin() as connection:
            await connection.execute(
                insert(task_runs),
                [
                    _row(),
                    _row(
                        task_id="task_2",
                        thread_id="thr_2",
                        owner_id="user_2",
                    ),
                ],
            )
        async with engine.connect() as connection:
            return len((await connection.execute(select(task_runs))).all())

    assert _run(scenario) == 2


def test_the_same_owner_and_key_may_recur_in_another_tenant() -> None:
    """Owner ids are not global identities; the tenant scopes a caller."""

    async def scenario(engine: AsyncEngine) -> int:
        async with engine.begin() as connection:
            await connection.execute(
                insert(task_runs),
                [
                    _row(),
                    _row(
                        task_id="task_2",
                        thread_id="thr_2",
                        tenant_id="tenant_b",
                    ),
                ],
            )
        async with engine.connect() as connection:
            return len((await connection.execute(select(task_runs))).all())

    assert _run(scenario) == 2


def test_a_thread_backs_exactly_one_task() -> None:
    """Two Registry rows over one checkpoint would make reconciliation ambiguous."""

    async def scenario(engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            await connection.execute(insert(task_runs), [_row()])
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    insert(task_runs),
                    [_row(task_id="task_2", submission_dedup_key="dedup_2")],
                )

    _run(scenario)


# --------------------------------------------------------------------------
# A state a human must act on always says why


@pytest.mark.parametrize("status", sorted(EXPLAINED_STATUSES))
def test_a_state_that_needs_a_human_cannot_be_recorded_without_a_reason(
    status: str,
) -> None:
    async def scenario(engine: AsyncEngine) -> None:
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    insert(task_runs), [_row(status=status, status_detail=None)]
                )

    _run(scenario)


@pytest.mark.parametrize("status", sorted(set(ALL_STATUSES) - EXPLAINED_STATUSES))
def test_a_state_nobody_has_to_act_on_carries_no_reason(status: str) -> None:
    """The constraint runs both ways: a reason on a running Task is stale text.

    Left writable, it would survive the transition that made it wrong and be
    read as the current explanation of a Task that is fine.
    """

    async def scenario(engine: AsyncEngine) -> None:
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    insert(task_runs),
                    [_row(status=status, status_detail="stale explanation")],
                )

    _run(scenario)


def test_a_transition_into_a_terminal_state_must_bring_its_reason_along() -> None:
    """The constraint holds on update, not only on insert."""

    async def scenario(engine: AsyncEngine) -> str:
        async with engine.begin() as connection:
            await connection.execute(insert(task_runs), [_row(status="running")])
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    update(task_runs)
                    .where(task_runs.c.task_id == "task_1")
                    .values(status="failed")
                )
        async with engine.begin() as connection:
            await connection.execute(
                update(task_runs)
                .where(task_runs.c.task_id == "task_1")
                .values(
                    status="failed",
                    status_detail="the model call died",
                    lease_owner=None,
                    lease_until=None,
                    heartbeat_at=None,
                )
            )
        async with engine.connect() as connection:
            return (
                await connection.execute(
                    select(task_runs.c.status_detail).where(
                        task_runs.c.task_id == "task_1"
                    )
                )
            ).scalar_one()

    assert _run(scenario) == "the model call died"


def test_leaving_a_terminal_state_would_have_to_drop_its_reason() -> None:
    """Not a guard against revival -- that is the repository's conditional
    update -- but the row cannot be half-moved: a Task cannot end up running
    while still carrying why it failed."""

    async def scenario(engine: AsyncEngine) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                insert(task_runs),
                [_row(status="failed", status_detail="the model call died")],
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    update(task_runs)
                    .where(task_runs.c.task_id == "task_1")
                    .values(
                        status="running",
                        lease_owner="worker_schema",
                        lease_epoch=1,
                        lease_until=datetime.now(UTC) + timedelta(minutes=5),
                        heartbeat_at=datetime.now(UTC),
                    )
                )

    _run(scenario)


# --------------------------------------------------------------------------
# What the Worker's pick order reads


def test_queued_tasks_come_back_oldest_first() -> None:
    """A single Worker's pick order, and the index that serves it.

    Priority and the SKIP LOCKED claim arrive with multiple Workers (WP08);
    until then "oldest queued" is the whole of the ordering, so it is the thing
    that has to be true.
    """

    async def scenario(engine: AsyncEngine) -> list[str]:
        async with engine.begin() as connection:
            for index in range(4):
                await connection.execute(
                    insert(task_runs),
                    [
                        _row(
                            task_id=f"task_{index}",
                            thread_id=f"thr_{index}",
                            submission_dedup_key=f"dedup_{index}",
                            status="queued" if index % 2 == 0 else "succeeded",
                        )
                    ],
                )
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(task_runs.c.task_id)
                    .where(task_runs.c.status == "queued")
                    .order_by(task_runs.c.created_at, task_runs.c.task_id)
                )
            ).all()
        return [row.task_id for row in rows]

    assert _run(scenario) == ["task_0", "task_2"]


def test_the_terminal_set_is_the_one_the_registry_will_not_reopen() -> None:
    """A cross-check on the domain constant the reconciliation branches on."""

    assert set(ALL_STATUSES) >= TERMINAL_STATUSES
    assert {"succeeded", "failed", "cancelled", "dead_letter"} == TERMINAL_STATUSES


# --------------------------------------------------------------------------
# A reservation is a triple or it is nothing


@pytest.mark.parametrize(
    "partial",
    [
        {"resolved_qdrant_collection": "kb_v3"},
        {"resolved_qdrant_index_version": "3"},
        {"resolved_qdrant_index_generation_id": "6f1d5a02-0000-4000-8000-000000000001"},
        {"resolved_qdrant_collection": "kb_v3", "resolved_qdrant_index_version": "3"},
    ],
)
def test_a_partial_index_reservation_cannot_be_stored(
    partial: dict[str, Any],
) -> None:
    """Two of the three would describe an index nothing can look up.

    A resume reads all three together -- collection, version and the generation
    the reservation is held against. Any subset is a Task that appears bound to
    a corpus while carrying no way to find it, which is worse than one that is
    plainly unbound.
    """

    async def scenario(engine: AsyncEngine) -> None:
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(insert(task_runs), [_row(**partial)])

    _run(scenario)
