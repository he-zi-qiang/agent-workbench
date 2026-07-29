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
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text, update

from agent_workbench.adapters.persistence import (
    PostgresEventLog,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import events, task_runs
from agent_workbench.domain.task_registry import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    sources_for,
)
from agent_workbench.ports.task_registry import (
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
                    text("TRUNCATE task_runs, events, event_streams CASCADE")
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
                    text("TRUNCATE task_runs, events, event_streams CASCADE")
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
    }
    base.update(overrides)
    return TaskSubmission.model_validate(base)


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
        second = await registry.submit(_submission())
        third = await registry.submit(_submission())
        return first.task_id, second.task_id, len({first, second, third})

    first_id, second_id, distinct = _run(scenario)

    assert first_id == second_id
    # Not just the same id: the same Task, field for field.
    assert distinct == 1


def test_a_repeated_key_with_a_different_request_is_a_conflict() -> None:
    """Idempotency answers the same question again; it does not answer a new one."""

    async def scenario(registry: PostgresTaskRegistry) -> None:
        await registry.submit(_submission())
        with pytest.raises(TaskSubmissionConflictError) as captured:
            await registry.submit(_submission(input_ref="input_2"))
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
            "submission_dedup_key": "dedup_1",
            "status": "queued",
            "status_detail": None,
        }
        returned = await _while_uncommitted(
            engine,
            task_runs.insert().values(winner),
            lambda: registry.submit(_submission()),
        )
        async with engine.connect() as connection:
            rows = len((await connection.execute(select(task_runs))).all())
        return returned.task_id, rows

    task_id, rows = _run_with_engine(scenario)

    assert task_id == "task_winner"
    assert rows == 1


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
        started = [await registry.start_next() for _ in range(4)]
        return [task.task_id if task else None for task in started] + [
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
        started = await registry.start_next()
        again = await registry.start_next()
        stored = await registry.get(task.task_id)
        assert stored is not None
        return started.task_id if started else "", again, stored.status

    started_id, again, status = _run(scenario)

    assert started_id
    assert again is None
    assert status == "running"


def test_an_empty_queue_hands_out_nothing() -> None:
    assert _run(lambda registry: registry.start_next()) is None


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
            .values(status="running"),
            registry.start_next,
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
        started = await registry.start_next()
        assert started is not None
        moved = await getattr(registry, move)(started.task_id, **kwargs)
        return moved.status, moved.status_detail

    status, detail = _run(scenario)

    assert status == expected
    assert (detail is None) is (
        expected not in {"failed", "waiting_migration", "cancelled"}
    )


def test_a_queued_task_cannot_be_settled_as_if_it_had_run() -> None:
    """``running`` is the only source of ``succeeded``, and the WHERE says so."""

    async def scenario(registry: PostgresTaskRegistry) -> tuple[Any, str]:
        task = await registry.submit(_submission())
        with pytest.raises(TaskTransitionRejectedError) as captured:
            await registry.mark_succeeded(task.task_id)
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
        started = await registry.start_next()
        assert started is not None
        await registry.mark_succeeded(started.task_id)
        with pytest.raises(TaskTransitionRejectedError) as captured:
            await getattr(registry, move)(started.task_id, **kwargs)
        stored = await registry.get(started.task_id)
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
        started = await registry.start_next()
        assert started is not None
        await registry.park_for_migration(started.task_id, reason="written by v0")
        with pytest.raises(TaskTransitionRejectedError) as captured:
            await registry.cancel(started.task_id, reason="give up")
        return captured.value.found_status

    assert _run(scenario) == "waiting_migration"


def test_a_move_on_a_task_that_does_not_exist_says_so() -> None:
    async def scenario(registry: PostgresTaskRegistry) -> Any:
        with pytest.raises(TaskTransitionRejectedError) as captured:
            await registry.mark_succeeded("task_absent")
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
        started = await registry.start_next()
        assert started is not None
        with pytest.raises(TypeError):
            await getattr(registry, move)(started.task_id, **kwargs)

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
        started = await registry.start_next()
        assert started is not None
        with pytest.raises(ValidationError):
            await registry.mark_failed(started.task_id, reason=reason)
        stored = await registry.get(started.task_id)
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
        await registry.submit(_submission())
        await registry.submit(_submission())
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
