"""In-memory event log.

It exists to make the event contract executable without PostgreSQL, and it
imitates the one behaviour that matters: a durable event receives the next
sequence of its stream while that stream is locked, so positions stay unique
and gap-free per stream. The PostgreSQL implementation replaces the lock with a
row lock on ``run_event_streams``; the observable contract is identical.

Transient events are returned to the caller and dropped. A subscriber that was
not listening at that moment has missed them for good, which is exactly the
guarantee token deltas carry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from agent_workbench.domain.events import (
    EVENT_DURABILITY,
    EventEnvelope,
    EventPayload,
)
from agent_workbench.ports.event_log import (
    EventKey,
    EventKeyConflictError,
    EventScope,
    validate_event_key,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InMemoryEventLog:
    """Append-only log with per-stream sequences, held in process memory."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        event_ids: Callable[[], str] | None = None,
    ) -> None:
        # The clock is injected so recovery and replay tests can produce
        # reproducible timestamps instead of racing the wall clock. Event ids
        # are injected for the same reason: a demo whose transcript is byte
        # identical on every run can be pinned by a golden file.
        self._clock = clock if clock is not None else _utc_now
        self._event_ids = event_ids
        self._streams: dict[str, list[EventEnvelope]] = {}
        self._keyed: dict[tuple[str, str], EventEnvelope] = {}
        self._next_sequence: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def append(
        self,
        scope: EventScope,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        durable = EVENT_DURABILITY[payload.kind] == "durable"
        event_key = validate_event_key(event_key)
        if not durable and event_key is not None:
            raise ValueError("transient events cannot carry an event_key")

        async with self._lock:
            if event_key is not None:
                existing = self._keyed.get((scope.stream_id, event_key))
                if existing is not None:
                    _require_same_event(
                        existing,
                        scope=scope,
                        payload=payload,
                        parent_event_id=parent_event_id,
                    )
                    return existing

            sequence: int | None = None
            if durable:
                sequence = self._next_sequence.get(scope.stream_id, 0) + 1
                self._next_sequence[scope.stream_id] = sequence

            envelope = EventEnvelope.for_payload(
                payload,
                stream_id=scope.stream_id,
                run_id=scope.run_id,
                timestamp=self._clock(),
                sequence=sequence,
                event_id=self._event_ids() if self._event_ids is not None else None,
                task_id=scope.task_id,
                graph_node_id=scope.graph_node_id,
                parent_event_id=parent_event_id,
            )
            if durable:
                self._streams.setdefault(scope.stream_id, []).append(envelope)
                if event_key is not None:
                    self._keyed[(scope.stream_id, event_key)] = envelope

        return envelope

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
        run_id: str | None = None,
    ) -> tuple[EventEnvelope, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")

        async with self._lock:
            stored = tuple(self._streams.get(stream_id, ()))

        if after_sequence is not None:
            stored = tuple(
                envelope
                for envelope in stored
                if envelope.sequence is not None and envelope.sequence > after_sequence
            )
        # Narrowed *before* the limit, which is the whole reason this belongs
        # in the store rather than in a caller. Filtering a page the caller
        # already holds returns "the events of this run that happened to be in
        # the first 500 of the stream", and a delegated run near the end of a
        # long Task is then invisible rather than empty.
        if run_id is not None:
            stored = tuple(envelope for envelope in stored if envelope.run_id == run_id)
        return stored[:limit]


def _require_same_event(
    existing: EventEnvelope,
    *,
    scope: EventScope,
    payload: EventPayload,
    parent_event_id: str | None,
) -> None:
    if (
        existing.stream_id != scope.stream_id
        or existing.run_id != scope.run_id
        or existing.task_id != scope.task_id
        or existing.graph_node_id != scope.graph_node_id
        or existing.payload != payload
        or existing.parent_event_id != parent_event_id
    ):
        raise EventKeyConflictError(
            "event_key already identifies a different durable event"
        )


__all__ = ["InMemoryEventLog"]
