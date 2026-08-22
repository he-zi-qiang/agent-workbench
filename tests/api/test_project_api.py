"""Projects over HTTP: ownership, and what an absent field means.

These routes reach no model, no vector index and no live database. Their whole
contract is the owner scope and the PATCH shape, so the in-memory project store
is the strongest small double -- it answers the same contract suite PostgreSQL
does.

The case worth having a whole test file for is the last one: ``{"project_id":
null}`` and ``{}`` parse to the same value and mean different things. An empty
body that silently took something out of its project would make the least
deliberate request the most destructive one.
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

from agent_workbench.adapters.memory import InMemoryProjectStore
from agent_workbench.application.projects import ProjectService
from agent_workbench.apps.api.identity import HeaderPrincipalResolver
from agent_workbench.apps.api.routes import projects as projects_route
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.domain.errors import NotFoundError

TENANT = "tenant_a"
OWNER = "user_owner"
NEIGHBOUR = "user_neighbour"


def _headers(tenant_id: str = TENANT, principal_id: str = OWNER) -> dict[str, str]:
    return {"x-tenant-id": tenant_id, "x-principal-id": principal_id}


class _World:
    def __init__(self) -> None:
        epoch = datetime(2026, 8, 20, tzinfo=UTC)
        ticks = count()
        self.clock = lambda: epoch + timedelta(seconds=next(ticks))
        self.store = InMemoryProjectStore(clock=self.clock)
        self.service = ProjectService(store=self.store, clock=self.clock)
        self.app = FastAPI()
        self.app.include_router(projects_route.router)
        self.app.include_router(projects_route.membership_router)
        setattr(
            self.app.state,
            STATE_ATTRIBUTE,
            SimpleNamespace(
                principals=HeaderPrincipalResolver(),
                projects=self.service,
            ),
        )
        self.app.add_exception_handler(NotFoundError, _not_found)


def _not_found(_request: Request, error: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(error)})


def _run(world: _World, scenario: Callable[[httpx.AsyncClient], Awaitable[Any]]) -> Any:
    async def execute() -> Any:
        transport = httpx.ASGITransport(app=world.app)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await scenario(client)

    return asyncio.run(execute())


def test_a_project_is_created_named_and_listed() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, list[str]]:
        created = await client.post(
            "/v1/projects", json={"name": "  季度复盘  "}, headers=_headers()
        )
        listed = await client.get("/v1/projects", headers=_headers())
        return created.status_code, [
            project["name"] for project in listed.json()["projects"]
        ]

    # Stripped by the type, not by the route: a name is a phrase, and one made
    # only of spaces is an invisible row.
    assert _run(world, scenario) == (201, ["季度复盘"])


def test_a_neighbour_sees_none_of_it() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[list[Any], int]:
        created = await client.post(
            "/v1/projects", json={"name": "季度复盘"}, headers=_headers()
        )
        project_id = created.json()["project_id"]
        listed = await client.get(
            "/v1/projects", headers=_headers(principal_id=NEIGHBOUR)
        )
        direct = await client.get(
            f"/v1/projects/{project_id}",
            headers=_headers(principal_id=NEIGHBOUR),
        )
        return listed.json()["projects"], direct.status_code

    assert _run(world, scenario) == ([], 404)


def test_archiving_hides_it_from_the_list_and_leaves_the_link_working() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[list[Any], int, bool]:
        created = await client.post(
            "/v1/projects", json={"name": "季度复盘"}, headers=_headers()
        )
        project_id = created.json()["project_id"]
        await client.patch(
            f"/v1/projects/{project_id}",
            json={"archived": True},
            headers=_headers(),
        )
        listed = await client.get("/v1/projects", headers=_headers())
        direct = await client.get(f"/v1/projects/{project_id}", headers=_headers())
        return (
            listed.json()["projects"],
            direct.status_code,
            direct.json()["archived_at"] is not None,
        )

    assert _run(world, scenario) == ([], 200, True)


def test_a_patch_that_asks_for_nothing_is_refused() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> int:
        created = await client.post(
            "/v1/projects", json={"name": "季度复盘"}, headers=_headers()
        )
        response = await client.patch(
            f"/v1/projects/{created.json()['project_id']}",
            json={},
            headers=_headers(),
        )
        return response.status_code

    assert _run(world, scenario) == 400


def test_deleting_a_project_releases_its_sessions_rather_than_removing_them() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, list[str], bool]:
        world.store.remember_session(
            tenant_id=TENANT, owner_id=OWNER, session_id="ses_1", title="问过的问题"
        )
        created = await client.post(
            "/v1/projects", json={"name": "季度复盘"}, headers=_headers()
        )
        project_id = created.json()["project_id"]
        await client.patch(
            "/v1/chat/sessions/ses_1/project",
            json={"project_id": project_id},
            headers=_headers(),
        )
        filed = await client.get(f"/v1/projects/{project_id}/items", headers=_headers())
        removed = await client.delete(f"/v1/projects/{project_id}", headers=_headers())
        # The session outlived the project it was filed under.
        survives = world.store.knows(tenant_id=TENANT, item_id="ses_1")
        return (
            removed.status_code,
            [item["item_id"] for item in filed.json()["items"]],
            survives,
        )

    assert _run(world, scenario) == (204, ["ses_1"], True)


def test_null_takes_it_out_and_an_absent_field_is_refused() -> None:
    """The distinction the whole PATCH shape exists for (ADR-071 4)."""

    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, list[str], int]:
        world.store.remember_session(
            tenant_id=TENANT, owner_id=OWNER, session_id="ses_1"
        )
        created = await client.post(
            "/v1/projects", json={"name": "季度复盘"}, headers=_headers()
        )
        project_id = created.json()["project_id"]
        await client.patch(
            "/v1/chat/sessions/ses_1/project",
            json={"project_id": project_id},
            headers=_headers(),
        )
        # An explicit null: take it out.
        released = await client.patch(
            "/v1/chat/sessions/ses_1/project",
            json={"project_id": None},
            headers=_headers(),
        )
        remaining = await client.get(
            f"/v1/projects/{project_id}/items", headers=_headers()
        )
        # An absent field: say so rather than guessing. An empty body must not
        # be the most destructive request in the API.
        silent = await client.patch(
            "/v1/chat/sessions/ses_1/project", json={}, headers=_headers()
        )
        return (
            released.status_code,
            [item["item_id"] for item in remaining.json()["items"]],
            silent.status_code,
        )

    assert _run(world, scenario) == (204, [], 400)


def test_filing_something_under_a_project_that_is_not_yours_is_a_404() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> int:
        world.store.remember_session(
            tenant_id=TENANT, owner_id=OWNER, session_id="ses_1"
        )
        created = await client.post(
            "/v1/projects",
            json={"name": "别人的项目"},
            headers=_headers(principal_id=NEIGHBOUR),
        )
        response = await client.patch(
            "/v1/chat/sessions/ses_1/project",
            json={"project_id": created.json()["project_id"]},
            headers=_headers(),
        )
        return response.status_code

    assert _run(world, scenario) == 404


def test_items_of_a_project_that_does_not_exist_is_a_404_not_an_empty_list() -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> int:
        response = await client.get(
            "/v1/projects/prj_never_created/items", headers=_headers()
        )
        return response.status_code

    assert _run(world, scenario) == 404


def test_a_coding_session_files_into_a_project_under_its_own_path() -> None:
    """The code half of "a project collects one piece of work".

    A coding session *is* a ``conversation_sessions`` row -- ``CodeSession.open``
    writes it with ``mode="code"`` -- so membership needed no column and no
    second application call. It gets its own path anyway, because the URL is
    what a reader of the API sees and telling them to PATCH a *chat* session in
    order to file a *coding* one is asking them to know an implementation
    detail every other ``/v1/code`` route is careful to hide.

    The kind is the point of the assertion. ``contents`` labels each row with
    the session's ``mode``, so a coding session filed here comes back as
    ``code`` -- which is what lets the project page route it to /code rather
    than to /chat.
    """

    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, list[tuple[str, str]]]:
        world.store.remember_session(
            tenant_id=TENANT,
            owner_id=OWNER,
            session_id="ses_code_1",
            mode="code",
            title="编写贪吃蛇",
        )
        created = await client.post(
            "/v1/projects", json={"name": "贪吃蛇复盘"}, headers=_headers()
        )
        project_id = created.json()["project_id"]
        filed = await client.patch(
            "/v1/code/sessions/ses_code_1/project",
            json={"project_id": project_id},
            headers=_headers(),
        )
        listed = await client.get(
            f"/v1/projects/{project_id}/items", headers=_headers()
        )
        return (
            filed.status_code,
            [(item["kind"], item["item_id"]) for item in listed.json()["items"]],
        )

    assert _run(world, scenario) == (204, [("code", "ses_code_1")])


def test_taking_a_coding_session_out_of_a_project_needs_an_explicit_null() -> None:
    """Same contract as the chat path, asserted separately because it is a
    separate route: an empty body must not be the most destructive request."""

    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[list[str], int, list[str]]:
        world.store.remember_session(
            tenant_id=TENANT, owner_id=OWNER, session_id="ses_code_1", mode="code"
        )
        created = await client.post(
            "/v1/projects", json={"name": "贪吃蛇复盘"}, headers=_headers()
        )
        project_id = created.json()["project_id"]
        await client.patch(
            "/v1/code/sessions/ses_code_1/project",
            json={"project_id": project_id},
            headers=_headers(),
        )
        silent = await client.patch(
            "/v1/code/sessions/ses_code_1/project", json={}, headers=_headers()
        )
        still_there = await client.get(
            f"/v1/projects/{project_id}/items", headers=_headers()
        )
        await client.patch(
            "/v1/code/sessions/ses_code_1/project",
            json={"project_id": None},
            headers=_headers(),
        )
        after = await client.get(f"/v1/projects/{project_id}/items", headers=_headers())
        return (
            [item["item_id"] for item in still_there.json()["items"]],
            silent.status_code,
            [item["item_id"] for item in after.json()["items"]],
        )

    assert _run(world, scenario) == (["ses_code_1"], 400, [])
