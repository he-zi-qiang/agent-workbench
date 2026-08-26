"""The event boundary: append, replay and the SSE cursor.

The log assigns sequences; producers do not. A durable event gets its position
when it is appended under its stream's row, which is what makes
``(stream_id, sequence)`` a cursor a client can resume from. Transient events
pass through to live subscribers and are never stored, so they never receive a
position they could not be replayed from.
"""

from __future__ import annotations

from typing import Annotated, Final, Protocol, runtime_checkable

from pydantic import Field, StringConstraints, ValidationError

from agent_workbench.domain.errors import IncompatibleSchemaError
from agent_workbench.domain.events import EventEnvelope, EventPayload
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel

CURSOR_SEPARATOR = ":"
# Enough for any 64-bit sequence; a longer run of digits is someone probing,
# not a position this log ever handed out.
MAX_SEQUENCE_DIGITS = 19
EVENT_KEY_MAX_LENGTH: Final[int] = 128

EventKey = Annotated[
    str,
    StringConstraints(min_length=1, max_length=EVENT_KEY_MAX_LENGTH),
]


class EventKeyConflictError(ValueError):
    """One stream-local idempotency key was reused for different content."""


def validate_event_key(event_key: object | None) -> EventKey | None:
    """Apply the runtime boundary that a static annotation cannot enforce."""

    if event_key is None:
        return None
    if (
        not isinstance(event_key, str)
        or not 1 <= len(event_key) <= EVENT_KEY_MAX_LENGTH
    ):
        raise ValueError(
            f"event_key must contain between 1 and {EVENT_KEY_MAX_LENGTH} characters"
        )
    return event_key


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
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        """Record one event and return the envelope that was produced.

        Durable payloads receive the next sequence of their stream. Transient
        payloads are returned without a sequence and without being stored, so
        they cannot carry an idempotency key.

        A durable ``event_key`` is unique within its stream. Repeating the key
        with the same scope, payload and parent returns the original envelope;
        reusing it for different content fails closed.
        """
        ...

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
        run_id: str | None = None,
    ) -> tuple[EventEnvelope, ...]:
        """Replay durable events of one stream in sequence order.

        ``run_id`` narrows the page to one run inside that stream. It is an
        optional narrowing rather than a second method because the cursor
        semantics have to be identical either way: ``after_sequence`` is a
        position in the *stream*, not an index into the filtered result, so a
        client can hold one cursor and change its mind about the filter.

        Several runs per stream is not new -- a Chat stream is a session and
        each turn is a run -- but until delegation there was never a reason to
        ask for one of them. Now there is: a delegated run writes into its
        parent's stream (ADR-082), so "show me only what this sub-agent did" is
        a question with an answer, and answering it by filtering a page the
        client already pulled misses everything past the page.
        """
        ...


@runtime_checkable
class EventSink(Protocol):
    """The narrow view the runtime holds: emit into an already-known scope."""

    async def emit(
        self,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
        event_key: EventKey | None = None,
    ) -> EventEnvelope: ...


__all__ = [
    "CURSOR_SEPARATOR",
    "EVENT_KEY_MAX_LENGTH",
    "MAX_SEQUENCE_DIGITS",
    "EventCursor",
    "EventKey",
    "EventKeyConflictError",
    "EventLogPort",
    "EventScope",
    "EventSink",
    "validate_event_key",
]
