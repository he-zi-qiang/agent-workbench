"""A code session is not addressable through the chat surface.

Both chat routes take a session id from a URL and both reach the same table,
so without a mode the only thing separating a code session from a chat one is
that nobody has typed its id into the other endpoint yet. That is not a
boundary, it is an absence of traffic.

The refusal has to happen at the store, which is why these tests drive the real
``ChatService.history`` and the real events route rather than asserting on a
double: the mode is fixed inside them, and a test that passed its own
``mode="chat"`` would be checking its own argument.

On ``/events`` the status code alone would not settle it. A subscription that
was refused only once streaming had begun would still be a 200 the client has
to interpret, so the second assertion is that no ``StreamingResponse`` was ever
constructed -- and the third is the control: a chat session builds exactly one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from agent_workbench.adapters.memory import InMemoryConversationStore, InMemoryEventLog
from agent_workbench.application.chat import ChatService
from agent_workbench.application.chat_execution import TurnExecution
from agent_workbench.apps.api.main import ERROR_STATUS
from agent_workbench.apps.api.routes import chat as chat_route
from agent_workbench.apps.api.routes import events as events_route
from agent_workbench.apps.api.sse import LiveEventChannel
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.ports.chat_release import ChatReleaseCoordinator

TENANT = "tenant_a"
OWNER = "user_1"
HEADERS = {"x-tenant-id": TENANT, "x-principal-id": OWNER}
CHAT_SESSION = "ses_chat_gate"
CODE_SESSION = "ses_code_gate"


class _StubPrincipal:
    tenant_id = TENANT
    principal_id = OWNER


class _StubPrincipals:
    def resolve(self, request: object) -> _StubPrincipal:
        return _StubPrincipal()


class _NeverExecutes:
    """An execution these routes must never reach: neither one answers."""

    async def produce(self, *_: object, **__: object) -> Any:
        raise AssertionError("history and subscribe do not produce answers")

    def live_text_policy(self, _request: object, /) -> Any:
        raise AssertionError("history and subscribe do not produce answers")


class _NeverReleases:
    """A releaser these routes must never reach, for the same reason."""

    async def release(self, *_: object, **__: object) -> Any:
        raise AssertionError("history and subscribe release nothing")


class _CountingStreamingResponse(StreamingResponse):
    """Records every streaming response the events route actually builds."""

    built: int = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).built += 1
        super().__init__(*args, **kwargs)


def _finite_stream(*_: object, **__: object) -> AsyncIterator[bytes]:
    """The event body, cut short so an in-process request can finish.

    ``ASGITransport`` buffers a response body whole and a real event stream
    ends only when its subscriber leaves, so the success path would hang rather
    than assert. Substituting the body is safe for what is under test: the
    session is authorized before this is ever called, which is precisely the
    claim these tests make.
    """

    async def frames() -> AsyncIterator[bytes]:
        yield b":ok\n\n"

    return frames()


def _app(conversations: InMemoryConversationStore) -> FastAPI:
    app = FastAPI()
    app.include_router(chat_route.router)
    app.include_router(events_route.router)
    chat = ChatService(
        execution=cast(TurnExecution, _NeverExecutes()),
        conversations=conversations,
        releaser=cast(ChatReleaseCoordinator, _NeverReleases()),
        request_timeout_seconds=30,
        orphan_grace_seconds=5,
    )
    setattr(
        app.state,
        STATE_ATTRIBUTE,
        SimpleNamespace(
            principals=_StubPrincipals(),
            chat=chat,
            events=InMemoryEventLog(),
            live_events=LiveEventChannel(
                buffer_events=8,
                max_subscribers_per_stream=4,
            ),
            config=SimpleNamespace(
                sse_heartbeat_seconds=600,
                event_stream=SimpleNamespace(
                    catchup_poll_seconds=1,
                    replay_page_size=500,
                    live_delta_coalesce_ms=10,
                ),
            ),
        ),
    )
    # Taken from the real mapping rather than restated, so a test cannot keep
    # asserting 404 after the application stopped answering that way.
    status = ERROR_STATUS[NotFoundError]

    def refuse(_request: Any, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    app.add_exception_handler(NotFoundError, refuse)  # pyright: ignore[reportArgumentType]
    return app


def _run(scenario: Any) -> Any:
    async def execute() -> Any:
        conversations = InMemoryConversationStore()
        await conversations.create_session(
            session_id=CHAT_SESSION, tenant_id=TENANT, owner_id=OWNER
        )
        await conversations.create_session(
            session_id=CODE_SESSION, tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        transport = httpx.ASGITransport(app=_app(conversations))  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await scenario(client)

    return asyncio.run(execute())


def test_a_code_session_has_no_chat_history() -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        refused = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions/{CODE_SESSION}/messages",
            headers=HEADERS,
        )
        allowed = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions/{CHAT_SESSION}/messages",
            headers=HEADERS,
        )
        return refused.status_code, allowed.status_code

    refused, allowed = _run(scenario)

    assert refused == 404
    # The control: the same route, the same principal, a chat session.
    assert allowed == 200


def test_a_code_session_opens_no_event_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        events_route, "StreamingResponse", _CountingStreamingResponse, raising=True
    )
    # The body is cut short here too, even though a passing run never reaches
    # it: with the gate removed this request opens a real event stream, and an
    # endless one would hang the suite instead of failing it. A test whose
    # failure mode is a hang cannot be told apart from a stuck machine.
    monkeypatch.setattr(events_route, "stream_events", _finite_stream, raising=True)
    _CountingStreamingResponse.built = 0

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        refused = await client.get(
            f"{events_route.EVENTS_PREFIX}/{CODE_SESSION}/events", headers=HEADERS
        )
        return refused.status_code, _CountingStreamingResponse.built

    status_code, built = _run(scenario)

    assert status_code == 404
    # Refused before the response exists, not by a frame inside one: a client
    # cannot tell a stream that ends early from a stream with nothing in it.
    assert built == 0


def test_a_chat_session_still_opens_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control for the counter: it can tell the two cases apart."""

    monkeypatch.setattr(
        events_route, "StreamingResponse", _CountingStreamingResponse, raising=True
    )
    monkeypatch.setattr(events_route, "stream_events", _finite_stream, raising=True)
    _CountingStreamingResponse.built = 0

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        opened = await client.get(
            f"{events_route.EVENTS_PREFIX}/{CHAT_SESSION}/events", headers=HEADERS
        )
        return opened.status_code, _CountingStreamingResponse.built

    status_code, built = _run(scenario)

    assert status_code == 200
    assert built == 1
