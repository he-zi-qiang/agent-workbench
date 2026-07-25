"""The event boundary: append, replay and the SSE cursor.

The log assigns sequences; producers do not. A durable event gets its position
when it is appended under its stream's row, which is what makes
``(stream_id, sequence)`` a cursor a client can resume from. Transient events
pass through to live subscribers and are never stored, so they never receive a
position they could not be replayed from.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field, ValidationError

from agent_workbench.domain.errors import IncompatibleSchemaError
from agent_workbench.domain.events import EventEnvelope, EventPayload
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel

CURSOR_SEPARATOR = ":"
# Enough for any 64-bit sequence; a longer run of digits is someone probing,
# not a position this log ever handed out.
MAX_SEQUENCE_DIGITS = 19


class EventScope(DomainModel):
    """Where the events of one unit of work belong."""

    stream_id: Identifier
    run_id: Identifier
    task_id: Identifier | None = None
    graph_node_id: Identifier | None = None


class EventCursor(DomainModel):
    """A subscriber's position in one stream.

    Encoded into the SSE event id, so a reconnecting client sends it back as
    ``Last-Event-ID`` and receives every durable event it missed.
    """

    stream_id: Identifier
    sequence: int = Field(ge=1)

    def encode(self) -> str:
        return f"{self.stream_id}{CURSOR_SEPARATOR}{self.sequence}"

    @classmethod
    def decode(cls, raw: str) -> EventCursor:
        """Parse a client-supplied cursor, failing closed on anything odd.

        Every rejection raises the same error with the same text. A cursor
        arrives from a browser over ``Last-Event-ID``, so a decoder that
        explained *why* it refused would describe the stream namespace to
        whoever guessed at it.
        """

        stream_id, separator, sequence = raw.partition(CURSOR_SEPARATOR)
        if (
            not separator
            or not sequence.isdigit()
            or len(sequence) > MAX_SEQUENCE_DIGITS
        ):
            raise IncompatibleSchemaError("malformed event cursor")
        try:
            return cls(stream_id=stream_id, sequence=int(sequence))
        except ValidationError as exc:
            raise IncompatibleSchemaError("malformed event cursor") from exc


@runtime_checkable
class EventLogPort(Protocol):
    """Append-only event store with per-stream ordering."""

    async def append(
        self,
        scope: EventScope,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
    ) -> EventEnvelope:
        """Record one event and return the envelope that was produced.

        Durable payloads receive the next sequence of their stream. Transient
        payloads are returned without a sequence and without being stored.
        """
        ...

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
    ) -> tuple[EventEnvelope, ...]:
        """Replay durable events of one stream in sequence order."""
        ...


@runtime_checkable
class EventSink(Protocol):
    """The narrow view the runtime holds: emit into an already-known scope."""

    async def emit(
        self,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
    ) -> EventEnvelope: ...


__all__ = [
    "CURSOR_SEPARATOR",
    "MAX_SEQUENCE_DIGITS",
    "EventCursor",
    "EventLogPort",
    "EventScope",
    "EventSink",
]
