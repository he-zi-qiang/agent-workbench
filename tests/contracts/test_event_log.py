"""Contract for the event log: sequences, replay and what is never stored."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.domain.errors import IncompatibleSchemaError
from agent_workbench.domain.events import (
    ModelDelta,
    RunCompleted,
    RunStarted,
    ToolProgress,
    ToolStarted,
)
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventCursor, EventScope

SCOPE = EventScope(stream_id="stream_1", run_id="run_1")
OTHER_SCOPE = EventScope(stream_id="stream_2", run_id="run_2")
BUDGET = RunBudget(max_steps=4, max_tool_calls=8)


def _fixed_clock() -> datetime:
    return datetime(2026, 7, 25, 3, 14, 15, tzinfo=UTC)


def _started() -> RunStarted:
    return RunStarted(run_kind="chat", model_profile="main", budget=BUDGET)


def test_durable_events_receive_a_gap_free_sequence_per_stream() -> None:
    async def scenario() -> list[int | None]:
        log = InMemoryEventLog(clock=_fixed_clock)
        first = await log.append(SCOPE, _started())
        second = await log.append(SCOPE, ToolStarted(tool_call_id="t1", tool_name="x"))
        third = await log.append(SCOPE, RunCompleted(stop_reason="completed"))
        return [first.sequence, second.sequence, third.sequence]

    assert asyncio.run(scenario()) == [1, 2, 3]


def test_streams_are_numbered_independently() -> None:
    async def scenario() -> tuple[int | None, int | None]:
        log = InMemoryEventLog(clock=_fixed_clock)
        await log.append(SCOPE, _started())
        await log.append(SCOPE, RunCompleted(stop_reason="completed"))
        mine = await log.append(SCOPE, ToolStarted(tool_call_id="t1", tool_name="x"))
        theirs = await log.append(OTHER_SCOPE, _started())
        return mine.sequence, theirs.sequence

    assert asyncio.run(scenario()) == (3, 1)


def test_transient_events_are_returned_but_never_stored() -> None:
    """Token deltas stream to whoever is listening and are then gone."""

    async def scenario() -> tuple[int | None, int]:
        log = InMemoryEventLog(clock=_fixed_clock)
        await log.append(SCOPE, _started())
        delta = await log.append(SCOPE, ModelDelta(model_call_id="mc_1", text="hi"))
        await log.append(SCOPE, ToolProgress(tool_call_id="t1", message="half"))
        replayed = await log.read(SCOPE.stream_id)
        return delta.sequence, len(replayed)

    sequence, stored = asyncio.run(scenario())

    assert sequence is None
    assert stored == 1


def test_transient_events_do_not_consume_a_sequence() -> None:
    async def scenario() -> int | None:
        log = InMemoryEventLog(clock=_fixed_clock)
        await log.append(SCOPE, _started())
        await log.append(SCOPE, ModelDelta(model_call_id="mc_1", text="hi"))
        return (await log.append(SCOPE, RunCompleted(stop_reason="completed"))).sequence

    assert asyncio.run(scenario()) == 2


def test_replay_resumes_after_a_cursor() -> None:
    """This is the reconnect path: everything the subscriber missed, in order."""

    async def scenario() -> list[int | None]:
        log = InMemoryEventLog(clock=_fixed_clock)
        await log.append(SCOPE, _started())
        await log.append(SCOPE, ToolStarted(tool_call_id="t1", tool_name="x"))
        await log.append(SCOPE, RunCompleted(stop_reason="completed"))
        missed = await log.read(SCOPE.stream_id, after_sequence=1)
        return [envelope.sequence for envelope in missed]

    assert asyncio.run(scenario()) == [2, 3]


def test_replay_respects_its_page_size() -> None:
    async def scenario() -> int:
        log = InMemoryEventLog(clock=_fixed_clock)
        for _ in range(5):
            await log.append(SCOPE, ToolStarted(tool_call_id="t1", tool_name="x"))
        return len(await log.read(SCOPE.stream_id, limit=2))

    assert asyncio.run(scenario()) == 2


def test_an_unknown_stream_replays_as_empty() -> None:
    async def scenario() -> tuple[()] | tuple[object, ...]:
        log = InMemoryEventLog(clock=_fixed_clock)
        return await log.read("stream_never_used")

    assert asyncio.run(scenario()) == ()


def test_the_clock_is_injected_so_timelines_stay_reproducible() -> None:
    moments = iter(
        [
            datetime(2026, 7, 25, 3, 0, tzinfo=UTC),
            datetime(2026, 7, 25, 3, 0, 30, tzinfo=UTC),
        ]
    )

    async def scenario() -> timedelta:
        log = InMemoryEventLog(clock=lambda: next(moments))
        first = await log.append(SCOPE, _started())
        second = await log.append(SCOPE, RunCompleted(stop_reason="completed"))
        return second.timestamp - first.timestamp

    assert asyncio.run(scenario()) == timedelta(seconds=30)


def test_a_scoped_sink_stamps_the_same_scope_on_every_event() -> None:
    async def scenario() -> tuple[str, str, str | None]:
        log = InMemoryEventLog(clock=_fixed_clock)
        scope = EventScope(
            stream_id="stream_1",
            run_id="run_1",
            task_id="task_1",
            graph_node_id="node_plan",
        )
        sink = ScopedEventSink(log=log, scope=scope)
        envelope = await sink.emit(_started())
        return envelope.stream_id, envelope.run_id, envelope.graph_node_id

    assert asyncio.run(scenario()) == ("stream_1", "run_1", "node_plan")


def test_parent_event_ids_chain_related_events() -> None:
    async def scenario() -> bool:
        log = InMemoryEventLog(clock=_fixed_clock)
        parent = await log.append(SCOPE, _started())
        child = await log.append(
            SCOPE,
            ToolStarted(tool_call_id="t1", tool_name="x"),
            parent_event_id=parent.event_id,
        )
        return child.parent_event_id == parent.event_id

    assert asyncio.run(scenario()) is True


def test_cursors_round_trip_through_their_wire_form() -> None:
    cursor = EventCursor(stream_id="stream_1", sequence=42)

    assert cursor.encode() == "stream_1:42"
    assert EventCursor.decode(cursor.encode()) == cursor


@pytest.mark.parametrize("raw", ["stream_1", "stream_1:", ":42", "stream_1:abc", ""])
def test_a_malformed_cursor_fails_closed(raw: str) -> None:
    """A client-supplied cursor is untrusted input like any other."""

    with pytest.raises(IncompatibleSchemaError):
        EventCursor.decode(raw)
