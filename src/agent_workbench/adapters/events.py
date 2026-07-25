"""Binding an event log to one unit of work.

The runtime should not repeat which stream, run, task and node it belongs to on
every emit; it holds a sink that already knows. Keeping the scope here rather
than inside a log implementation means every log gets the same behaviour for
free.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agent_workbench.domain.events import EventEnvelope, EventPayload
from agent_workbench.ports.event_log import EventLogPort, EventScope, EventSink


@dataclass(frozen=True, slots=True)
class ScopedEventSink:
    """An ``EventSink`` that appends into one fixed scope."""

    log: EventLogPort
    scope: EventScope

    async def emit(
        self,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
    ) -> EventEnvelope:
        return await self.log.append(
            self.scope,
            payload,
            parent_event_id=parent_event_id,
        )


@dataclass(frozen=True, slots=True)
class ObservingEventSink:
    """Reports every emitted envelope to a live observer, then delegates.

    A live subscriber has to see transient events, and those never reach the
    durable log by definition. The sink is the single point both kinds pass
    through, so tee-ing here is what lets a terminal or an SSE connection show
    token deltas while replay still returns only what was persisted.
    """

    inner: EventSink
    observer: Callable[[EventEnvelope], None]

    async def emit(
        self,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
    ) -> EventEnvelope:
        envelope = await self.inner.emit(payload, parent_event_id=parent_event_id)
        self.observer(envelope)
        return envelope


__all__ = ["ObservingEventSink", "ScopedEventSink"]
