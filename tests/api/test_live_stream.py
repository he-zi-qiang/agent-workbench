"""Transient events on a live subscription, and what they may not disturb.

The durable stream is a record a client can ask for again. The live channel is
not: it carries what is happening *now*, at whatever rate a provider produces
it, and none of it is stored. Everything here is about keeping those two on one
socket without letting the second one damage the first -- no cursor moved by a
frame that cannot be replayed, no unbounded memory held for a reader who has
stopped reading, and no silence where text was dropped.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from agent_workbench.adapters.events import ObservingEventSink, ScopedEventSink
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.apps.api.sse import (
    DEGRADED_EVENT,
    LiveEventChannel,
    LiveSubscription,
    TooManyLiveSubscribersError,
    degraded_frame,
    live_frames,
    stream_events,
)
from agent_workbench.domain.events import (
    EventEnvelope,
    ModelDelta,
    ModelStarted,
    RunStarted,
    ToolProgress,
)
from agent_workbench.domain.runs import RunBudget
from agent_workbench.domain.schema import BOUNDED_TEXT_LIMIT
from agent_workbench.ports.event_log import EventScope

BUDGET = RunBudget(max_steps=4, max_tool_calls=4)
STREAM = "ses_live"


def _started() -> RunStarted:
    return RunStarted(run_kind="chat", model_profile="main", budget=BUDGET)


def _delta(text: str, call: str = "mc_1") -> EventEnvelope:
    return EventEnvelope.for_payload(
        ModelDelta(model_call_id=call, text=text),
        stream_id=STREAM,
        run_id="run_1",
        timestamp=datetime.now(UTC),
    )


def _durable() -> EventEnvelope:
    """A durable envelope, built the way the log builds one: with a position."""

    return EventEnvelope.for_payload(
        _started(),
        stream_id=STREAM,
        run_id="run_1",
        timestamp=datetime.now(UTC),
        sequence=1,
    )


def _channel(*, buffer: int = 64, per_stream: int = 4) -> LiveEventChannel:
    return LiveEventChannel(
        buffer_events=buffer,
        max_subscribers_per_stream=per_stream,
    )


def _open(channel: LiveEventChannel, stream_id: str = STREAM) -> LiveSubscription:
    return channel.subscribe(stream_id)


# --- what the channel forwards, and what it refuses to ------------------------


def test_a_transient_event_reaches_the_stream_s_live_subscriber() -> None:
    channel = _channel()
    subscription = _open(channel)

    channel.observe(_delta("hello"))

    taken, dropped = subscription.drain()
    assert dropped == 0
    assert [e.payload.text for e in taken] == ["hello"]  # pyright: ignore[reportAttributeAccessIssue]


def test_a_durable_event_is_not_forwarded_live() -> None:
    """It is already on its way, with the position a reconnect resumes from.

    The control is the other half: the same channel, the same subscriber, a
    transient event *does* arrive. Without it this test would also pass against
    a channel that forwards nothing at all.
    """

    channel = _channel()
    subscription = _open(channel)

    channel.observe(_durable())
    assert subscription.drain() == ((), 0)

    channel.observe(_delta("live"))
    taken, _ = subscription.drain()
    assert len(taken) == 1


def test_a_transient_event_for_another_stream_is_not_forwarded() -> None:
    channel = _channel()
    subscription = _open(channel, "ses_mine")

    channel.observe(_delta("not yours"))  # built for STREAM

    assert subscription.drain() == ((), 0)


def test_the_sink_every_run_writes_through_tees_into_the_channel() -> None:
    """The wiring the API assembles, exercised end to end at sink level."""

    async def scenario() -> tuple[int, int]:
        log = InMemoryEventLog()
        channel = _channel()
        subscription = _open(channel)
        sink = ObservingEventSink(
            inner=ScopedEventSink(
                log=log,
                scope=EventScope(stream_id=STREAM, run_id="run_1"),
            ),
            observer=channel.observe,
        )
        await sink.emit(_started())
        await sink.emit(ModelDelta(model_call_id="mc_1", text="partial"))
        taken, _ = subscription.drain()
        stored = await log.read(STREAM)
        return len(taken), len(stored)

    live, stored = asyncio.run(scenario())

    # One of each went in: the durable one is stored and not live, the
    # transient one is live and not stored.
    assert (live, stored) == (1, 1)


# --- the buffer, and what overflowing it says ---------------------------------


def test_overflow_drops_the_oldest_and_counts_it() -> None:
    channel = _channel(buffer=3)
    subscription = _open(channel)

    for index in range(5):
        channel.observe(_delta(str(index)))

    taken, dropped = subscription.drain()
    assert dropped == 2
    assert [e.payload.text for e in taken] == ["2", "3", "4"]  # pyright: ignore[reportAttributeAccessIssue]


def test_a_drained_subscription_starts_counting_again() -> None:
    """The count belongs to the gap it describes, not to the connection."""

    channel = _channel(buffer=2)
    subscription = _open(channel)
    for index in range(4):
        channel.observe(_delta(str(index)))
    subscription.drain()

    channel.observe(_delta("after"))

    taken, dropped = subscription.drain()
    assert dropped == 0
    assert [e.payload.text for e in taken] == ["after"]  # pyright: ignore[reportAttributeAccessIssue]


# --- how many subscribers one stream may have ---------------------------------


def test_a_stream_refuses_more_live_subscribers_than_it_may_hold() -> None:
    channel = _channel(per_stream=2)
    _open(channel)
    _open(channel)

    with pytest.raises(TooManyLiveSubscribersError):
        _open(channel)


def test_closing_a_subscription_frees_its_place() -> None:
    """Otherwise a reopened tab is refused for as long as the process lives."""

    channel = _channel(per_stream=1)
    first = _open(channel)
    first.close()

    second = _open(channel)  # would raise if the place were still taken

    assert channel.subscriber_count(STREAM) == 1
    second.close()
    assert channel.subscriber_count(STREAM) == 0


def test_another_stream_has_its_own_ceiling() -> None:
    channel = _channel(per_stream=1)
    _open(channel, "ses_a")

    _open(channel, "ses_b")  # a full stream must not close a different one

    assert channel.subscriber_count("ses_a") == 1
    assert channel.subscriber_count("ses_b") == 1


# --- frames: no id, and no silent trimming ------------------------------------


def test_a_live_frame_carries_no_id_line() -> None:
    """An id would move ``Last-Event-ID`` to a position replay cannot serve."""

    frame = next(iter(live_frames((_delta("hi"),))))

    assert not frame.startswith("id:")
    assert "\nid: " not in frame
    assert frame.startswith("event: ModelDelta\n")


def test_a_live_frame_says_it_is_transient_and_has_no_position() -> None:
    frame = next(iter(live_frames((_delta("hi"),))))

    assert '"durability":"transient"' in frame
    assert '"sequence":null' in frame


def test_a_degraded_frame_carries_no_id_line() -> None:
    frame = degraded_frame(3)

    assert not frame.startswith("id:")
    assert "\nid: " not in frame
    assert frame.startswith(f"event: {DEGRADED_EVENT}\n")
    assert '"dropped_events": 3' in frame


def test_adjacent_deltas_of_one_call_become_one_frame() -> None:
    frames = list(live_frames((_delta("he"), _delta("llo"), _delta("!"))))

    assert len(frames) == 1
    assert '"text":"hello!"' in frames[0]


def test_deltas_of_different_calls_do_not_merge() -> None:
    """A tool round between two model calls would otherwise vanish."""

    frames = list(live_frames((_delta("a", "mc_1"), _delta("b", "mc_2"))))

    assert len(frames) == 2
    assert '"model_call_id":"mc_1"' in frames[0]
    assert '"model_call_id":"mc_2"' in frames[1]


def test_a_non_delta_transient_event_passes_through_in_order() -> None:
    progress = EventEnvelope.for_payload(
        ToolProgress(tool_call_id="tc_1", message="reading"),
        stream_id=STREAM,
        run_id="run_1",
        timestamp=datetime.now(UTC),
    )

    frames = list(live_frames((_delta("before"), progress, _delta("after"))))

    assert [f.split("\n")[0] for f in frames] == [
        "event: ModelDelta",
        "event: ToolProgress",
        "event: ModelDelta",
    ]


def test_a_merge_too_large_for_one_frame_is_split_rather_than_trimmed() -> None:
    """``ModelDelta.text`` is bounded, and text is what the frame is for.

    Trimming would produce a frame that looks complete and is not, which the
    subscriber has no way to detect. Splitting is visible in the only way that
    matters: the pieces still concatenate to exactly what was produced.
    """

    piece = "x" * (BOUNDED_TEXT_LIMIT // 2 + 100)
    frames = list(live_frames((_delta(piece), _delta(piece), _delta(piece))))

    assert len(frames) == 3
    assert "".join(_text_of(frame) for frame in frames) == piece * 3


def _text_of(frame: str) -> str:
    body = frame.split("data: ", 1)[1].rstrip("\n")
    payload = json.loads(body)["payload"]
    text = payload["text"]
    assert isinstance(text, str)
    return text


# --- the stream itself: live frames must not disturb the durable cursor -------


async def _collect(
    log: InMemoryEventLog,
    live: LiveSubscription,
    *,
    frames: int,
) -> list[str]:
    out: list[str] = []
    generator = stream_events(
        log,
        stream_id=STREAM,
        after_sequence=None,
        poll_seconds=1,
        page_size=500,
        # Far above the poll interval, so a heartbeat never interleaves with
        # what these tests are actually about.
        heartbeat_seconds=600,
        disconnected=None,
        live=live,
        coalesce_seconds=0.01,
    )

    async def take() -> None:
        async for frame in generator:
            out.append(frame)
            if len(out) >= frames:
                return

    try:
        # Bounded, because the failure mode this guards against is a frame that
        # never arrives: without it the loop waits for the heartbeat interval
        # and the suite looks hung rather than red.
        await asyncio.wait_for(take(), timeout=10)
    finally:
        await generator.aclose()
    return out


def test_a_live_frame_between_two_durable_ones_does_not_move_the_cursor() -> None:
    """The durable ids stay consecutive across any number of live frames."""

    async def scenario() -> list[str]:
        log = InMemoryEventLog()
        channel = _channel()
        subscription = _open(channel)
        scope = EventScope(stream_id=STREAM, run_id="run_1")
        await log.append(scope, _started())

        async def produce() -> None:
            await asyncio.sleep(0.05)
            channel.observe(_delta("live text"))
            await asyncio.sleep(0.05)
            await log.append(
                scope,
                ModelStarted(model_call_id="mc_1", model_profile="main", model_id="m"),
            )

        producer = asyncio.create_task(produce())
        try:
            frames = await _collect(log, subscription, frames=3)
        finally:
            await producer
        return frames

    frames = asyncio.run(scenario())

    first, live, second = frames
    assert first.startswith(f"id: {STREAM}:1\n")
    assert not live.startswith("id:")
    assert live.startswith("event: ModelDelta\n")
    assert second.startswith(f"id: {STREAM}:2\n")


def test_the_generator_releases_its_subscription_when_it_ends() -> None:
    """Otherwise every dropped connection costs a place until restart."""

    async def scenario() -> int:
        log = InMemoryEventLog()
        channel = _channel(per_stream=1)
        subscription = _open(channel)
        channel.observe(_delta("one"))
        await _collect(log, subscription, frames=1)
        return channel.subscriber_count(STREAM)

    assert asyncio.run(scenario()) == 0


def test_a_dropped_burst_is_announced_before_the_events_it_made_room_for() -> None:
    async def scenario() -> list[str]:
        log = InMemoryEventLog()
        channel = _channel(buffer=2)
        subscription = _open(channel)
        for index in range(5):
            channel.observe(_delta(str(index)))
        return await _collect(log, subscription, frames=2)

    frames = asyncio.run(scenario())

    assert frames[0].startswith(f"event: {DEGRADED_EVENT}\n")
    assert '"dropped_events": 3' in frames[0]
    # The survivors, merged: the gap is in front of them, not inside them.
    assert '"text":"34"' in frames[1]


# --- the route: a refusal is a status code, not a truncated stream ------------


class _StubPrincipal:
    tenant_id = "tenant_1"
    principal_id = "principal_1"


class _StubPrincipals:
    def resolve(self, request: object) -> _StubPrincipal:
        return _StubPrincipal()


class _StubConversations:
    async def history(self, **_: object) -> tuple[()]:
        """Authorization, which this route does before anything else."""

        return ()


class _CountingLog(InMemoryEventLog):
    """A log that records whether the stream ever asked it for a page."""

    reads: int = 0

    async def read(self, stream_id: str, **kwargs: object) -> tuple[EventEnvelope, ...]:
        type(self).reads += 1
        return await super().read(stream_id, **kwargs)  # pyright: ignore[reportArgumentType]


def _stub_dependencies(channel: LiveEventChannel, log: InMemoryEventLog) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        principals=_StubPrincipals(),
        chat=SimpleNamespace(conversations=_StubConversations()),
        events=log,
        live_events=channel,
        config=SimpleNamespace(
            sse_heartbeat_seconds=600,
            event_stream=SimpleNamespace(
                catchup_poll_seconds=1,
                replay_page_size=500,
                live_delta_coalesce_ms=10,
            ),
        ),
    )


def _app(channel: LiveEventChannel, log: InMemoryEventLog) -> object:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from agent_workbench.apps.api.main import ERROR_STATUS
    from agent_workbench.apps.api.routes import events as events_route
    from agent_workbench.apps.api.state import STATE_ATTRIBUTE

    app = FastAPI()
    app.include_router(events_route.router)
    setattr(app.state, STATE_ATTRIBUTE, _stub_dependencies(channel, log))

    # Taken from the real mapping rather than restated: a test that hard-coded
    # 429 here would keep passing after somebody removed the entry from main.
    status = ERROR_STATUS[TooManyLiveSubscribersError]

    def refuse(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    app.add_exception_handler(TooManyLiveSubscribersError, refuse)  # pyright: ignore[reportArgumentType]
    return app


def test_a_stream_at_its_subscriber_ceiling_refuses_before_it_streams() -> None:
    """429 rather than a stream that opens and then delivers nothing.

    Two assertions carry this, and neither is the status code alone. The read
    counter says the refusal happened *before* the generator ran -- a refusal
    from inside it would already have paged the log, and the client would be
    holding a 200 that never explains why it is empty. The subscriber count
    says the refused request took nothing with it, so a client that retries
    after a real subscriber leaves is not refused forever.

    The success path is deliberately not exercised here. ``ASGITransport``
    buffers a response body whole, and this stream never ends -- a 200 case
    would hang rather than assert. What the route does when there is room is
    covered above, against the generator itself.
    """

    import httpx

    async def scenario() -> tuple[int, int, int]:
        channel = _channel(per_stream=1)
        log = _CountingLog()
        _CountingLog.reads = 0
        held = channel.subscribe("ses_taken")  # the one place this stream has
        transport = httpx.ASGITransport(app=_app(channel, log))  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            refused = await client.get("/v1/chat/sessions/ses_taken/events")
        still_held = channel.subscriber_count("ses_taken")
        held.close()
        return refused.status_code, still_held, _CountingLog.reads

    refused, still_held, reads = asyncio.run(scenario())

    assert refused == 429
    assert still_held == 1
    assert reads == 0
