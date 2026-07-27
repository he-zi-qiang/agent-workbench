"""The event log contract, against both implementations.

The in-memory log made this executable. The PostgreSQL one has to keep it under
concurrency, and gap-free sequencing is the property that only appears there:
an identity column would give positions that are unique and full of holes,
because a rolled-back transaction consumes a value it never writes -- and a
subscriber resuming from a cursor cannot tell a hole from an event that has not
arrived yet.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from harness import StoreHarness

from agent_workbench.domain.events import ModelDelta, RunStarted
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventScope

SCOPE = EventScope(stream_id="str_events_a", run_id="run_1")
OTHER = EventScope(stream_id="str_events_b", run_id="run_2")
BUDGET = RunBudget(max_steps=4, max_tool_calls=4)


def _started() -> RunStarted:
    return RunStarted(run_kind="chat", model_profile="main", budget=BUDGET)


def test_durable_events_are_numbered_from_one(event_logs: StoreHarness) -> None:
    async def scenario(log: Any) -> list[int]:
        return [(await log.append(SCOPE, _started())).sequence for _ in range(3)]

    assert event_logs.run(scenario) == [1, 2, 3]


def test_sequences_have_no_gaps_under_concurrent_appends(
    event_logs: StoreHarness,
) -> None:
    """Unique is not enough. A hole is indistinguishable from a missing event."""

    async def scenario(log: Any) -> list[int]:
        envelopes = await asyncio.gather(
            *(log.append(SCOPE, _started()) for _ in range(8))
        )
        return sorted(envelope.sequence for envelope in envelopes)

    assert event_logs.run(scenario) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_streams_are_numbered_independently(event_logs: StoreHarness) -> None:
    async def scenario(log: Any) -> tuple[int, int]:
        first = await log.append(SCOPE, _started())
        second = await log.append(OTHER, _started())
        return first.sequence, second.sequence

    assert event_logs.run(scenario) == (1, 1)


def test_a_transient_event_is_not_stored_and_has_no_position(
    event_logs: StoreHarness,
) -> None:
    """A position it could not be replayed from would make the cursor lie."""

    async def scenario(log: Any) -> tuple[Any, int]:
        transient = await log.append(
            SCOPE, ModelDelta(model_call_id="mc_1", text="partial")
        )
        replayed = await log.read(SCOPE.stream_id)
        return transient.sequence, len(replayed)

    assert event_logs.run(scenario) == (None, 0)


def test_replay_returns_events_in_order(event_logs: StoreHarness) -> None:
    async def scenario(log: Any) -> list[int]:
        for _ in range(4):
            await log.append(SCOPE, _started())
        return [e.sequence for e in await log.read(SCOPE.stream_id)]

    assert event_logs.run(scenario) == [1, 2, 3, 4]


def test_replay_resumes_after_a_cursor(event_logs: StoreHarness) -> None:
    """What a reconnecting subscriber sends back as Last-Event-ID."""

    async def scenario(log: Any) -> list[int]:
        for _ in range(5):
            await log.append(SCOPE, _started())
        resumed = await log.read(SCOPE.stream_id, after_sequence=2)
        return [e.sequence for e in resumed]

    assert event_logs.run(scenario) == [3, 4, 5]


def test_replay_does_not_cross_streams(event_logs: StoreHarness) -> None:
    async def scenario(log: Any) -> int:
        await log.append(SCOPE, _started())
        await log.append(OTHER, _started())
        return len(await log.read(SCOPE.stream_id))

    assert event_logs.run(scenario) == 1


def test_a_replayed_event_carries_its_payload_back(event_logs: StoreHarness) -> None:
    """Read through the model that wrote it, so an unknown row fails closed."""

    async def scenario(log: Any) -> tuple[str, Any]:
        await log.append(SCOPE, _started())
        replayed = (await log.read(SCOPE.stream_id))[0]
        return replayed.event_type, replayed.payload.run_kind

    assert event_logs.run(scenario) == ("RunStarted", "chat")


def test_a_non_positive_limit_is_refused(event_logs: StoreHarness) -> None:
    async def scenario(log: Any) -> None:
        await log.read(SCOPE.stream_id, limit=0)

    with pytest.raises(ValueError, match="limit"):
        event_logs.run(scenario)
