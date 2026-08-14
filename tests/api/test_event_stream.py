"""Subscribing to a session's events, and resuming after a drop.

The property that matters is resumption: a client that reconnects with the last
id it saw must receive everything after it and nothing before. That only works
because sequences are gap-free, so these tests are the reason the event log was
built the way it was.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import text

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.adapters.persistence import (
    PostgresEventLog,
    create_query_engine,
)
from agent_workbench.application.answer_release import AnswerReleaseSink
from agent_workbench.apps.api.sse import (
    HEARTBEAT,
    LAST_EVENT_ID_HEADER,
    frame_for,
    resume_from,
    stream_events,
)
from agent_workbench.domain.events import ModelCompleted, ModelDelta, RunStarted
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventCursor, EventScope

BUDGET = RunBudget(max_steps=4, max_tool_calls=4)


def _started() -> RunStarted:
    return RunStarted(run_kind="chat", model_profile="main", budget=BUDGET)


class _FakeRequest:
    """Only the header lookup ``resume_from`` uses."""

    def __init__(self, raw: str | None) -> None:
        self.headers = {} if raw is None else {LAST_EVENT_ID_HEADER: raw}


# --- where a reconnecting client resumes from --------------------------------


def test_no_cursor_starts_at_the_beginning() -> None:
    assert resume_from(_FakeRequest(None), "ses_1") is None  # pyright: ignore[reportArgumentType]


def test_a_cursor_resumes_after_its_sequence() -> None:
    raw = EventCursor(stream_id="ses_1", sequence=7).encode()

    assert resume_from(_FakeRequest(raw), "ses_1") == 7  # pyright: ignore[reportArgumentType]


def test_a_cursor_for_another_stream_is_ignored() -> None:
    """Honouring its number would silently skip events in this one."""

    raw = EventCursor(stream_id="ses_other", sequence=7).encode()

    assert resume_from(_FakeRequest(raw), "ses_1") is None  # pyright: ignore[reportArgumentType]


def test_a_malformed_cursor_starts_over_rather_than_failing() -> None:
    """It arrives from a browser that may have stored it across a deploy.

    Refusing the connection would leave that client unable to reconnect at all,
    with no way to discover that clearing it would help.
    """

    assert resume_from(_FakeRequest("not-a-cursor"), "ses_1") is None  # pyright: ignore[reportArgumentType]


def test_uncommitted_answer_text_cannot_enter_an_sse_frame() -> None:
    """SSE serializes only the redacted provider event and safe refusal."""

    secret = "private answer generated before the final ACL check"

    async def scenario() -> str:
        log = InMemoryEventLog()
        scope = EventScope(stream_id="ses_1", run_id="run_1")
        release = AnswerReleaseSink(ScopedEventSink(log=log, scope=scope))
        await release.emit(
            ModelCompleted(
                model_call_id="mc_1",
                finish_reason="stop",
                text=secret,
            )
        )
        await release.withhold(text="safe refusal")
        replayed = await log.read(scope.stream_id)
        return "".join(
            frame_for(event, scope.stream_id, event.sequence)
            for event in replayed
            if event.sequence is not None
        )

    frames = asyncio.run(scenario())

    assert secret not in frames
    assert "safe refusal" in frames
    assert "AnswerWithheld" in frames


# --- the stream itself, against a real log -----------------------------------


async def _collect(
    log: Any, stream_id: str, *, after: int | None, frames: int
) -> list[str]:
    """Take the first ``frames`` frames, then stop."""

    out: list[str] = []
    generator = stream_events(
        log,
        stream_id=stream_id,
        after_sequence=after,
        poll_seconds=1,
        page_size=500,
        heartbeat_seconds=1,
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


async def _fresh_log(dsn: str) -> tuple[PostgresEventLog, Any]:
    """A log on an engine created inside the caller's own loop."""

    engine = create_query_engine(dsn, application_name="agent-workbench-tests")
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE events, event_streams CASCADE"))
    return PostgresEventLog(engine), engine


def test_a_subscriber_receives_the_events_in_order(events_dsn: str) -> None:
    async def scenario() -> list[str]:
        log, engine = await _fresh_log(events_dsn)
        try:
            scope = EventScope(stream_id=f"ses_{uuid.uuid4().hex}", run_id="run_1")
            for _ in range(3):
                await log.append(scope, _started())
            frames = await _collect(log, scope.stream_id, after=None, frames=3)
            return [f.split("\n")[0] for f in frames]
        finally:
            await engine.dispose()

    ids = asyncio.run(scenario())

    assert [i.split(":")[-1] for i in ids] == ["1", "2", "3"]


def test_resuming_returns_only_what_came_after(events_dsn: str) -> None:
    """The whole point of the cursor, and of gap-free sequences."""

    async def scenario() -> list[str]:
        log, engine = await _fresh_log(events_dsn)
        try:
            scope = EventScope(stream_id=f"ses_{uuid.uuid4().hex}", run_id="run_1")
            for _ in range(5):
                await log.append(scope, _started())
            frames = await _collect(log, scope.stream_id, after=2, frames=3)
            return [f.split("\n")[0].split(":")[-1] for f in frames]
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == ["3", "4", "5"]


def test_the_frame_id_is_a_cursor_the_client_can_send_back(
    events_dsn: str,
) -> None:
    async def scenario() -> tuple[str, int]:
        log, engine = await _fresh_log(events_dsn)
        try:
            scope = EventScope(stream_id=f"ses_{uuid.uuid4().hex}", run_id="run_1")
            await log.append(scope, _started())
            frame = (await _collect(log, scope.stream_id, after=None, frames=1))[0]
            raw = frame.split("\n")[0].removeprefix("id: ")
            cursor = EventCursor.decode(raw)
            return cursor.stream_id, cursor.sequence
        finally:
            await engine.dispose()

    stream_id, sequence = asyncio.run(scenario())

    assert stream_id.startswith("ses_")
    assert sequence == 1


def test_the_frame_names_its_event_type(events_dsn: str) -> None:
    async def scenario() -> str:
        log, engine = await _fresh_log(events_dsn)
        try:
            scope = EventScope(stream_id=f"ses_{uuid.uuid4().hex}", run_id="run_1")
            await log.append(scope, _started())
            frame = (await _collect(log, scope.stream_id, after=None, frames=1))[0]
            return frame.split("\n")[1]
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == "event: RunStarted"


def test_a_transient_event_is_never_replayed(events_dsn: str) -> None:
    """Established by the log, not by a filter in the stream.

    ``read`` returns durable events by contract, so the replay has nothing to
    exclude -- the guard in it is unreachable, and removing it fails nothing.
    Said plainly here because a test named for the stream, passing either way,
    would look like coverage the stream does not have.

    Renamed from "never reaches a subscriber", which stopped being true when
    the live channel arrived: a transient event now reaches a subscriber
    *live*, and never through a replay. The distinction is the whole contract
    -- a reconnecting client is given back exactly the durable history -- so a
    name that blurred it would make this test read as a guard against the
    feature beside it. See ``tests/api/test_live_stream.py``.
    """

    async def scenario() -> list[str]:
        log, engine = await _fresh_log(events_dsn)
        try:
            scope = EventScope(stream_id=f"ses_{uuid.uuid4().hex}", run_id="run_1")
            await log.append(scope, ModelDelta(model_call_id="mc_1", text="partial"))
            await log.append(scope, _started())
            frames = await _collect(log, scope.stream_id, after=None, frames=1)
            return [f.split("\n")[1] for f in frames]
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == ["event: RunStarted"]


def test_an_idle_stream_sends_a_heartbeat(events_dsn: str) -> None:
    """A silent socket is what a proxy times out."""

    async def scenario() -> list[str]:
        log, engine = await _fresh_log(events_dsn)
        try:
            scope = EventScope(stream_id=f"ses_{uuid.uuid4().hex}", run_id="run_1")
            return await _collect(log, scope.stream_id, after=None, frames=1)
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == [HEARTBEAT]


def test_a_disconnected_subscriber_stops_the_stream(events_dsn: str) -> None:
    """Otherwise a closed tab keeps a database poll running forever."""

    async def scenario() -> int:
        log, engine = await _fresh_log(events_dsn)
        try:
            scope = EventScope(stream_id=f"ses_{uuid.uuid4().hex}", run_id="run_1")
            await log.append(scope, _started())
        finally:
            await engine.dispose()

        async def gone() -> bool:
            return True

        generator = stream_events(
            log,
            stream_id=scope.stream_id,
            after_sequence=None,
            poll_seconds=1,
            page_size=500,
            heartbeat_seconds=1,
            disconnected=gone,
        )
        return len([frame async for frame in generator])

    assert asyncio.run(scenario()) == 0
