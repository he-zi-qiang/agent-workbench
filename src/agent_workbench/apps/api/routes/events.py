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
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

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

router = APIRouter(prefix=EVENTS_PREFIX, tags=["events"])


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
    while True:
        if disconnected is not None and await disconnected():
            return

        batch = await events.read(stream_id, after_sequence=cursor, limit=page_size)
        for envelope in batch:
            if envelope.sequence is None:
                # Unreachable: read() returns durable events, which always
                # carry a position. Kept because the type says it may be None
                # and skipping is the only safe reading if that ever changes;
                # no test covers it, and none can while the contract holds.
                continue  # pragma: no cover
            cursor = envelope.sequence
            yield _frame(envelope, stream_id, cursor)

        if batch:
            idle = 0.0
            # Straight back around: a full page means there is probably more,
            # and waiting a poll interval to find out would make a burst of
            # events arrive one page per interval.
            continue

        idle += poll_seconds
        if idle >= heartbeat_seconds:
            idle = 0.0
            yield HEARTBEAT
        await asyncio.sleep(poll_seconds)


def _frame(envelope: EventEnvelope, stream_id: str, sequence: int) -> str:
    """One SSE frame, with the cursor as its id."""

    cursor = EventCursor(stream_id=stream_id, sequence=sequence).encode()
    return (
        f"id: {cursor}\n"
        f"event: {envelope.event_type}\n"
        f"data: {envelope.model_dump_json()}\n\n"
    )


__all__ = ["EVENTS_PREFIX", "HEARTBEAT", "LAST_EVENT_ID_HEADER", "router"]
