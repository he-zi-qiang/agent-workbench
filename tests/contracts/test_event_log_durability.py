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

from agent_workbench.adapters.events import ObservingEventSink, ScopedEventSink
from agent_workbench.domain.events import ModelDelta, RunCompleted, RunStarted
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventKeyConflictError, EventScope

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


def test_repeating_a_durable_event_key_returns_the_original_envelope(
    event_logs: StoreHarness,
) -> None:
    async def scenario(log: Any) -> tuple[bool, bool, int, int, list[int]]:
        first = await log.append(SCOPE, _started(), event_key="run-started")
        repeated = await log.append(SCOPE, _started(), event_key="run-started")
        following = await log.append(SCOPE, RunCompleted(stop_reason="completed"))
        replayed = await log.read(SCOPE.stream_id)
        return (
            repeated == first,
            repeated.event_id == first.event_id,
            repeated.sequence,
            following.sequence,
            [event.sequence for event in replayed],
        )

    assert event_logs.run(scenario) == (True, True, 1, 2, [1, 2])


@pytest.mark.parametrize("changed_field", ["scope", "payload", "parent"])
def test_reusing_an_event_key_for_different_content_fails_without_a_sequence_hole(
    event_logs: StoreHarness,
    changed_field: str,
) -> None:
    async def scenario(log: Any) -> tuple[int, list[int]]:
        await log.append(
            SCOPE,
            _started(),
            parent_event_id="evt_parent",
            event_key="one-logical-event",
        )
        scope = SCOPE
        payload = _started()
        parent_event_id = "evt_parent"
        if changed_field == "scope":
            scope = EventScope(stream_id=SCOPE.stream_id, run_id="run_changed")
        elif changed_field == "payload":
            payload = RunCompleted(stop_reason="completed")
        else:
            parent_event_id = "evt_changed"

        with pytest.raises(EventKeyConflictError):
            await log.append(
                scope,
                payload,
                parent_event_id=parent_event_id,
                event_key="one-logical-event",
            )

        following = await log.append(
            SCOPE,
            RunCompleted(stop_reason="completed"),
        )
        replayed = await log.read(SCOPE.stream_id)
        return following.sequence, [event.sequence for event in replayed]

    assert event_logs.run(scenario) == (2, [1, 2])


def test_concurrent_appends_of_one_event_key_create_one_event(
    event_logs: StoreHarness,
) -> None:
    async def scenario(log: Any) -> tuple[int, int, set[int], int, list[int]]:
        envelopes = await asyncio.gather(
            *(
                log.append(SCOPE, _started(), event_key="concurrent-start")
                for _ in range(8)
            )
        )
        following = await log.append(SCOPE, RunCompleted(stop_reason="completed"))
        replayed = await log.read(SCOPE.stream_id)
        return (
            len(replayed),
            len({event.event_id for event in envelopes}),
            {event.sequence for event in envelopes},
            following.sequence,
            [event.sequence for event in replayed],
        )

    assert event_logs.run(scenario) == (2, 1, {1}, 2, [1, 2])


def test_the_same_event_key_is_independent_across_streams(
    event_logs: StoreHarness,
) -> None:
    async def scenario(log: Any) -> tuple[bool, int, int, int, int]:
        first = await log.append(SCOPE, _started(), event_key="shared-key")
        second = await log.append(OTHER, _started(), event_key="shared-key")
        return (
            first.event_id != second.event_id,
            first.sequence,
            second.sequence,
            len(await log.read(SCOPE.stream_id)),
            len(await log.read(OTHER.stream_id)),
        )

    assert event_logs.run(scenario) == (True, 1, 1, 1, 1)


@pytest.mark.parametrize("event_key", ["", "x" * 129])
def test_event_key_length_is_enforced(
    event_logs: StoreHarness,
    event_key: str,
) -> None:
    async def scenario(log: Any) -> int:
        with pytest.raises(ValueError, match="event_key"):
            await log.append(SCOPE, _started(), event_key=event_key)
        return (await log.append(SCOPE, _started())).sequence

    assert event_logs.run(scenario) == 1


def test_a_128_character_event_key_is_accepted(event_logs: StoreHarness) -> None:
    async def scenario(log: Any) -> tuple[int, bool]:
        first = await log.append(SCOPE, _started(), event_key="x" * 128)
        repeated = await log.append(SCOPE, _started(), event_key="x" * 128)
        return first.sequence, repeated.event_id == first.event_id

    assert event_logs.run(scenario) == (1, True)


def test_a_transient_event_key_is_rejected_without_consuming_a_sequence(
    event_logs: StoreHarness,
) -> None:
    async def scenario(log: Any) -> int:
        with pytest.raises(ValueError, match="transient"):
            await log.append(
                SCOPE,
                ModelDelta(model_call_id="mc_1", text="partial"),
                event_key="invalid-transient-key",
            )
        return (await log.append(SCOPE, _started())).sequence

    assert event_logs.run(scenario) == 1


def test_scoped_and_observing_sinks_forward_the_event_key(
    event_logs: StoreHarness,
) -> None:
    async def scenario(log: Any) -> tuple[bool, list[str], int]:
        observed: list[Any] = []
        sink = ObservingEventSink(
            inner=ScopedEventSink(log=log, scope=SCOPE),
            observer=observed.append,
        )
        first = await sink.emit(_started(), event_key="sink-key")
        repeated = await sink.emit(_started(), event_key="sink-key")
        return (
            repeated.event_id == first.event_id,
            [event.event_id for event in observed],
            len(await log.read(SCOPE.stream_id)),
        )

    same, observed_ids, stored = event_logs.run(scenario)
    assert same is True
    assert observed_ids[0] == observed_ids[1]
    assert stored == 1
