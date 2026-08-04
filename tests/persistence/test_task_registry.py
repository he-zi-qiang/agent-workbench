"""The Task Registry repository: idempotent submission, and legal moves only.

Two things are being established. Submitting the same key twice returns the
same Task -- and submitting it with a *different* request does not, because
answering that with the first Task would be answering a question nobody asked.
And every status change is a conditional update, so a move that is not legal
from where the Task actually is fails loudly rather than writing anyway.

The transition table lives in the domain and the SQL is derived from it, so the
tests here check the behaviour rather than the table; the table has its own
tests where it is defined.

Real PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from agent_workbench.adapters.persistence import (
    PostgresEventLog,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import (
    events,
    qdrant_index_generations,
    task_runs,
)
from agent_workbench.domain.task_registry import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    sources_for,
)
from agent_workbench.ports.task_registry import (
    ExecutionLease,
    IndexGenerationNotReservableError,
    IndexReservation,
    StaleExecutionError,
    TaskRegistry,
    TaskSubmission,
    TaskSubmissionConflictError,
    TaskTransitionRejectedError,
)

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(scenario: Callable[[PostgresTaskRegistry], Awaitable[Any]]) -> Any:
    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE task_runs, events, event_streams, "
                        "qdrant_index_generations CASCADE"
                    )
                )
            return await scenario(PostgresTaskRegistry(engine))
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _run_with_engine(
    scenario: Callable[[Any, PostgresTaskRegistry], Awaitable[Any]],
) -> Any:
    """For the races, which need a second connection the registry does not own."""

    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE task_runs, events, event_streams, "
                        "qdrant_index_generations CASCADE"
                    )
                )
            return await scenario(engine, PostgresTaskRegistry(engine))
        finally:
            await engine.dispose()

    return asyncio.run(execute())


async def _while_uncommitted(
    engine: Any, statement: Any, racer: Callable[[], Awaitable[Any]]
) -> Any:
    """Run ``racer`` against a row another transaction has written but not committed.

    This is what makes the two races below deterministic rather than hopeful.
    ``racer`` blocks on the row or the index, the conflicting transaction then
    commits, and the interleaving under test is the only one that can happen --
    an ``asyncio.gather`` of the same calls interleaves differently on every
    machine and, measurably, usually not at all.
    """

    holder = await engine.connect()
    transaction = await holder.begin()
    await holder.execute(statement)
    running = asyncio.create_task(racer())
    # Long enough for the racer to reach the lock. It cannot proceed past it
    # until the commit below, so a slow machine waits longer rather than
    # testing something else.
    await asyncio.sleep(0.3)
    await transaction.commit()
    await holder.close()
    return await running


def _submission(**overrides: Any) -> TaskSubmission:
    base: dict[str, Any] = {
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
        "thread_id": "thr_1",
        "graph_version": "v1",
        "input_ref": "input_1",
        "submission_dedup_key": "dedup_1",
        "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
        "run_semantics_revision": "1.2:v1.3:abc0123456789def",
        "submitted_policy_revision": "policy-1",
        "submitted_policy_fingerprint": "f" * 16,
        "submitted_authorization_envelope": {},
        "submitted_principal_scopes": [],
    }
    base.update(overrides)
    base.setdefault(
        "input_fingerprint",
        hashlib.sha256(str(base["input_ref"]).encode("utf-8")).hexdigest(),
    )
    return TaskSubmission.model_validate(base)


# --------------------------------------------------------------------------
# Lease claim, heartbeat and stale recovery (E1)


async def _claim(registry: PostgresTaskRegistry, worker_id: str = "worker_1") -> Any:
    claim = await registry.claim_next(worker_id, lease_seconds=60)
    assert claim is not None
    return claim


def test_two_workers_claiming_concurrently_receive_one_task_only() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, Any]:
        await registry.submit(_submission())
        return await asyncio.gather(
            registry.claim_next("worker_1", lease_seconds=60),
            registry.claim_next("worker_2", lease_seconds=60),
        )

    first, second = _run(scenario)

    claims = [claim for claim in (first, second) if claim is not None]
    assert len(claims) == 1
    assert claims[0].task.status == "running"
    assert claims[0].lease.epoch == 1


def test_the_oldest_eligible_task_is_claimed_first() -> None:
    """Among Tasks a Worker may take, the one waiting longest goes first.

    Only the part both the baseline and the code agree on is pinned here:
    ordering by creation among *eligible* rows. The code additionally leads with
    `available_at`, and the baseline's own claim query leads with `priority` --
    neither is asserted, because nothing states which of the two is intended,
    and a test would be inventing that answer rather than checking it.
    """

    async def scenario(registry: PostgresTaskRegistry) -> list[str]:
        opened = []
        for index in range(3):
            opened.append(
                await registry.submit(
                    _submission(
                        thread_id=f"thr_o{index}",
                        submission_dedup_key=f"dedup_o{index}",
                    )
                )
            )
        claimed = []
        for index in range(3):
            claim = await _claim(registry, f"worker_o{index}")
            claimed.append(claim.task.task_id)
        return claimed + [task.task_id for task in opened]

    result = _run(scenario)

    assert result[:3] == result[3:]


def test_a_task_still_inside_its_backoff_is_not_claimed() -> None:
    """The retry delay is enforced by the claim, not only written by the reaper.

    A backoff nothing reads is a backoff that does not exist: the reaper would
    set `available_at` and the next claim would take the Task anyway.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, Any]:
        task = await registry.submit(_submission())
        async with registry._engine.begin() as connection:
            await connection.execute(
                update(task_runs)
                .where(task_runs.c.task_id == task.task_id)
                .values(available_at=func.now() + text("interval '1 hour'"))
            )
        deferred = await registry.claim_next("worker_early", lease_seconds=60)
        async with registry._engine.begin() as connection:
            await connection.execute(
                update(task_runs)
                .where(task_runs.c.task_id == task.task_id)
                .values(available_at=func.now() - text("interval '1 second'"))
            )
        due = await registry.claim_next("worker_due", lease_seconds=60)
        return deferred, due

    deferred, due = _run(scenario)

    assert deferred is None
    assert due is not None


def test_an_old_epoch_cannot_heartbeat_or_settle_after_reclaim() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> tuple[int, int]:
        task = await registry.submit(_submission())
        first = await _claim(registry, "worker_old")
        async with registry._engine.begin() as connection:
            await connection.execute(
                update(task_runs)
                .where(task_runs.c.task_id == task.task_id)
                .values(lease_until=func.now() - text("interval '1 second'"))
            )
        await registry.reclaim_expired(
            limit=1,
            max_attempts=3,
            retry_base_seconds=1,
            retry_max_seconds=1,
        )
        async with registry._engine.begin() as connection:
            await connection.execute(
                update(task_runs)
                .where(task_runs.c.task_id == task.task_id)
                .values(available_at=func.now() - text("interval '1 second'"))
            )
        second = await _claim(registry, "worker_new")
        with pytest.raises(StaleExecutionError):
            await registry.heartbeat(first.lease, lease_seconds=60)
        with pytest.raises(StaleExecutionError):
            await registry.mark_succeeded(first.lease)
        return first.lease.epoch, second.lease.epoch

    assert _run(scenario) == (1, 2)


async def _expire(registry: PostgresTaskRegistry, task_id: str) -> None:
    async with registry._engine.begin() as connection:
        await connection.execute(
            update(task_runs)
            .where(task_runs.c.task_id == task_id)
            .values(lease_until=func.now() - text("interval '1 second'"))
        )


async def _reclaim(
    registry: PostgresTaskRegistry,
    *,
    limit: int = 10,
    max_attempts: int = 5,
    retry_base_seconds: int = 4,
    retry_max_seconds: int = 600,
) -> tuple[Any, ...]:
    return await registry.reclaim_expired(
        limit=limit,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
    )


def test_a_reaper_leaves_a_live_lease_alone() -> None:
    """The reaper recovers abandoned work; it does not take work in progress.

    Nothing else distinguishes a running Worker from a dead one at this layer --
    both rows say `running` and name an owner. Only the deadline does. A reaper
    that ignored it would hand a live Worker's Task to somebody else and run it
    twice, which is the failure the whole lease design exists to prevent.

    The deadline is checked twice, in the sweep's select and again in its
    update, so removing either one alone changes nothing. This test is what
    fails when *both* go, which is the property worth holding.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[int, str, Any]:
        task = await registry.submit(_submission())
        claim = await _claim(registry, "worker_live")
        # No expiry: the lease has 60 seconds left.
        reclaimed = await _reclaim(registry)
        stored = await registry.get(task.task_id)
        assert stored is not None
        # The live Worker can still act, which is the point.
        beat = await registry.heartbeat(claim.lease, lease_seconds=60)
        return len(reclaimed), stored.status, beat.lease_owner

    count, status, owner = _run(scenario)

    assert count == 0
    assert status == "running"
    assert owner == "worker_live"


def test_a_reaper_does_not_reopen_a_task_that_already_finished() -> None:
    """Terminal is terminal, whatever a stale deadline says.

    A settled Task drops its lease, so `lease_until` is null rather than
    expired, and `NULL < now()` is not true -- which is why the sweep's
    `status = 'running'` condition cannot be made to fail on its own: the
    lease-lifecycle constraint means no non-running row can carry the expired
    deadline it would need. The invariant is real and is what this asserts; the
    condition guarding it is redundant until that constraint changes.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[int, str]:
        task = await registry.submit(_submission())
        claim = await _claim(registry)
        await registry.mark_succeeded(claim.lease)
        # Backdate every timestamp the reaper could key on. The row is
        # terminal, and that alone must keep it out of the sweep.
        async with registry._engine.begin() as connection:
            await connection.execute(
                update(task_runs)
                .where(task_runs.c.task_id == task.task_id)
                .values(
                    updated_at=func.now() - text("interval '1 hour'"),
                    available_at=func.now() - text("interval '1 hour'"),
                )
            )
        reclaimed = await _reclaim(registry)
        stored = await registry.get(task.task_id)
        assert stored is not None
        return len(reclaimed), stored.status

    assert _run(scenario) == (0, "succeeded")


def test_a_requeued_task_is_not_claimable_until_its_backoff_elapses() -> None:
    """Without a delay a poison task is claimed, fails and is claimed again.

    The attempt budget bounds how many times that happens; the backoff bounds
    how fast. Dropping it turns a failing Task into a hot loop against whatever
    it was failing to call.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[str, Any, bool]:
        task = await registry.submit(_submission())
        await _claim(registry)
        await _expire(registry, task.task_id)
        reclaimed = await _reclaim(registry, retry_base_seconds=30)
        assert len(reclaimed) == 1
        # Queued, but not yet available: a claim now finds nothing.
        immediate = await registry.claim_next("worker_next", lease_seconds=60)
        stored = await registry.get(task.task_id)
        assert stored is not None
        return stored.status, immediate, stored.available_at > stored.updated_at

    status, immediate, deferred = _run(scenario)

    assert status == "queued"
    assert immediate is None
    assert deferred


def test_the_backoff_grows_with_each_attempt() -> None:
    """Exponential, not fixed: the second wait is longer than the first.

    Measured as the gap between attempts rather than against a wall clock, so
    a slow machine reads the same as a fast one.
    """

    async def scenario(registry: PostgresTaskRegistry) -> list[float]:
        task = await registry.submit(_submission())
        delays: list[float] = []
        for _ in range(3):
            async with registry._engine.begin() as connection:
                await connection.execute(
                    update(task_runs)
                    .where(task_runs.c.task_id == task.task_id)
                    .values(available_at=func.now() - text("interval '1 hour'"))
                )
            await _claim(registry)
            await _expire(registry, task.task_id)
            reclaimed = await _reclaim(registry, retry_base_seconds=10, max_attempts=9)
            assert len(reclaimed) == 1
            stored = reclaimed[0]
            delays.append((stored.available_at - stored.updated_at).total_seconds())
        return delays

    delays = _run(scenario)

    # Three reclaims at attempts 1, 2, 3 with a 10s base: 10, 20, 40.
    assert delays[0] < delays[1] < delays[2]
    assert delays[1] >= delays[0] * 1.5


def test_the_backoff_is_capped_rather_than_doubling_forever() -> None:
    """An unbounded exponential eventually parks a Task past any horizon."""

    async def scenario(registry: PostgresTaskRegistry) -> float:
        task = await registry.submit(_submission())
        for _ in range(4):
            async with registry._engine.begin() as connection:
                await connection.execute(
                    update(task_runs)
                    .where(task_runs.c.task_id == task.task_id)
                    .values(available_at=func.now() - text("interval '1 hour'"))
                )
            await _claim(registry)
            await _expire(registry, task.task_id)
            reclaimed = await _reclaim(
                registry, retry_base_seconds=10, retry_max_seconds=25, max_attempts=9
            )
            assert len(reclaimed) == 1
        return (reclaimed[0].available_at - reclaimed[0].updated_at).total_seconds()

    # Attempt 4 would want 80s; the cap is 25.
    assert _run(scenario) <= 26


def test_two_reapers_sweeping_together_each_recover_different_tasks() -> None:
    """`SKIP LOCKED` is why a second reaper is useful rather than blocked.

    Without it the second sweep waits on the first's row locks, so two reapers
    are strictly slower than one -- and a reaper that blocks is one that stops
    being run.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[int, int, int]:
        tasks = []
        for index in range(4):
            task = await registry.submit(
                _submission(
                    thread_id=f"thr_r{index}",
                    submission_dedup_key=f"dedup_r{index}",
                )
            )
            await _claim(registry, f"worker_{index}")
            await _expire(registry, task.task_id)
            tasks.append(task)
        first, second = await asyncio.gather(
            _reclaim(registry, limit=2, retry_base_seconds=1),
            _reclaim(registry, limit=2, retry_base_seconds=1),
        )
        recovered = {task.task_id for task in (*first, *second)}
        return len(first), len(second), len(recovered)

    first, second, distinct = _run(scenario)

    # Every expired Task recovered exactly once between the two sweeps.
    assert first + second == 4
    assert distinct == 4


def test_a_reclaim_racing_a_fresh_claim_does_not_undo_it() -> None:
    """The update re-checks what the select saw, so a stale sweep loses.

    A reaper selects an expired row, and before its update lands the Task is
    claimed again -- new epoch, new deadline. Without the epoch and expiry
    conditions on the update, the sweep would strip a live Worker's lease and
    requeue the Task underneath it.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> tuple[Any, ...]:
        task = await registry.submit(_submission())
        await _claim(registry, "worker_old")
        await _expire(registry, task.task_id)

        # Hold the row as a competing reaper would, so the real reclaim blocks
        # on it; meanwhile the row is claimed afresh and committed.
        holder = await engine.connect()
        transaction = await holder.begin()
        await holder.execute(
            select(task_runs.c.task_id)
            .where(task_runs.c.task_id == task.task_id)
            .with_for_update()
        )
        sweeping = asyncio.create_task(_reclaim(registry, retry_base_seconds=1))
        await asyncio.sleep(0.3)
        await holder.execute(
            update(task_runs)
            .where(task_runs.c.task_id == task.task_id)
            .values(
                lease_owner="worker_new",
                lease_epoch=task_runs.c.lease_epoch + 1,
                lease_until=func.now() + text("interval '60 seconds'"),
                heartbeat_at=func.now(),
            )
        )
        await transaction.commit()
        await holder.close()
        reclaimed = await sweeping

        stored = await registry.get(task.task_id)
        assert stored is not None
        return len(reclaimed), stored.status, stored.lease_owner

    count, status, owner = _run_with_engine(scenario)

    # The sweep recovered nothing: what it had selected was no longer expired.
    assert count == 0
    assert status == "running"
    assert owner == "worker_new"


def test_an_expired_claim_at_its_attempt_budget_is_dead_lettered() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> tuple[str, str | None]:
        task = await registry.submit(_submission())
        await _claim(registry)
        async with registry._engine.begin() as connection:
            await connection.execute(
                update(task_runs)
                .where(task_runs.c.task_id == task.task_id)
                .values(lease_until=func.now() - text("interval '1 second'"))
            )
        reclaimed = await registry.reclaim_expired(
            limit=1,
            max_attempts=1,
            retry_base_seconds=1,
            retry_max_seconds=1,
        )
        assert len(reclaimed) == 1
        return reclaimed[0].status, reclaimed[0].status_detail

    status, detail = _run(scenario)

    assert status == "dead_letter"
    assert detail == "lease expired after 1 attempts"


def test_reclaim_records_retry_and_dead_letter_on_the_task_timeline() -> None:
    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> list[str]:
        task = await registry.submit(_submission())
        first = await _claim(registry)
        async with engine.begin() as connection:
            await connection.execute(
                update(task_runs)
                .where(task_runs.c.task_id == task.task_id)
                .values(lease_until=func.now() - text("interval '1 second'"))
            )
        await registry.reclaim_expired(
            limit=1,
            max_attempts=2,
            retry_base_seconds=1,
            retry_max_seconds=1,
        )
        async with engine.begin() as connection:
            await connection.execute(
                update(task_runs)
                .where(task_runs.c.task_id == task.task_id)
                .values(available_at=func.now() - text("interval '1 second'"))
            )
        second = await _claim(registry, "worker_2")
        assert second.lease.epoch == first.lease.epoch + 1
        async with engine.begin() as connection:
            await connection.execute(
                update(task_runs)
                .where(task_runs.c.task_id == task.task_id)
                .values(lease_until=func.now() - text("interval '1 second'"))
            )
        await registry.reclaim_expired(
            limit=1,
            max_attempts=2,
            retry_base_seconds=1,
            retry_max_seconds=1,
        )
        async with engine.connect() as connection:
            return list(
                (
                    await connection.execute(
                        select(events.c.event_type)
                        .where(events.c.stream_id == task.thread_id)
                        .order_by(events.c.sequence)
                    )
                ).scalars()
            )

    assert _run_with_engine(scenario) == [
        "TaskSubmitted",
        "TaskClaimed",
        "TaskRetryScheduled",
        "TaskClaimed",
        "TaskDeadLettered",
    ]


def test_cancelling_a_claim_clears_its_lease_and_rejects_late_settlement() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, Any, Any]:
        task = await registry.submit(_submission())
        claim = await _claim(registry)
        cancelled = await registry.cancel(task.task_id, reason="owner cancelled")
        with pytest.raises(TaskTransitionRejectedError):
            await registry.mark_succeeded(claim.lease)
        return cancelled.status, cancelled.lease_owner, cancelled.lease_until

    assert _run(scenario) == ("cancelled", None, None)


def test_lifecycle_transitions_append_a_safe_ordered_task_timeline() -> None:
    async def scenario(
        engine: Any, registry: PostgresTaskRegistry
    ) -> tuple[list[str], dict[str, Any]]:
        task = await registry.submit(_submission())
        first = await _claim(registry)
        await registry.release_for_retry(first.lease, delay_seconds=0)
        second = await _claim(registry, "worker_2")
        await registry.mark_failed(second.lease, reason="provider body must not leak")
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(events.c.event_type, events.c.payload)
                        .where(events.c.stream_id == task.thread_id)
                        .order_by(events.c.sequence)
                    )
                )
                .mappings()
                .all()
            )
        return [row["event_type"] for row in rows], rows[-1]["payload"]

    kinds, failed_payload = _run_with_engine(scenario)

    assert kinds == [
        "TaskSubmitted",
        "TaskClaimed",
        "TaskRetryScheduled",
        "TaskClaimed",
        "TaskFailed",
    ]
    assert failed_payload["kind"] == "TaskFailed"
    assert failed_payload["epoch"] == 2
    assert failed_payload["attempt"] == 2
    assert failed_payload["status"] == "failed"
    assert failed_payload["reason_code"] == "execution_failed"
    assert "provider body must not leak" not in str(failed_payload)


def test_a_heartbeat_is_not_a_high_frequency_timeline_event() -> None:
    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> int:
        await registry.submit(_submission())
        claim = await _claim(registry)
        await registry.heartbeat(claim.lease, lease_seconds=60)
        async with engine.connect() as connection:
            return len((await connection.execute(select(events))).all())

    # The Task opening and claim are durable facts; lease maintenance is not.
    assert _run_with_engine(scenario) == 2


def test_a_refused_claim_event_rolls_back_the_claim_with_it() -> None:
    class _RefusingClaimLog(PostgresEventLog):
        async def append_durable_in_transaction(self, *args: Any, **kwargs: Any) -> Any:
            payload = args[2]
            if payload.kind == "TaskClaimed":
                raise RuntimeError("claim event injection")
            return await super().append_durable_in_transaction(*args, **kwargs)

    async def scenario(engine: Any, _: PostgresTaskRegistry) -> tuple[str, int]:
        registry = PostgresTaskRegistry(engine, events=_RefusingClaimLog(engine))
        task = await registry.submit(_submission())
        with pytest.raises(RuntimeError, match="injection"):
            await registry.claim_next("worker_1", lease_seconds=60)
        stored = await registry.get(task.task_id)
        assert stored is not None
        async with engine.connect() as connection:
            count = len((await connection.execute(select(events))).all())
        return stored.status, count

    assert _run_with_engine(scenario) == ("queued", 1)


def test_a_live_claim_can_be_released_for_a_fenced_delayed_retry() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> tuple[str, Any, Any, Any]:
        await registry.submit(_submission())
        claim = await _claim(registry)
        released = await registry.release_for_retry(claim.lease, delay_seconds=30)
        with pytest.raises(TaskTransitionRejectedError):
            await registry.mark_succeeded(claim.lease)
        return (
            released.status,
            released.lease_owner,
            released.lease_until,
            released.available_at,
        )

    status, owner, until, available_at = _run(scenario)

    assert status == "queued"
    assert owner is None
    assert until is None
    assert available_at is not None


# --------------------------------------------------------------------------
# Submission


def test_the_adapter_satisfies_the_framework_neutral_port() -> None:
    dsn = _dsn()
    engine = create_query_engine(dsn, application_name="agent-workbench-tests")
    try:
        assert isinstance(PostgresTaskRegistry(engine), TaskRegistry)
    finally:
        asyncio.run(engine.dispose())


def test_a_repeated_submission_key_returns_the_same_task() -> None:
    """The exit condition, from the caller's side rather than the constraint's."""

    async def scenario(registry: PostgresTaskRegistry) -> tuple[str, str, int]:
        first = await registry.submit(_submission())
        # All three values are server-owned decisions, and therefore change
        # on a retry after a process restart. They cannot be used to reject a
        # caller repeating the same request.
        second = await registry.submit(
            _submission(
                thread_id="thr_retry",
                run_semantics_revision="1.2:v1.3:0000000000000000",
                submitted_policy_fingerprint="0" * 16,
            )
        )
        third = await registry.submit(_submission(thread_id="thr_retry_2"))
        # Compared by value, not by identity: a Task now carries its
        # submitted semantics, and a dict is not hashable.
        return first.task_id, second.task_id, len({first == second, second == third})

    first_id, second_id, distinct = _run(scenario)

    assert first_id == second_id
    # Not just the same id: the same Task, field for field.
    assert distinct == 1


def test_equal_input_content_retries_ignore_their_generated_artifact_ids() -> None:
    """Only the first artifact reference becomes the Task's source of truth."""

    async def scenario(registry: PostgresTaskRegistry) -> tuple[str, str, str]:
        fingerprint = "a" * 64
        first = await registry.submit(
            _submission(input_ref="art_first", input_fingerprint=fingerprint)
        )
        retry = await registry.submit(
            _submission(
                thread_id="thr_retry",
                input_ref="art_retry_orphan",
                input_fingerprint=fingerprint,
            )
        )
        return first.task_id, retry.task_id, retry.input_ref

    first_id, retry_id, input_ref = _run(scenario)

    assert first_id == retry_id
    assert input_ref == "art_first"


@pytest.mark.parametrize(
    "changes",
    [
        {"input_ref": "input_2"},
        {"graph_version": "v2"},
    ],
    ids=["input", "graph"],
)
def test_a_repeated_key_with_a_different_request_is_a_conflict(
    changes: dict[str, str],
) -> None:
    """Idempotency answers the same question again; it does not answer a new one."""

    async def scenario(registry: PostgresTaskRegistry) -> None:
        await registry.submit(_submission())
        with pytest.raises(TaskSubmissionConflictError) as captured:
            await registry.submit(_submission(**changes))
        assert captured.value.submission_dedup_key == "dedup_1"

    _run(scenario)


def test_two_owners_may_use_the_same_submission_key() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> int:
        first = await registry.submit(_submission())
        second = await registry.submit(
            _submission(owner_id="user_2", thread_id="thr_2")
        )
        return len({first.task_id, second.task_id})

    assert _run(scenario) == 2


def test_the_same_owner_and_key_are_independent_between_tenants() -> None:
    """Principal identifiers are tenant-local, so deduplication must be too."""

    async def scenario(registry: PostgresTaskRegistry) -> tuple[str, str]:
        first = await registry.submit(_submission())
        second = await registry.submit(
            _submission(tenant_id="tenant_b", thread_id="thr_tenant_b")
        )
        return first.task_id, second.task_id

    first, second = _run(scenario)

    assert first != second


def test_a_submitted_task_starts_queued_and_carries_no_explanation() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> tuple[str, str | None]:
        task = await registry.submit(_submission())
        return task.status, task.status_detail

    assert _run(scenario) == ("queued", None)


def test_a_submission_that_loses_the_race_returns_the_winner_s_task() -> None:
    """Insert-or-nothing, not read-then-insert.

    Reading first and inserting when absent loses this race with a duplicate
    key error rather than with idempotency, and the loser is a caller who did
    nothing wrong. The conflicting row is written and held uncommitted so the
    losing branch is the only one this can take: the submit blocks on the
    unique index, and by the time it looks the winner is there.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> tuple[str, int]:
        winner = {
            "task_id": "task_winner",
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
        returned = await _while_uncommitted(
            engine,
            task_runs.insert().values(winner),
            lambda: registry.submit(
                _submission(
                    thread_id="thr_loser",
                    input_ref="input_1_retry_artifact",
                    input_fingerprint=hashlib.sha256(b"input_1").hexdigest(),
                    run_semantics_revision="1.2:v1.3:0000000000000000",
                )
            ),
        )
        async with engine.connect() as connection:
            rows = len((await connection.execute(select(task_runs))).all())
        return returned.task_id, rows

    task_id, rows = _run_with_engine(scenario)

    assert task_id == "task_winner"
    assert rows == 1


def test_a_race_loser_records_the_submission_event_on_the_winner_s_thread() -> None:
    """A retry has a new candidate thread, but the stored Task owns its stream."""

    async def scenario(
        engine: Any, registry: PostgresTaskRegistry
    ) -> tuple[str, str, str]:
        winner = {
            "task_id": "task_winner",
            "tenant_id": "tenant_a",
            "owner_id": "user_1",
            "thread_id": "thr_winner",
            "graph_version": "v1",
            "input_ref": "input_winner",
            "input_fingerprint": hashlib.sha256(b"input_winner").hexdigest(),
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
        returned = await _while_uncommitted(
            engine,
            task_runs.insert().values(winner),
            lambda: registry.submit(
                _submission(
                    thread_id="thr_retry",
                    input_ref="input_winner",
                    run_semantics_revision="1.2:v1.3:0000000000000000",
                )
            ),
        )
        async with engine.connect() as connection:
            event = (
                (await connection.execute(select(events.c.stream_id, events.c.payload)))
                .mappings()
                .one()
            )
        return returned.thread_id, event["stream_id"], event["payload"]["input_ref"]

    task_thread, event_thread, input_ref = _run_with_engine(scenario)

    assert (task_thread, event_thread, input_ref) == (
        "thr_winner",
        "thr_winner",
        "input_winner",
    )


# --------------------------------------------------------------------------
# Handing out work


def test_the_oldest_queued_task_is_the_one_that_starts() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> list[str | None]:
        opened = []
        for index in range(3):
            opened.append(
                await registry.submit(
                    _submission(
                        thread_id=f"thr_{index}",
                        submission_dedup_key=f"dedup_{index}",
                    )
                )
            )
        claims = [
            await registry.claim_next(f"worker_{index}", lease_seconds=60)
            for index in range(4)
        ]
        return [claim.task.task_id if claim else None for claim in claims] + [
            task.task_id for task in opened
        ]

    result = _run(scenario)
    started, opened = result[:4], result[4:]

    assert started[:3] == opened
    # And nothing left to hand out.
    assert started[3] is None


def test_starting_a_task_takes_it_out_of_the_queue() -> None:
    """Running once means running once: the same Task is not handed out twice."""

    async def scenario(registry: PostgresTaskRegistry) -> tuple[str, Any, str]:
        task = await registry.submit(_submission())
        started = await registry.claim_next("worker_1", lease_seconds=60)
        again = await registry.claim_next("worker_2", lease_seconds=60)
        stored = await registry.get(task.task_id)
        assert stored is not None
        return started.task.task_id if started else "", again, stored.status

    started_id, again, status = _run(scenario)

    assert started_id
    assert again is None
    assert status == "running"


def test_an_empty_queue_hands_out_nothing() -> None:
    assert (
        _run(lambda registry: registry.claim_next("worker_1", lease_seconds=60)) is None
    )


def test_a_task_another_transaction_already_claimed_is_not_handed_out_again() -> None:
    """Why the claiming UPDATE repeats the status the sub-select already filtered.

    PostgreSQL re-checks an UPDATE's qualification against the row another
    transaction just wrote, but it does not re-run the sub-select inside that
    qualification. So ``task_id = (SELECT ... WHERE status = 'queued')`` still
    matches a row that is no longer queued, and without the second condition
    the same Task is dispatched twice -- measured, not assumed: dropping the
    condition makes this return the task instead of nothing.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> Any:
        task = await registry.submit(_submission())
        return await _while_uncommitted(
            engine,
            update(task_runs)
            .where(task_runs.c.task_id == task.task_id)
            .values(
                status="running",
                lease_owner="racer",
                lease_epoch=1,
                lease_until=func.now() + text("interval '1 minute'"),
                heartbeat_at=func.now(),
            ),
            lambda: registry.claim_next("worker_1", lease_seconds=60),
        )

    assert _run_with_engine(scenario) is None


# --------------------------------------------------------------------------
# Transitions are conditional, and the condition is the domain's


@pytest.mark.parametrize(
    ("move", "kwargs", "expected"),
    [
        ("mark_succeeded", {}, "succeeded"),
        ("mark_failed", {"reason": "the model call died"}, "failed"),
        ("park_for_migration", {"reason": "written by v0"}, "waiting_migration"),
        ("await_approval", {}, "waiting_approval"),
        ("cancel", {"reason": "the owner asked"}, "cancelled"),
    ],
)
def test_a_running_task_can_move_where_the_table_allows(
    move: str, kwargs: dict[str, Any], expected: str
) -> None:
    async def scenario(registry: PostgresTaskRegistry) -> tuple[str, str | None]:
        await registry.submit(_submission())
        claim = await _claim(registry)
        if move == "cancel":
            moved = await registry.cancel(claim.task.task_id, **kwargs)
        else:
            moved = await getattr(registry, move)(claim.lease, **kwargs)
        return moved.status, moved.status_detail

    status, detail = _run(scenario)

    assert status == expected
    assert (detail is None) is (
        expected not in {"failed", "waiting_migration", "cancelled"}
    )


@pytest.mark.parametrize(
    ("move", "kwargs", "event_type"),
    [
        ("mark_succeeded", {}, "TaskSucceeded"),
        ("mark_failed", {"reason": "provider exception body"}, "TaskFailed"),
        (
            "park_for_migration",
            {"reason": "a private graph version detail"},
            "TaskParkedForMigration",
        ),
        ("await_approval", {}, "TaskAwaitingApproval"),
        ("cancel", {"reason": "owner supplied private text"}, "TaskCancelled"),
    ],
)
def test_each_settlement_writes_its_own_safe_lifecycle_event(
    move: str, kwargs: dict[str, Any], event_type: str
) -> None:
    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> dict[str, Any]:
        task = await registry.submit(_submission())
        claim = await _claim(registry)
        if move == "cancel":
            await registry.cancel(task.task_id, **kwargs)
        else:
            await getattr(registry, move)(claim.lease, **kwargs)
        async with engine.connect() as connection:
            return (
                await connection.execute(
                    select(events.c.payload)
                    .where(events.c.stream_id == task.thread_id)
                    .order_by(events.c.sequence.desc())
                    .limit(1)
                )
            ).scalar_one()

    payload = _run_with_engine(scenario)

    assert payload["kind"] == event_type
    assert payload["task_id"]
    assert "private" not in str(payload)
    assert "exception" not in str(payload)


def test_a_queued_task_cannot_be_settled_as_if_it_had_run() -> None:
    """``running`` is the only source of ``succeeded``, and the WHERE says so."""

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, str]:
        task = await registry.submit(_submission())
        with pytest.raises(TaskTransitionRejectedError) as captured:
            await registry.mark_succeeded(
                ExecutionLease(task_id=task.task_id, worker_id="worker_1", epoch=1)
            )
        stored = await registry.get(task.task_id)
        assert stored is not None
        return captured.value.found_status, stored.status

    found, still = _run(scenario)

    # The error names where the Task actually was, not just that it refused.
    assert found == "queued"
    # And refusing left it exactly there.
    assert still == "queued"


@pytest.mark.parametrize(
    ("move", "kwargs"),
    [
        ("mark_succeeded", {}),
        ("mark_failed", {"reason": "too late"}),
        ("await_approval", {}),
        ("cancel", {"reason": "too late"}),
        ("park_for_migration", {"reason": "too late"}),
    ],
)
def test_nothing_moves_a_task_out_of_a_terminal_state(
    move: str, kwargs: dict[str, Any]
) -> None:
    """A late approval, a late cancel and a late settle all lose.

    Terminal statuses have no outgoing edge in the table, so this is not five
    rules -- it is one, checked five ways.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, str]:
        await registry.submit(_submission())
        claim = await _claim(registry)
        await registry.mark_succeeded(claim.lease)
        with pytest.raises(TaskTransitionRejectedError) as captured:
            if move == "cancel":
                await registry.cancel(claim.task.task_id, **kwargs)
            else:
                await getattr(registry, move)(claim.lease, **kwargs)
        stored = await registry.get(claim.task.task_id)
        assert stored is not None
        return captured.value.found_status, stored.status

    found, still = _run(scenario)

    assert found == "succeeded"
    assert still == "succeeded"


def test_a_task_parked_for_a_migration_has_no_way_out_yet() -> None:
    """Not an oversight: nothing in the plan says who performs a migration.

    An edge invented here would be a procedure nobody has designed, so the
    table has none and this records that it is a decision rather than a gap in
    the tests.
    """

    assert ALLOWED_TRANSITIONS["waiting_migration"] == frozenset()

    async def scenario(registry: PostgresTaskRegistry) -> Any:
        await registry.submit(_submission())
        claim = await _claim(registry)
        await registry.park_for_migration(claim.lease, reason="written by v0")
        with pytest.raises(TaskTransitionRejectedError) as captured:
            await registry.cancel(claim.task.task_id, reason="give up")
        return captured.value.found_status

    assert _run(scenario) == "waiting_migration"


def test_a_move_on_a_task_that_does_not_exist_says_so() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> Any:
        with pytest.raises(TaskTransitionRejectedError) as captured:
            await registry.mark_succeeded(
                ExecutionLease(task_id="task_absent", worker_id="worker_1", epoch=1)
            )
        return captured.value.found_status

    assert _run(scenario) is None


@pytest.mark.parametrize(
    ("move", "kwargs"),
    [("mark_succeeded", {"reason": "unnecessary"}), ("mark_failed", {})],
)
def test_whether_a_move_takes_a_reason_is_settled_by_its_signature(
    move: str, kwargs: dict[str, Any]
) -> None:
    """There is no way to ask for a reason where none belongs, or omit one.

    Not a runtime check: the methods that need a reason require it and the ones
    that do not accept it, so the mistake is a ``TypeError`` before anything
    reaches the database.
    """

    async def scenario(registry: PostgresTaskRegistry) -> None:
        await registry.submit(_submission())
        claim = await _claim(registry)
        with pytest.raises(TypeError):
            await getattr(registry, move)(claim.lease, **kwargs)

    _run(scenario)


@pytest.mark.parametrize("reason", ["", "   "])
def test_an_empty_reason_is_refused_before_it_becomes_an_unreadable_row(
    reason: str,
) -> None:
    """The gap between what the column accepts and what the model can read.

    ``status_detail`` is nullable text, so an empty string satisfies both the
    NOT NULL half of the lifecycle constraint and the column's type. ``TaskRun``
    requires a non-empty reason, so such a row writes successfully and then
    cannot be read back -- the worst shape a failure can take. Validated
    through the same type the model uses, so the two cannot drift apart.
    """

    async def scenario(registry: PostgresTaskRegistry) -> str:
        await registry.submit(_submission())
        claim = await _claim(registry)
        with pytest.raises(ValidationError):
            await registry.mark_failed(claim.lease, reason=reason)
        stored = await registry.get(claim.task.task_id)
        assert stored is not None
        return stored.status

    # And the refused move left the Task exactly where it was.
    assert _run(scenario) == "running"


def test_the_sql_condition_is_the_domain_table_and_not_a_second_copy() -> None:
    """What ``sources_for`` promises, spelled out where a reader can check it."""

    assert sources_for("running") == {"queued"}
    assert sources_for("succeeded") == {"running"}
    assert sources_for("cancelled") == {"queued", "running", "waiting_approval"}
    for terminal in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()


# --------------------------------------------------------------------------
# The row and the event that opened it


def test_opening_a_task_records_why_in_the_same_transaction() -> None:
    """WP07's first exit condition: state and event commit together."""

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> tuple[Any, ...]:
        task = await registry.submit(_submission())
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(
                            events.c.event_type, events.c.stream_id, events.c.payload
                        )
                    )
                )
                .mappings()
                .all()
            )
        return (
            *(
                (row["event_type"], row["stream_id"], row["payload"]["input_ref"])
                for row in rows
            ),
            task.thread_id,
        )

    recorded = _run_with_engine(scenario)
    thread_id = recorded[-1]

    assert len(recorded) == 2
    # On the Task's own stream, so it is the first thing on its timeline.
    assert recorded[0] == ("TaskSubmitted", thread_id, "input_1")


def test_a_repeated_submission_does_not_open_a_second_event() -> None:
    """Idempotent by the Task, so a retried submission adds no history."""

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> int:
        await registry.submit(_submission())
        await registry.submit(_submission(thread_id="thr_retry"))
        await registry.submit(_submission(thread_id="thr_retry_2"))
        async with engine.connect() as connection:
            return len((await connection.execute(select(events))).all())

    assert _run_with_engine(scenario) == 1


def test_a_submission_that_cannot_be_recorded_opens_no_task() -> None:
    """The other direction of the same transaction.

    A log that refuses the event must take the Task row down with it, or the
    Registry would hold a Task nothing can explain.
    """

    class _RefusingLog(PostgresEventLog):
        async def append_durable_in_transaction(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the event log refused")

    async def scenario(engine: Any, _: PostgresTaskRegistry) -> tuple[int, int]:
        registry = PostgresTaskRegistry(engine, events=_RefusingLog(engine))
        with pytest.raises(RuntimeError, match="refused"):
            await registry.submit(_submission())
        async with engine.connect() as connection:
            tasks = len((await connection.execute(select(task_runs))).all())
            recorded = len((await connection.execute(select(events))).all())
        return tasks, recorded

    assert _run_with_engine(scenario) == (0, 0)


def test_a_conflicting_key_is_reported_as_a_submission_not_as_an_event() -> None:
    """The caller made an ordinary mistake, and hears about that one.

    The identity check runs before the append, so a key reused for a different
    request never reaches the log -- where it would surface as a conflict about
    this project's own event bookkeeping.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> int:
        await registry.submit(_submission())
        with pytest.raises(TaskSubmissionConflictError):
            await registry.submit(_submission(input_ref="input_2"))
        async with engine.connect() as connection:
            return len((await connection.execute(select(events))).all())

    # And the refused submission left no second event behind.
    assert _run_with_engine(scenario) == 1


def test_the_event_is_not_visible_outside_the_transaction_that_writes_it() -> None:
    """A reader must never see a Task's history before the Task.

    Checked from a *second* connection while the append is in flight: nothing
    outside sees the event until the transaction closes.

    What this does not distinguish is "the registry's transaction" from "a
    transaction the log opened one frame up" -- `append` calls the
    in-transaction form, so both look identical from here, and separating them
    would need a seam in `submit` that exists only for the test. The properties
    that do the work are covered instead: a refused append opens no Task, and a
    repeated submission appends nothing.
    """

    dsn = _dsn()
    seen: list[int] = []

    class _ObservingLog(PostgresEventLog):
        async def append_durable_in_transaction(self, *args: Any, **kwargs: Any) -> Any:
            appended = await super().append_durable_in_transaction(*args, **kwargs)
            observer = create_query_engine(dsn, application_name="observer")
            try:
                async with observer.connect() as connection:
                    seen.append(len((await connection.execute(select(events))).all()))
            finally:
                await observer.dispose()
            return appended

    async def scenario(engine: Any, _: PostgresTaskRegistry) -> int:
        registry = PostgresTaskRegistry(engine, events=_ObservingLog(engine))
        await registry.submit(_submission())
        async with engine.connect() as connection:
            return len((await connection.execute(select(events))).all())

    committed = _run_with_engine(scenario)

    assert seen == [0]
    assert committed == 1


# --------------------------------------------------------------------------
# What the Task meant when it was submitted


def test_the_submitted_semantics_survive_a_round_trip() -> None:
    """A resume restores what the Task meant, so it has to come back intact."""

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, ...]:
        opened = await registry.submit(_submission())
        reread = await registry.get(opened.task_id)
        assert reread is not None
        return (
            reread.run_semantics_snapshot,
            reread.run_semantics_revision,
            reread.submitted_policy_revision,
            reread.submitted_policy_fingerprint,
            reread.submitted_authorization_envelope.max_tool_risk,
            reread.submitted_principal_scopes,
        )

    snapshot, revision, policy_revision, fingerprint, risk, scopes = _run(scenario)

    assert snapshot == {"model": {"provider": "deepseek"}}
    assert revision == "1.2:v1.3:abc0123456789def"
    assert policy_revision == "policy-1"
    assert fingerprint == "f" * 16
    # The envelope comes back as the model, not as a dict: an authorization
    # ceiling read as raw JSON is one nothing re-validates.
    assert risk == "read"
    assert scopes == ()


def test_a_retried_key_with_more_scopes_keeps_the_original_scope_ceiling() -> None:
    """An idempotency retry cannot widen the Task it resolves to."""

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, Any]:
        first = await registry.submit(
            _submission(submitted_principal_scopes=("knowledge:read",))
        )
        retry = await registry.submit(
            _submission(
                thread_id="thr_retry",
                submitted_principal_scopes=("external:search", "knowledge:read"),
            )
        )
        return first, retry

    first, retry = _run(scenario)

    assert retry.task_id == first.task_id
    assert retry.submitted_principal_scopes == ("knowledge:read",)


def test_a_key_reused_under_new_server_semantics_returns_the_original_task() -> None:
    """Semantics and policy are persisted from the first accepted request.

    They are deployment decisions, not caller-supplied request identity. A
    retry must keep the original snapshot rather than fail because settings or
    policy identity changed between attempts.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, Any]:
        first = await registry.submit(_submission())
        retry = await registry.submit(
            _submission(
                thread_id="thr_retry",
                run_semantics_revision="1.2:v1.3:0000000000000000",
                submitted_policy_fingerprint="0" * 16,
            )
        )
        return first, retry

    first, retry = _run(scenario)

    assert retry == first


def test_the_snapshot_a_task_carries_is_the_one_it_was_submitted_with() -> None:
    """Two Tasks opened under different semantics keep their own.

    A snapshot read from live settings at resume time would make an old Task
    mean whatever the deployment means now.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, Any]:
        first = await registry.submit(_submission())
        second = await registry.submit(
            _submission(
                thread_id="thr_2",
                submission_dedup_key="dedup_2",
                run_semantics_snapshot={"model": {"provider": "other"}},
            )
        )
        return first.run_semantics_snapshot, second.run_semantics_snapshot

    first, second = _run(scenario)

    assert first == {"model": {"provider": "deepseek"}}
    assert second == {"model": {"provider": "other"}}


# --------------------------------------------------------------------------
# The Qdrant index a Task is reserved against (WP07-04)

GENERATION = "6f1d5a02-0000-4000-8000-000000000001"


async def _generation(
    registry: PostgresTaskRegistry,
    *,
    generation_id: str = GENERATION,
    collection: str = "kb_v3",
    index_version: str = "3",
    status: str = "active",
) -> IndexReservation:
    async with registry._engine.begin() as connection:
        await connection.execute(
            qdrant_index_generations.insert().values(
                generation_id=generation_id,
                collection_name=collection,
                index_version=index_version,
                status=status,
            )
        )
    return IndexReservation(
        collection_name=collection,
        index_version=index_version,
        generation_id=generation_id,
    )


def test_a_submission_stores_the_concrete_index_it_reserved() -> None:
    """Never the alias: the three columns are what a resume reads."""

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, ...]:
        reservation = await _generation(registry)
        opened = await registry.submit(_submission(index_reservation=reservation))
        reread = await registry.get(opened.task_id)
        assert reread is not None
        return (
            reread.resolved_qdrant_collection,
            reread.resolved_qdrant_index_version,
            reread.resolved_qdrant_index_generation_id,
        )

    assert _run(scenario) == ("kb_v3", "3", GENERATION)


def test_a_submission_s_objective_label_survives_the_round_trip() -> None:
    """The label is what a Task list shows, so it has to come back off the row."""

    async def scenario(registry: PostgresTaskRegistry) -> Any:
        opened = await registry.submit(
            _submission(objective_preview="整理这批资料并输出一份建议报告")
        )
        reread = await registry.get(opened.task_id)
        assert reread is not None
        return reread.objective_preview

    assert _run(scenario) == "整理这批资料并输出一份建议报告"


def test_a_submission_without_a_label_opens_a_task_anyway() -> None:
    """Resume paths submit an input reference and have no objective to label with."""

    async def scenario(registry: PostgresTaskRegistry) -> Any:
        opened = await registry.submit(_submission())
        return opened.objective_preview

    assert _run(scenario) is None


def test_a_retry_is_not_rejected_for_disagreeing_about_the_label() -> None:
    """The label is derived from the input, so it cannot be an identity field.

    Rejecting here would turn an ordinary retry -- one whose objective merely
    re-wrapped its whitespace -- into a submission conflict.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[str, Any]:
        first = await registry.submit(_submission(objective_preview="first label"))
        again = await registry.submit(_submission(objective_preview="second label"))
        return first.task_id, again.task_id

    opened, retried = _run(scenario)
    assert opened == retried


def test_a_task_that_reserves_nothing_stores_nothing() -> None:
    """A Task touching no knowledge base has no index to be bound to."""

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, ...]:
        opened = await registry.submit(_submission())
        return (
            opened.resolved_qdrant_collection,
            opened.resolved_qdrant_index_version,
            opened.resolved_qdrant_index_generation_id,
        )

    assert _run(scenario) == (None, None, None)


@pytest.mark.parametrize("status", ["draining", "retired"])
def test_a_generation_that_stopped_taking_reservations_refuses_the_task(
    status: str,
) -> None:
    """Reserve-or-retry: the submission fails closed and writes nothing.

    ``draining`` keeps existing reservations valid while refusing new ones, so
    an alias switch can drain instead of cutting; ``retired`` refuses outright.
    Either way the caller resolves again rather than committing a reference to
    an index it may not serve from.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, int]:
        reservation = await _generation(registry, status=status)
        with pytest.raises(IndexGenerationNotReservableError) as captured:
            await registry.submit(_submission(index_reservation=reservation))
        async with registry._engine.connect() as connection:
            tasks = len((await connection.execute(select(task_runs))).all())
        return captured.value.found_status, tasks

    found, tasks = _run(scenario)

    assert found == status
    # The whole submission rolled back, so there is no half-opened Task.
    assert tasks == 0


def test_a_reservation_naming_a_generation_that_does_not_exist_is_refused() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> Any:
        phantom = IndexReservation(
            collection_name="kb_v3",
            index_version="3",
            generation_id="6f1d5a02-0000-4000-8000-00000000dead",
        )
        with pytest.raises(IndexGenerationNotReservableError) as captured:
            await registry.submit(_submission(index_reservation=phantom))
        return captured.value.found_status

    assert _run(scenario) is None


def test_a_reservation_must_name_the_generation_s_own_collection() -> None:
    """The triple is checked as a triple, not as an id with two labels.

    A generation id paired with the wrong collection or version would store a
    snapshot that disagrees with the index it points at -- which is precisely
    the inconsistency the plan says must fail closed.
    """

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, Any]:
        real = await _generation(registry)
        mislabelled = real.model_copy(update={"collection_name": "kb_v2"})
        with pytest.raises(IndexGenerationNotReservableError):
            await registry.submit(_submission(index_reservation=mislabelled))
        wrong_version = real.model_copy(update={"index_version": "2"})
        with pytest.raises(IndexGenerationNotReservableError):
            await registry.submit(
                _submission(
                    thread_id="thr_v2",
                    submission_dedup_key="dedup_v2",
                    index_reservation=wrong_version,
                )
            )
        async with registry._engine.connect() as connection:
            return len((await connection.execute(select(task_runs))).all()), None

    assert _run(scenario) == (0, None)


def test_a_reserved_generation_cannot_be_deleted_while_a_task_holds_it() -> None:
    """The foreign key *is* the reservation.

    Not a convention a future GC has to remember: while any Task references the
    generation, deleting it is impossible, so the collection it names cannot be
    reclaimed underneath a Task that is still going to read from it.
    """

    async def scenario(registry: PostgresTaskRegistry) -> None:
        reservation = await _generation(registry)
        await registry.submit(_submission(index_reservation=reservation))
        with pytest.raises(IntegrityError):
            async with registry._engine.begin() as connection:
                await connection.execute(
                    qdrant_index_generations.delete().where(
                        qdrant_index_generations.c.generation_id
                        == reservation.generation_id
                    )
                )

    _run(scenario)


def test_a_retirement_racing_a_submission_cannot_slip_between_check_and_insert() -> (
    None
):
    """The generation row is locked, so the two orderings both stay consistent.

    A retirement that commits first makes the submission fail closed; one that
    arrives while the submission holds the row waits, and then finds the
    reference. What must never happen is a committed Task pointing at a
    generation that was retired without seeing it.
    """

    async def scenario(engine: Any, registry: PostgresTaskRegistry) -> tuple[Any, ...]:
        reservation = await _generation(registry)

        # Retire the generation in a transaction that has not committed, then
        # submit: the submission blocks on the locked row rather than reading a
        # stale `active`.
        retired = await _while_uncommitted(
            engine,
            qdrant_index_generations.update()
            .where(
                qdrant_index_generations.c.generation_id == reservation.generation_id
            )
            .values(status="retired"),
            lambda: _submit_or_error(registry, reservation),
        )
        async with engine.connect() as connection:
            tasks = len((await connection.execute(select(task_runs))).all())
        return type(retired).__name__, tasks

    error, tasks = _run_with_engine(scenario)

    assert error == "IndexGenerationNotReservableError"
    assert tasks == 0


async def _submit_or_error(
    registry: PostgresTaskRegistry, reservation: IndexReservation
) -> object:
    try:
        return await registry.submit(_submission(index_reservation=reservation))
    except IndexGenerationNotReservableError as error:
        return error
