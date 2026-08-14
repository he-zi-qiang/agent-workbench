"""Subscribing to one chat session's events, and resuming after a drop.

Everything about *how* a subscription is served -- replay, cursors, quarantine
notices, heartbeats -- lives in :mod:`agent_workbench.apps.api.sse`, because
none of it depends on what the stream belongs to. What is left here is the only
part that does: a stream id in a URL is no more a credential than it is
anywhere else, so the session is authorized through the conversation store
before a byte is streamed, and only this module knows that the id in its path
names a chat session.

The three transport names this module calls lost their leading underscore in
the move, and that is not cosmetic: an underscore says "private to the module
that declares it", and these are now the surface a second subscriber route is
written against. Keeping the old spelling would have made every future caller
reach into another module's privates with the type checker's blessing.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from agent_workbench.apps.api.sse import resume_from, stream_events
from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.errors import NotFoundError

EVENTS_PREFIX = "/v1/chat/sessions"

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

    after = resume_from(request, session_id)
    return StreamingResponse(
        stream_events(
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


__all__ = ["EVENTS_PREFIX", "router"]
