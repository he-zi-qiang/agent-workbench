"""A Task's events, read back as one ordered timeline.

The last of WP06's exit conditions: a Task query returns a unified event
timeline. Unified means one stream -- the events a Task's runs append go to the
thread it owns, so a single ``(stream, sequence)`` cursor means "everything
about this Task up to here" and a client that reconnects sends back one value.

The events here are appended through the same ``EventLogPort`` an agent run
uses, against real PostgreSQL, because the ordering being tested is the log's
gap-free per-stream sequence and not a list this test built.

Real PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence import (
    PostgresEventLog,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.application.tasks import (
    TaskService,
    TimelineUnavailableError,
    task_stream_id,
)
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.events import RunCompleted, RunStarted, ToolStarted
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventCursor, EventScope
from agent_workbench.ports.task_registry import TaskRun

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = "task_runs, events, event_streams"

OWNER = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")
OTHER_OWNER = PrincipalContext(principal_id="user_2", tenant_id="tenant_a")


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(scenario: Callable[[TaskService, Any], Awaitable[Any]]) -> Any:
    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            service = TaskService(
                registry=PostgresTaskRegistry(engine),
                events=PostgresEventLog(engine),
            )
            return await scenario(service, PostgresEventLog(engine))
        finally:
            await engine.dispose()

    return asyncio.run(execute())


async def _open(service: TaskService, key: str = "dedup_1") -> TaskRun:
    return await service.submit(OWNER, input_ref="input_1", submission_dedup_key=key)


async def _record(log: Any, task: TaskRun, count: int) -> None:
    """Append the shape of events an agent run inside a graph node produces."""

    scope = EventScope(
        stream_id=task_stream_id(task),
        run_id="run_1",
        task_id=task.task_id,
        graph_node_id="understand",
    )
    payloads = [
        RunStarted(
            run_kind="task",
            model_profile="main",
            budget=RunBudget(max_steps=4, max_tool_calls=4),
        ),
        ToolStarted(tool_call_id="call_1", tool_name="knowledge_search"),
        RunCompleted(stop_reason="completed"),
    ]
    for index in range(count):
        await log.append(scope, payloads[index % len(payloads)])


# --------------------------------------------------------------------------


def test_a_task_s_events_come_back_in_order_with_a_cursor() -> None:
    async def scenario(service: TaskService, log: Any) -> tuple[list[int], Any, int]:
        task = await _open(service)
        await _record(log, task, 5)
        timeline = await service.timeline(OWNER, task.task_id)
        return (
            [event.sequence for event in timeline.events],
            timeline.cursor,
            len(timeline.events),
        )

    sequences, cursor, count = _run(scenario)

    assert count == 5
    # Gap-free and ascending: the log's per-stream sequence, not a sort here.
    assert sequences == [1, 2, 3, 4, 5]
    assert cursor is not None
    assert cursor.sequence == 5


def test_a_cursor_resumes_exactly_where_the_last_slice_stopped() -> None:
    """Sliced, and the two halves join without overlap or gap."""

    async def scenario(service: TaskService, log: Any) -> tuple[list[int], list[int]]:
        task = await _open(service)
        await _record(log, task, 6)
        first = await service.timeline(OWNER, task.task_id, limit=4)
        assert first.cursor is not None
        second = await service.timeline(OWNER, task.task_id, after=first.cursor)
        return (
            [event.sequence for event in first.events],
            [event.sequence for event in second.events],
        )

    first, second = _run(scenario)

    assert first == [1, 2, 3, 4]
    assert second == [5, 6]


def test_a_cursor_at_the_end_of_the_stream_delivers_nothing_and_stays_put() -> None:
    """The position must not jump forward over events that have not arrived."""

    async def scenario(service: TaskService, log: Any) -> tuple[int, Any, Any]:
        task = await _open(service)
        await _record(log, task, 3)
        caught_up = await service.timeline(OWNER, task.task_id)
        assert caught_up.cursor is not None
        idle = await service.timeline(OWNER, task.task_id, after=caught_up.cursor)
        return len(idle.events), idle.cursor, caught_up.cursor

    delivered, idle_cursor, previous = _run(scenario)

    assert delivered == 0
    assert idle_cursor == previous


def test_a_task_with_no_events_has_an_empty_timeline_and_no_cursor() -> None:
    async def scenario(service: TaskService, log: Any) -> tuple[int, Any]:
        task = await _open(service)
        timeline = await service.timeline(OWNER, task.task_id)
        return len(timeline.events), timeline.cursor

    assert _run(scenario) == (0, None)


def test_one_task_s_timeline_never_contains_another_s() -> None:
    """Unified means one stream per Task, and the streams do not mix."""

    async def scenario(service: TaskService, log: Any) -> tuple[list[str], list[str]]:
        first = await _open(service, key="dedup_1")
        second = await _open(service, key="dedup_2")
        await _record(log, first, 3)
        await _record(log, second, 2)
        one = await service.timeline(OWNER, first.task_id)
        two = await service.timeline(OWNER, second.task_id)
        return (
            [event.stream_id for event in one.events],
            [event.stream_id for event in two.events],
        )

    one, two = _run(scenario)

    assert len(one) == 3
    assert len(two) == 2
    assert set(one).isdisjoint(set(two))


# --------------------------------------------------------------------------
# The boundary a timeline read must not be weaker than


def test_another_owner_cannot_read_a_timeline_they_cannot_read_the_task_of() -> None:
    """Checked before the log is touched, and refused the same way.

    A timeline that answered differently would leak precisely what the Task
    read refuses to: that the id exists.
    """

    async def scenario(service: TaskService, log: Any) -> None:
        task = await _open(service)
        await _record(log, task, 2)
        with pytest.raises(NotFoundError):
            await service.timeline(OTHER_OWNER, task.task_id)

    _run(scenario)


def test_a_cursor_from_another_stream_is_refused_rather_than_ignored() -> None:
    """A cursor is client-supplied, so it is validated against this Task.

    Ignoring a foreign cursor would silently restart the timeline from the top
    for a client that asked to continue something else.
    """

    async def scenario(service: TaskService, log: Any) -> None:
        task = await _open(service)
        await _record(log, task, 2)
        with pytest.raises(NotFoundError):
            await service.timeline(
                OWNER,
                task.task_id,
                after=EventCursor(stream_id="thr_somebody_else", sequence=1),
            )

    _run(scenario)


def test_a_limit_of_zero_is_refused() -> None:
    """A slice of nothing is a request that cannot mean what it says.

    That the *upper* bound is capped is asserted where it can be observed
    without storing five hundred events -- against a log that records what it
    was asked for, in the service's own tests.
    """

    async def scenario(service: TaskService, log: Any) -> None:
        task = await _open(service)
        with pytest.raises(ValueError):
            await service.timeline(OWNER, task.task_id, limit=0)

    _run(scenario)


def test_a_service_without_a_log_says_so_instead_of_returning_nothing() -> None:
    """An empty timeline and an unwired one must not look the same.

    Returning "no events" for a service that cannot read any would present a
    misconfiguration as a Task that did nothing.
    """

    dsn = _dsn()

    async def scenario() -> None:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            service = TaskService(registry=PostgresTaskRegistry(engine))
            with pytest.raises(TimelineUnavailableError):
                await service.timeline(OWNER, "task_1")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
