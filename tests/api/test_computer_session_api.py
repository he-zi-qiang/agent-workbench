"""``GET /v1/computer/session``: the forward, and the two answers it must keep apart.

No database. This route talks to one loopback URL and reports what came back,
so the harness is the router plus a stub identity adapter and a fake upstream --
which is also why these run in CI while most of ``tests/api`` skips itself.

The upstream here is a real Starlette app served over a real socket rather than
a mocked ``httpx`` call. What is under test is a hop between two processes, and
a mock would let a route that never made the request pass.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_workbench.apps.api.routes import computer as computer_route
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.domain.policies import PrincipalContext

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
PRINCIPAL = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")

#: What the screen server answers. Shaped like the real payload rather than
#: minimally, because the claim under test is that this route passes it through
#: without re-modelling it.
UPSTREAM_BODY: dict[str, Any] = {
    "service": "agent-workbench-computer",
    "scope": "process",
    "granted": [
        {"bundle_id": "com.apple.Notes", "name": "Notes", "tier": "full"},
    ],
    "frontmost": {
        "bundle_id": "com.apple.mail",
        "name": "Mail",
        "granted": False,
    },
    "actions": [
        {
            "at": "2026-08-29T11:20:18+00:00",
            "action": "left_click",
            "application": {"bundle_id": "com.apple.mail", "name": "Mail"},
            "allowed": False,
            "reason": (
                "The frontmost application is not in this session's approved list."
            ),
            "detail": "",
        }
    ],
}


class _StubPrincipals:
    def resolve(self, request: object) -> object:
        del request
        return PRINCIPAL


@pytest.fixture
def upstream() -> Iterator[str]:
    """A stand-in screen server on a real loopback port.

    Port 0 so two suites running at once do not collide; the socket reports
    which one it got.
    """

    async def session(request: Request) -> JSONResponse:
        del request
        return JSONResponse(UPSTREAM_BODY)

    app = Starlette(routes=[Route("/session", endpoint=session, methods=["GET"])])
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "the stand-in screen server did not start"
        [socket] = server.servers[0].sockets
        yield f"http://127.0.0.1:{socket.getsockname()[1]}/session"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _get(url: str) -> httpx.Response:
    app = FastAPI()
    app.include_router(computer_route.router)
    setattr(
        app.state,
        STATE_ATTRIBUTE,
        SimpleNamespace(
            principals=_StubPrincipals(),
            config=SimpleNamespace(computer_session_url=url),
        ),
    )

    async def execute() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await client.get("/v1/computer/session", headers=HEADERS)

    return asyncio.run(execute())


def test_the_screen_servers_answer_arrives_unchanged(upstream: str) -> None:
    """Passed through, not re-modelled.

    This process does not own the shape of a screen session. A response model
    for it here would be a second definition that drifts from the first, and the
    drift would show up as a field the console silently stops seeing.
    """

    answered = _get(upstream)

    assert answered.status_code == 200
    body = answered.json()
    assert body["reachable"] is True
    assert body["session"] == UPSTREAM_BODY
    assert body["detail"] == ""


def test_the_person_gets_the_name_the_model_was_refused(upstream: str) -> None:
    """ADR-095 §2 reaching the console, which is the point of the whole path.

    The upstream row records that a click was refused because the frontmost
    window was unapproved; the refusal the model read does not say which window,
    and this one does. Asserted here rather than only at the screen server so
    that a forward which quietly stripped fields would fail.
    """

    body = _get(upstream).json()

    assert body["session"]["frontmost"]["name"] == "Mail"
    [action] = body["session"]["actions"]
    assert action["allowed"] is False
    assert action["application"]["name"] == "Mail"


def test_a_screen_server_that_is_not_running_is_said_so_and_not_faked() -> None:
    """The normal answer on a normal machine, and it must not look like an
    empty allowlist.

    That server is not started by any ordinary `scripts/dev.sh` path. "Not
    running" and "running, with nothing approved" are the two states this page
    has been careful about since it was written, so they are different shapes
    rather than the same empty list.
    """

    # Port 1 on loopback: nothing listens there, and the connection is refused
    # immediately rather than hanging until the timeout.
    answered = _get("http://127.0.0.1:1/session")

    assert answered.status_code == 200
    body = answered.json()
    assert body["reachable"] is False
    assert body["session"] is None
    assert body["detail"] != ""


def test_an_upstream_that_answers_badly_is_also_not_reachable() -> None:
    """A 500 from that server is not a session either.

    Folded into the same shape as a refused connection, because the console's
    next move is identical -- there is nothing to show -- and a third state
    would be one it had to invent a rendering for.
    """

    async def broken(request: Request) -> JSONResponse:
        del request
        return JSONResponse({"detail": "no"}, status_code=500)

    app = Starlette(routes=[Route("/session", endpoint=broken, methods=["GET"])])
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        [socket] = server.servers[0].sockets
        body = _get(f"http://127.0.0.1:{socket.getsockname()[1]}/session").json()
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    assert body["reachable"] is False
    assert body["session"] is None
    assert "500" in body["detail"]
