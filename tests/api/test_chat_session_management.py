"""Listing and renaming durable Chat sessions over HTTP.

These routes do not need a model, vector index or live database. Their whole
contract is the projection and the three-axis session gate, so the shared
in-memory conversation store is the strongest small test double: it implements
the same tenant, principal and mode rules as PostgreSQL under the store contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_workbench.adapters.memory import InMemoryConversationStore
from agent_workbench.application.chat import ChatService
from agent_workbench.apps.api.identity import HeaderPrincipalResolver
from agent_workbench.apps.api.routes import chat as chat_route
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.domain.errors import NotFoundError

TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"
OWNER = "user_owner"
NEIGHBOUR = "user_neighbour"


def _headers(tenant_id: str, principal_id: str) -> dict[str, str]:
    return {"x-tenant-id": tenant_id, "x-principal-id": principal_id}


class _World:
    def __init__(self) -> None:
        epoch = datetime(2026, 1, 1, tzinfo=UTC)
        ticks = count()
        self.conversations = InMemoryConversationStore(
            clock=lambda: epoch + timedelta(seconds=next(ticks))
        )
        self.service = ChatService(
            # Session management reaches neither dependency. Keeping them as
            # inert objects makes that boundary part of this test: listing and
            # renaming must not load a model or assemble a release path.
            execution=SimpleNamespace(),  # type: ignore[arg-type]
            conversations=self.conversations,
            releaser=SimpleNamespace(),  # type: ignore[arg-type]
            request_timeout_seconds=30,
            orphan_grace_seconds=5,
        )
        self.app = FastAPI()
        self.app.include_router(chat_route.router)
        setattr(
            self.app.state,
            STATE_ATTRIBUTE,
            SimpleNamespace(
                principals=HeaderPrincipalResolver(),
                chat=self.service,
            ),
        )
        self.app.add_exception_handler(NotFoundError, _not_found)


def _not_found(_request: Request, error: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(error)})


def _run(
    world: _World,
    scenario: Callable[[httpx.AsyncClient], Awaitable[Any]],
) -> Any:
    async def execute() -> Any:
        transport = httpx.ASGITransport(app=world.app)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await scenario(client)

    return asyncio.run(execute())


def test_the_list_is_recent_bounded_and_scoped_on_tenant_principal_and_mode() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[list[str], list[str]]:
        await world.conversations.create_session(
            session_id="ses_owner_old",
            tenant_id=TENANT,
            owner_id=OWNER,
            title="Older chat",
        )
        await world.conversations.create_session(
            session_id="ses_neighbour",
            tenant_id=TENANT,
            owner_id=NEIGHBOUR,
            title="Neighbour private chat",
        )
        await world.conversations.create_session(
            session_id="ses_other_tenant",
            tenant_id=OTHER_TENANT,
            owner_id=OWNER,
            title="Other tenant private chat",
        )
        await world.conversations.create_session(
            session_id="ses_code",
            tenant_id=TENANT,
            owner_id=OWNER,
            title="Code work",
            mode="code",
        )
        await world.conversations.create_session(
            session_id="ses_owner_new",
            tenant_id=TENANT,
            owner_id=OWNER,
            title="Newer chat",
        )

        listed = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions", headers=_headers(TENANT, OWNER)
        )
        bounded = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions?limit=1",
            headers=_headers(TENANT, OWNER),
        )
        assert listed.status_code == bounded.status_code == 200
        rows = listed.json()["sessions"]
        assert set(rows[0]) == {"session_id", "title", "last_activity_at"}
        assert all(row["last_activity_at"] is not None for row in rows)
        return (
            [row["session_id"] for row in rows],
            [row["session_id"] for row in bounded.json()["sessions"]],
        )

    listed, bounded = _run(world, scenario)

    assert listed == ["ses_owner_new", "ses_owner_old"]
    assert bounded == ["ses_owner_new"]


def test_rename_persists_but_foreign_and_code_sessions_answer_the_same_404() -> None:
    world = _World()

    async def scenario(
        client: httpx.AsyncClient,
    ) -> tuple[str, str, tuple[int, int, int], list[str]]:
        await world.conversations.create_session(
            session_id="ses_chat",
            tenant_id=TENANT,
            owner_id=OWNER,
            title="First title",
        )
        await world.conversations.create_session(
            session_id="ses_code",
            tenant_id=TENANT,
            owner_id=OWNER,
            title="Code title",
            mode="code",
        )

        renamed = await client.patch(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_chat",
            headers=_headers(TENANT, OWNER),
            json={"title": "Name chosen by owner"},
        )
        listed = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions", headers=_headers(TENANT, OWNER)
        )
        neighbour = await client.patch(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_chat",
            headers=_headers(TENANT, NEIGHBOUR),
            json={"title": "Taken by neighbour"},
        )
        other_tenant = await client.patch(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_chat",
            headers=_headers(OTHER_TENANT, OWNER),
            json={"title": "Taken by other tenant"},
        )
        wrong_mode = await client.patch(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_code",
            headers=_headers(TENANT, OWNER),
            json={"title": "Reached through Chat"},
        )
        refused_bodies = [neighbour.text, other_tenant.text, wrong_mode.text]
        return (
            renamed.json()["title"],
            listed.json()["sessions"][0]["title"],
            (neighbour.status_code, other_tenant.status_code, wrong_mode.status_code),
            refused_bodies,
        )

    renamed, listed, statuses, refused_bodies = _run(world, scenario)

    assert renamed == listed == "Name chosen by owner"
    assert statuses == (404, 404, 404)
    assert all("Name chosen by owner" not in body for body in refused_bodies)


def test_single_session_resolves_old_deep_links_through_the_same_owner_gate() -> None:
    world = _World()

    async def scenario(
        client: httpx.AsyncClient,
    ) -> tuple[str, tuple[int, int, int], list[str]]:
        await world.conversations.create_session(
            session_id="ses_chat",
            tenant_id=TENANT,
            owner_id=OWNER,
            title="Older but still valid",
        )
        await world.conversations.create_session(
            session_id="ses_code",
            tenant_id=TENANT,
            owner_id=OWNER,
            title="Code session",
            mode="code",
        )

        owned = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_chat",
            headers=_headers(TENANT, OWNER),
        )
        neighbour = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_chat",
            headers=_headers(TENANT, NEIGHBOUR),
        )
        other_tenant = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_chat",
            headers=_headers(OTHER_TENANT, OWNER),
        )
        wrong_mode = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_code",
            headers=_headers(TENANT, OWNER),
        )
        refused = [neighbour, other_tenant, wrong_mode]
        return (
            owned.json()["title"],
            tuple(response.status_code for response in refused),
            [response.text for response in refused],
        )

    title, statuses, refused_bodies = _run(world, scenario)

    assert title == "Older but still valid"
    assert statuses == (404, 404, 404)
    assert all("Older but still valid" not in body for body in refused_bodies)


def test_list_and_rename_inputs_are_bounded_by_the_http_contract() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int, int, int, int]:
        await world.conversations.create_session(
            session_id="ses_chat",
            tenant_id=TENANT,
            owner_id=OWNER,
        )
        headers = _headers(TENANT, OWNER)
        empty = await client.patch(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_chat",
            headers=headers,
            json={"title": ""},
        )
        whitespace = await client.patch(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_chat",
            headers=headers,
            json={"title": "   \t"},
        )
        extra = await client.patch(
            f"{chat_route.CHAT_PREFIX}/sessions/ses_chat",
            headers=headers,
            json={"title": "valid", "owner_id": NEIGHBOUR},
        )
        zero = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions?limit=0", headers=headers
        )
        too_many = await client.get(
            f"{chat_route.CHAT_PREFIX}/sessions?limit=201", headers=headers
        )
        return (
            empty.status_code,
            whitespace.status_code,
            extra.status_code,
            zero.status_code,
            too_many.status_code,
        )

    assert _run(world, scenario) == (422, 422, 422, 422, 422)
