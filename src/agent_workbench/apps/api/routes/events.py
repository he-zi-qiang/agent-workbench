"""Subscribing to a session's events over SSE, and resuming after a drop.

A subscription is a replay that has not finished yet. The client sends the last
id it saw, the server sends everything after it, and then keeps sending. That
is the same operation twice, which is why there is no separate "catch up" path:
a reconnect is just a subscription starting further along.

The cursor is the SSE event id, so a browser resumes with ``Last-Event-ID``
without the client writing any resumption logic of its own. It only means
anything because sequences are gap-free -- a subscriber cannot tell a hole from
an event that has not arrived, so a log with holes would make "everything after
n" unanswerable.

Only durable events are replayable, and only durable events are sent here. A
transient one has no position, so a client that reconnected would have no way
to ask for it again and no way to know it had missed it.

One stored row this process cannot decode used to end the subscription: the
strict replay raises, and the whole session becomes unreachable behind a single
damaged event. Where the log offers the isolating replay this stream uses it
instead -- and says so, in a frame of its own. A subscription that quietly
handed back an incomplete history would be worse than the block it replaces,
because a block is something somebody notices.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.errors import IncompatibleSchemaError, NotFoundError
from agent_workbench.domain.events import EventEnvelope
from agent_workbench.ports.event_log import EventCursor, EventLogPort

EVENTS_PREFIX = "/v1/chat/sessions"

LAST_EVENT_ID_HEADER = "last-event-id"

# Sent when nothing has happened, so an idle connection is not mistaken for a
# dead one by a proxy that times out silent sockets.
HEARTBEAT = ": heartbeat\n\n"

# The SSE event name for a position that was examined and not delivered.
# Deliberately not shaped like a domain event type: those are the payload class
# names, ``RunStarted`` and friends, so a dotted lower-case name cannot collide
# with one now or after any event is added. A client dispatching on ``event:``
# therefore cannot mistake this for something a workflow emitted.
QUARANTINE_EVENT = "stream.quarantined"

router = APIRouter(prefix=EVENTS_PREFIX, tags=["events"])


class _QuarantinedEvent(Protocol):
    """The part of a quarantine record a subscriber is told about."""

    @property
    def stream_id(self) -> str: ...
    @property
    def sequence(self) -> int: ...
    @property
    def event_id(self) -> str: ...
    @property
    def event_type(self) -> str: ...
    @property
    def schema_version(self) -> int: ...


class _ReplayPage(Protocol):
    """One page of a replay: what arrived, what did not, how far it looked."""

    @property
    def events(self) -> tuple[EventEnvelope, ...]: ...
    @property
    def quarantined(self) -> tuple[_QuarantinedEvent, ...]: ...
    @property
    def resume_after(self) -> int | None: ...


@runtime_checkable
class IsolatingEventLog(Protocol):
    """A log that can replay past a row it cannot decode, and name it.

    Structural, and declared here rather than added to ``EventLogPort``,
    because the capability is deliberately not part of the port: a port method
    returning "everything except what broke" would make degraded replay the
    contract for every implementation, including the ones that have no way to
    see a damaged row. Asking for the shape instead of for a concrete class
    also keeps this route off the PostgreSQL adapter, so the in-memory log --
    which cannot hold an undecodable row and does not offer this -- still
    serves the stream through the strict path below.
    """

    async def read_isolating(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
    ) -> _ReplayPage: ...


@dataclass(frozen=True, slots=True)
class _StrictPage:
    """A strict ``read`` in the shape the isolating one returns.

    So the loop below has one page shape to reason about instead of two
    branches that have to be kept in step. ``quarantined`` is always empty
    here, and that is the whole difference: the strict read raises on a row it
    cannot decode rather than skipping it, which is the contract every caller
    had before isolation existed.
    """

    events: tuple[EventEnvelope, ...]
    quarantined: tuple[_QuarantinedEvent, ...]
    resume_after: int | None


@router.get("/{session_id}/events")
async def subscribe(session_id: str, request: Request) -> StreamingResponse:
    """Stream a session's durable events, resuming from ``Last-Event-ID``.

    The session is authorized first, through the conversation store, so a
    stream id in a URL is no more a credential here than it is anywhere else.
    """

    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    chat = dependencies.chat
    if chat is None:  # pragma: no cover - the router is mounted only with one
        raise NotFoundError("this process does not serve chat")

    # Raises NotFoundError for another principal's session, another tenant's,
    # and one that does not exist -- the same answer to all three.
    await chat.conversations.history(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        limit=1,
    )

    after = _resume_from(request, session_id)
    return StreamingResponse(
        _stream(
            dependencies.events,
            stream_id=session_id,
            after_sequence=after,
            poll_seconds=dependencies.config.event_stream.catchup_poll_seconds,
            page_size=dependencies.config.event_stream.replay_page_size,
            heartbeat_seconds=dependencies.config.sse_heartbeat_seconds,
            disconnected=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={
            # Proxies that buffer an event stream turn it into a slow download.
            "cache-control": "no-store",
            "x-accel-buffering": "no",
        },
    )


def _resume_from(request: Request, session_id: str) -> int | None:
    """Where this subscriber left off, or ``None`` to start at the beginning.

    A malformed cursor starts from the beginning rather than failing the
    request. It arrives from a browser that may have stored it across a
    deploy, and refusing the connection would leave a client unable to
    reconnect at all -- with no way to discover that clearing it would help.
    """

    raw = request.headers.get(LAST_EVENT_ID_HEADER)
    if not raw:
        return None
    try:
        cursor = EventCursor.decode(raw)
    except IncompatibleSchemaError:
        return None
    if cursor.stream_id != session_id:
        # A cursor for a different stream says nothing about this one, and
        # honouring its number would silently skip events.
        return None
    return cursor.sequence


async def _stream(
    events: EventLogPort,
    *,
    stream_id: str,
    after_sequence: int | None,
    poll_seconds: int,
    page_size: int,
    heartbeat_seconds: int,
    disconnected: Callable[[], Awaitable[bool]] | None,
) -> AsyncIterator[str]:
    """Replay, then keep replaying from wherever the last batch ended.

    Latency here is bounded by the poll interval. The configuration also names
    a LISTEN/NOTIFY wakeup backend, which nothing consumes yet: until it does,
    this is the honest behaviour rather than a claim about it.
    """

    cursor = after_sequence
    idle = 0.0
    # Resolved once, before the loop: whether a log can isolate is a property
    # of the object, not of a page, and re-deciding it on every poll would let
    # one subscription serve part of a stream strictly and part of it
    # isolating -- two different meanings for one connection's frames.
    isolating = events if isinstance(events, IsolatingEventLog) else None
    while True:
        if disconnected is not None and await disconnected():
            return

        page = await _read_page(
            events,
            isolating,
            stream_id=stream_id,
            after_sequence=cursor,
            page_size=page_size,
        )
        for frame in _frames(page, stream_id):
            yield frame

        if page.resume_after is not None:
            # ``resume_after``, not "the page delivered something". A page can
            # be entirely quarantined -- ``events`` empty while the log looked
            # at, and named, several rows -- and stopping there would leave the
            # subscription parked in front of a run of unreadable rows it has
            # already skipped past.
            cursor = page.resume_after
            idle = 0.0
            # Straight back around: a full page means there is probably more,
            # and waiting a poll interval to find out would make a burst of
            # events arrive one page per interval.
            continue

        # ``None`` means nothing was examined, and then the caller's own cursor
        # is still the truth. Assigning it anyway would reset the subscription
        # to the head of the stream on the first idle poll and replay the whole
        # session, forever.
        idle += poll_seconds
        if idle >= heartbeat_seconds:
            idle = 0.0
            yield HEARTBEAT
        await asyncio.sleep(poll_seconds)


async def _read_page(
    events: EventLogPort,
    isolating: IsolatingEventLog | None,
    *,
    stream_id: str,
    after_sequence: int | None,
    page_size: int,
) -> _ReplayPage:
    """One page, from whichever replay this log supports."""

    if isolating is not None:
        return await isolating.read_isolating(
            stream_id, after_sequence=after_sequence, limit=page_size
        )
    batch = await events.read(stream_id, after_sequence=after_sequence, limit=page_size)
    return _StrictPage(
        events=batch,
        quarantined=(),
        # The last position this page reached, which for a strict read is the
        # last event it delivered: it raises rather than passing over anything.
        resume_after=next(
            (event.sequence for event in reversed(batch) if event.sequence is not None),
            None,
        ),
    )


def _frames(page: _ReplayPage, stream_id: str) -> Iterator[str]:
    """The page's frames, in stream order.

    Delivered events and quarantine notices are merged by sequence rather than
    sent as two blocks, because a notice's only job is to mark *where* the
    history is missing something. Appended at the end of a page it would say
    "one of the rows you just received is not all of them" and leave the client
    to guess which gap it belonged to -- and on a page boundary it would land
    after events that come later in the stream than the row it describes.

    Both inputs arrive in ascending sequence order, so one pass with a single
    look-ahead is enough.
    """

    notices = iter(page.quarantined)
    pending = next(notices, None)
    for envelope in page.events:
        sequence = envelope.sequence
        if sequence is None:
            # Unreachable: a replay returns durable events, which always carry
            # a position. Kept because the type says it may be None and
            # dropping the frame is the only safe reading if that ever
            # changes; no test covers it, and none can while the contract
            # holds.
            continue  # pragma: no cover
        while pending is not None and pending.sequence < sequence:
            yield _quarantine_frame(pending, stream_id)
            pending = next(notices, None)
        yield _frame(envelope, stream_id, sequence)
    while pending is not None:
        yield _quarantine_frame(pending, stream_id)
        pending = next(notices, None)


def _frame(envelope: EventEnvelope, stream_id: str, sequence: int) -> str:
    """One SSE frame, with the cursor as its id."""

    cursor = EventCursor(stream_id=stream_id, sequence=sequence).encode()
    return (
        f"id: {cursor}\n"
        f"event: {envelope.event_type}\n"
        f"data: {envelope.model_dump_json()}\n\n"
    )


def _quarantine_frame(record: _QuarantinedEvent, stream_id: str) -> str:
    """The frame that says one durable position was skipped, and which.

    A frame of its own, rather than a count carried on the next event. An
    envelope is the log's record of one thing that happened; a field added to
    it would make one event's frame assert something about a different
    position, and a client that dispatches or filters by event type -- this
    project's own web reducer does -- would drop the notice while keeping the
    events on either side of the hole. That is the silent partial replay this
    path exists to prevent, arrived at by a different route.

    Not an SSE comment (``: skipped 1``) for the same reason, only worse: every
    conforming parser, including this repository's, discards comment lines
    before a frame is assembled. The skip would be in the bytes and invisible
    to the program reading them.

    It carries an ``id`` because the id is what a browser sends back as
    ``Last-Event-ID``. Set to the skipped position, it is what stops a
    reconnect from resuming *before* the unreadable row and meeting it again on
    every attempt -- the same reason the page's ``resume_after`` counts
    examined rows rather than delivered ones. The consequence is stated rather
    than hidden: the client's cursor moves past a position it never received,
    and this frame is the record that it did.

    The decode failure's reason is deliberately absent. It describes this
    process's envelope contract to whoever holds the socket, and the operator
    who has to repair the row already has it in the log line the isolating read
    wrote against the same ``event_id``.
    """

    cursor = EventCursor(stream_id=stream_id, sequence=record.sequence).encode()
    body = json.dumps(
        {
            "event_id": record.event_id,
            "event_type": record.event_type,
            "schema_version": record.schema_version,
            "sequence": record.sequence,
            "stream_id": record.stream_id,
        },
        # Sorted and ASCII-escaped, so the frame is one line and byte-stable
        # across processes -- a client may hash frames, and a dict ordering is
        # not a wire format.
        sort_keys=True,
    )
    return f"id: {cursor}\nevent: {QUARANTINE_EVENT}\ndata: {body}\n\n"


__all__ = [
    "EVENTS_PREFIX",
    "HEARTBEAT",
    "LAST_EVENT_ID_HEADER",
    "QUARANTINE_EVENT",
    "IsolatingEventLog",
    "router",
]
