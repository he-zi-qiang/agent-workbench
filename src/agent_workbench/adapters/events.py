"""Binding an event log to one unit of work.

The runtime should not repeat which stream, run, task and node it belongs to on
every emit; it holds a sink that already knows. Keeping the scope here rather
than inside a log implementation means every log gets the same behaviour for
free.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.domain.events import EventEnvelope, EventPayload
from agent_workbench.ports.event_log import EventLogPort, EventScope


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


__all__ = ["ScopedEventSink"]
