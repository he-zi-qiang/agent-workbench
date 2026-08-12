"""One unreadable row no longer ends a subscription -- and never does so quietly.

The mechanism these tests exercise lives in the event log; what they are about
is the subscription that now uses it. Two properties matter equally. Events
after a damaged row must arrive, because otherwise a single row from an
abandoned experiment makes a whole session unreachable. And the subscriber must
be *told*, in the stream itself, that a position was skipped: a replay that is
silently short is worse than one that stops, because a replay that stops is
something somebody notices.

Real PostgreSQL only. The corruption under test is a stored row, and a log that
returns whatever it was handed cannot have one.

Each scenario writes to a stream id of its own and never truncates. Other
suites share this database and truncate ``events`` freely; a TRUNCATE here
would delete their rows just as readily, and nothing below reads back anything
but the stream it just wrote.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.adapters.persistence import (
    PostgresEventLog,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import events as events_table
from agent_workbench.apps.api.routes.events import (
    HEARTBEAT,
    QUARANTINE_EVENT,
    IsolatingEventLog,
    _frame,
    _stream,
)
from agent_workbench.domain.events import RunCompleted, RunStarted, ToolStarted
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventCursor, EventScope

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
REQUIRED_TEST_DATABASE_SUFFIX = "_test"

BUDGET = RunBudget(max_steps=4, max_tool_calls=4)


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    database = dsn.rsplit("/", maxsplit=1)[-1].split("?", maxsplit=1)[0]
    if not database.endswith(REQUIRED_TEST_DATABASE_SUFFIX):
        raise AssertionError(
            f"{TEST_DSN_ENV_VAR} must name a database ending in "
            f"{REQUIRED_TEST_DATABASE_SUFFIX!r}; this suite writes damaged rows"
        )
    return dsn


def _run[T](scenario: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    """Open the engine inside the loop the scenario runs on.

    An engine is bound to the loop it was created on, so one handed across an
    ``asyncio.run`` boundary leaves connections attached to a closed loop.
    """

    dsn = _dsn()

    async def execute() -> T:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _scope() -> EventScope:
    # Unique per scenario: see the module docstring on why nothing truncates.
    return EventScope(
        stream_id=f"ses_isolate_{uuid.uuid4().hex[:16]}",
        run_id="run_isolate",
    )


def _started() -> RunStarted:
    return RunStarted(run_kind="chat", model_profile="main", budget=BUDGET)


async def _write_three(log: PostgresEventLog, scope: EventScope) -> tuple[str, ...]:
    """One ordinary session: three durable events at sequences 1, 2, 3."""

    first = await log.append(scope, _started())
    second = await log.append(
        scope, ToolStarted(tool_call_id="tc_1", tool_name="knowledge_search")
    )
    third = await log.append(scope, RunCompleted(stop_reason="completed"))
    return (first.event_id, second.event_id, third.event_id)


async def _damage(engine: AsyncEngine, event_id: str, **values: Any) -> None:
    """Leave a stored row this process cannot turn into an envelope.

    A payload that lost every field but its discriminator: the shape a
    partially written row or a bad hand-edit leaves behind.
    """

    async with engine.begin() as connection:
        await connection.execute(
            update(events_table)
            .where(events_table.c.event_id == event_id)
            .values(**values)
        )


async def _collect(
    log: Any,
    stream_id: str,
    *,
    after: int | None = None,
    frames: int,
    page_size: int = 500,
) -> list[str]:
    """Take the first ``frames`` frames, then stop.

    ``poll_seconds`` and ``heartbeat_seconds`` are zero so an idle stream keeps
    yielding heartbeats instead of blocking: a test that expects a fourth frame
    and never receives one should fail on the frame it got, not by hanging.
    """

    out: list[str] = []
    generator = _stream(
        log,
        stream_id=stream_id,
        after_sequence=after,
        poll_seconds=0,
        page_size=page_size,
        heartbeat_seconds=0,
        disconnected=None,
    )
    try:
        async for frame in generator:
            out.append(frame)
            if len(out) >= frames:
                break
    finally:
        await generator.aclose()
    return out


def _event_names(frames: list[str]) -> list[str]:
    return [
        frame.split("\n")[1].removeprefix("event: ")
        if frame is not HEARTBEAT
        else "heartbeat"
        for frame in frames
    ]


def _notice(frame: str) -> dict[str, Any]:
    body = next(
        line.removeprefix("data: ")
        for line in frame.split("\n")
        if line.startswith("data: ")
    )
    decoded: Any = json.loads(body)
    assert isinstance(decoded, dict)
    return decoded


def _frame_id(frame: str) -> EventCursor:
    return EventCursor.decode(frame.split("\n")[0].removeprefix("id: "))


# --- the poison row itself ---------------------------------------------------


def test_a_poison_row_no_longer_blocks_the_events_behind_it() -> None:
    """Before this, the strict read raised and took the session with it."""

    async def scenario(engine: AsyncEngine) -> tuple[Any, ...]:
        scope = _scope()
        log = PostgresEventLog(engine)
        _, second, _ = await _write_three(log, scope)
        await _damage(engine, second, payload={"kind": "RunStarted"})

        frames = await _collect(log, scope.stream_id, frames=3)
        return (_event_names(frames), frames, scope.stream_id)

    names, frames, stream_id = _run(scenario)

    # The event after the damage arrives, which is the whole point, and the
    # notice sits where the hole is rather than at the end of the page.
    assert names == ["RunStarted", QUARANTINE_EVENT, "RunCompleted"]
    assert [_frame_id(frame).sequence for frame in frames] == [1, 2, 3]
    assert all(_frame_id(frame).stream_id == stream_id for frame in frames)


def test_the_skipped_position_is_named_rather_than_merely_counted() -> None:
    """A count would say a replay is short without saying where or which row.

    An operator repairs a row by id; a client that wants to say "history is
    incomplete here" needs the position. Both are in the frame.
    """

    async def scenario(engine: AsyncEngine) -> tuple[Any, ...]:
        scope = _scope()
        log = PostgresEventLog(engine)
        _, second, _ = await _write_three(log, scope)
        await _damage(engine, second, payload={"kind": "RunStarted"})

        frames = await _collect(log, scope.stream_id, frames=3)
        return (_notice(frames[1]), frames[1], scope.stream_id, second)

    notice, frame, stream_id, damaged = _run(scenario)

    assert notice == {
        "event_id": damaged,
        # From the column, not the payload: the payload is what could not be
        # read, and the column is what an operator searches on.
        "event_type": "ToolStarted",
        "schema_version": 1,
        "sequence": 2,
        "stream_id": stream_id,
    }
    # The decode failure's reason names this process's envelope contract and is
    # deliberately kept to the server log, which already carries it against the
    # same event id.
    assert "run_kind" not in frame


def test_a_clean_stream_is_byte_identical_to_the_strict_replay() -> None:
    """The control group, and the one an over-eager isolator fails.

    An implementation that always went through the isolating path and quietly
    dropped or annotated something would still satisfy every assertion above.
    The oracle is the strict ``read`` -- untouched by this change -- rendered
    through the same frame writer, which is byte for byte what this stream
    produced before isolation existed.
    """

    async def scenario(engine: AsyncEngine) -> tuple[str, str]:
        scope = _scope()
        log = PostgresEventLog(engine)
        await _write_three(log, scope)

        frames = await _collect(log, scope.stream_id, frames=3)
        strict = await log.read(scope.stream_id)
        expected = "".join(
            _frame(envelope, scope.stream_id, envelope.sequence)
            for envelope in strict
            if envelope.sequence is not None
        )
        return ("".join(frames), expected)

    produced, expected = _run(scenario)

    assert produced == expected
    assert QUARANTINE_EVENT not in produced


# --- the two ways a caller gets the cursor wrong -----------------------------


def test_a_page_of_only_poison_rows_does_not_stop_the_replay() -> None:
    """The trap in ``if not page.events: break``.

    A page can be entirely undecodable: no events, and a resume position that
    has already moved past all of them. Stopping there parks the subscription
    in front of rows it has already skipped, and it never reaches the readable
    event on the next page.
    """

    async def scenario(engine: AsyncEngine) -> list[str]:
        scope = _scope()
        log = PostgresEventLog(engine)
        first, second, _ = await _write_three(log, scope)
        await _damage(engine, first, payload={"kind": "RunStarted"})
        await _damage(engine, second, payload={"kind": "RunStarted"})

        # Two rows per page, so the first page is nothing but damage.
        return await _collect(log, scope.stream_id, frames=3, page_size=2)

    frames = _run(scenario)

    assert _event_names(frames) == [
        QUARANTINE_EVENT,
        QUARANTINE_EVENT,
        "RunCompleted",
    ]


def test_a_trailing_poison_row_does_not_send_the_cursor_back_to_the_start() -> None:
    """The trap in ``cursor = page.resume_after``.

    An empty page reports ``None``, meaning "your own cursor is still the
    truth". Assigned anyway it becomes "start at the beginning", and the next
    poll replays the whole session -- forever, because every poll after a
    caught-up subscription returns an empty page.

    Two idle polls, not one: a subscription that reset its cursor would still
    emit the first heartbeat before re-reading the stream, so a fourth frame
    alone proves nothing.
    """

    async def scenario(engine: AsyncEngine) -> list[str]:
        scope = _scope()
        log = PostgresEventLog(engine)
        _, _, third = await _write_three(log, scope)
        await _damage(engine, third, payload={"kind": "RunStarted"})

        return await _collect(log, scope.stream_id, frames=5)

    frames = _run(scenario)

    assert _event_names(frames) == [
        "RunStarted",
        "ToolStarted",
        QUARANTINE_EVENT,
        "heartbeat",
        "heartbeat",
    ]
    # And the notice carries the position past the damage, so a browser that
    # reconnects with this Last-Event-ID does not meet the row again.
    assert _frame_id(frames[2]).sequence == 3


# --- logs that do not offer the capability -----------------------------------


def test_a_log_without_the_capability_keeps_the_strict_replay() -> None:
    """Isolation is opt-in, and the stream did not start requiring it.

    The in-memory log holds envelopes rather than rows, so it cannot have an
    undecodable one and does not offer the isolating read. Asserted rather than
    assumed: if it ever grew the method, the check above it would start taking
    a different path through the same stream.
    """

    async def scenario() -> list[str]:
        log = InMemoryEventLog()
        scope = EventScope(stream_id="ses_memory", run_id="run_1")
        assert not isinstance(log, IsolatingEventLog)
        await log.append(scope, _started())
        await log.append(scope, RunCompleted(stop_reason="completed"))
        return await _collect(log, scope.stream_id, frames=2)

    frames = asyncio.run(scenario())

    assert _event_names(frames) == ["RunStarted", "RunCompleted"]
    assert QUARANTINE_EVENT not in "".join(frames)
