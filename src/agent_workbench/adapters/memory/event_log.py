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
from agent_workbench.ports.event_log import EventScope


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
        self._next_sequence: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def append(
        self,
        scope: EventScope,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
    ) -> EventEnvelope:
        durable = EVENT_DURABILITY[payload.kind] == "durable"

        async with self._lock:
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

        return envelope

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
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
        return stored[:limit]


__all__ = ["InMemoryEventLog"]
