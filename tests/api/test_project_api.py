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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent_workbench.adapters.filesystem.project_files import (
    FilesystemProjectFileStoreFactory,
)
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
    def __init__(self, root: Path | None = None) -> None:
        epoch = datetime(2026, 8, 20, tzinfo=UTC)
        ticks = count()
        self.clock = lambda: epoch + timedelta(seconds=next(ticks))
        self.store = InMemoryProjectStore(clock=self.clock)
        self.root = root
        # The *real* factory over a `tmp_path`, not a double. What these routes
        # have to get right is the refusal -- a mistyped root, a path leaving
        # the project -- and a double would have to reimplement the sandbox to
        # produce it, at which point the tests would be checking the double.
        self.service = ProjectService(
            store=self.store,
            clock=self.clock,
            files=FilesystemProjectFileStoreFactory(),
        )
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


# --- the directory a project is (ADR-072) ------------------------------------


def _new_project(client: httpx.AsyncClient) -> Awaitable[httpx.Response]:
    return client.post("/v1/projects", json={"name": "alpha"}, headers=_headers())


def test_a_project_reports_no_directory_until_one_is_registered(
    tmp_path: Path,
) -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> Any:
        created = await _new_project(client)
        return created.json()["root_path"]

    # Present and null, not omitted: a client has to be able to tell "no
    # directory registered" from "this build does not do directories".
    assert _run(world, scenario) is None


def test_registering_a_directory_then_listing_it(tmp_path: Path) -> None:
    world = _World()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")
    (tmp_path / "README.md").write_text("# alpha\n")

    async def scenario(client: httpx.AsyncClient) -> tuple[Any, Any, Any]:
        project_id = (await _new_project(client)).json()["project_id"]
        patched = await client.patch(
            f"/v1/projects/{project_id}",
            json={"root_path": str(tmp_path)},
            headers=_headers(),
        )
        listed = await client.get(
            f"/v1/projects/{project_id}/files", headers=_headers()
        )
        body = listed.json()
        return (
            patched.json()["root_path"],
            [entry["path"] for entry in body["entries"]],
            body["truncated"],
        )

    assert _run(world, scenario) == (str(tmp_path), ["src", "README.md"], False)


def test_a_mistyped_directory_is_refused_at_registration(tmp_path: Path) -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, Any]:
        project_id = (await _new_project(client)).json()["project_id"]
        refused = await client.patch(
            f"/v1/projects/{project_id}",
            json={"root_path": str(tmp_path / "typo")},
            headers=_headers(),
        )
        stored = await client.get(f"/v1/projects/{project_id}", headers=_headers())
        return refused.status_code, stored.json()["root_path"]

    # 400 at the moment somebody typed it, and *nothing stored*. Accepting it and
    # failing on first use would make an agent's error the first report of a
    # typo made days earlier.
    assert _run(world, scenario) == (400, None)


def test_a_path_leaving_the_project_is_refused(tmp_path: Path) -> None:
    world = _World()
    (tmp_path / "inside.txt").write_text("ok\n")

    async def scenario(client: httpx.AsyncClient) -> list[int]:
        project_id = (await _new_project(client)).json()["project_id"]
        await client.patch(
            f"/v1/projects/{project_id}",
            json={"root_path": str(tmp_path)},
            headers=_headers(),
        )
        return [
            (
                await client.get(
                    f"/v1/projects/{project_id}/file",
                    params={"path": path},
                    headers=_headers(),
                )
            ).status_code
            for path in ("../../etc/passwd", "/etc/passwd", "a\x00b")
        ]

    # 400, not 404: the request is malformed, and a 404 would additionally be a
    # statement about whether that file exists.
    assert _run(world, scenario) == [400, 400, 400]


def test_writing_reading_and_deleting_a_file(tmp_path: Path) -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[Any, Any, Any, Any]:
        project_id = (await _new_project(client)).json()["project_id"]
        await client.patch(
            f"/v1/projects/{project_id}",
            json={"root_path": str(tmp_path)},
            headers=_headers(),
        )
        written = await client.put(
            f"/v1/projects/{project_id}/file",
            json={"path": "docs/adr/0072.md", "content": "# ADR-072\n"},
            headers=_headers(),
        )
        read = await client.get(
            f"/v1/projects/{project_id}/file",
            params={"path": "docs/adr/0072.md"},
            headers=_headers(),
        )
        removed = await client.delete(
            f"/v1/projects/{project_id}/file",
            params={"path": "docs/adr/0072.md"},
            headers=_headers(),
        )
        again = await client.delete(
            f"/v1/projects/{project_id}/file",
            params={"path": "docs/adr/0072.md"},
            headers=_headers(),
        )
        return (
            written.json()["path"],
            read.json()["text"],
            removed.status_code,
            again.status_code,
        )

    # The second DELETE is 204 too: DELETE is idempotent, and a 404 for "already
    # gone" makes a retry after a dropped response look like a failure.
    assert _run(world, scenario) == ("docs/adr/0072.md", "# ADR-072\n", 204, 204)


def test_a_neighbour_cannot_read_the_files(tmp_path: Path) -> None:
    world = _World()
    (tmp_path / "secret.md").write_text("mine\n")

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        project_id = (await _new_project(client)).json()["project_id"]
        await client.patch(
            f"/v1/projects/{project_id}",
            json={"root_path": str(tmp_path)},
            headers=_headers(),
        )
        listed = await client.get(
            f"/v1/projects/{project_id}/files",
            headers=_headers(principal_id=NEIGHBOUR),
        )
        read = await client.get(
            f"/v1/projects/{project_id}/file",
            params={"path": "secret.md"},
            headers=_headers(principal_id=NEIGHBOUR),
        )
        return listed.status_code, read.status_code

    # The root path comes out of a row the store refuses to hand to anybody
    # else. A version that took the path from the request would be an open
    # directory-read endpoint with a project id decorating it.
    assert _run(world, scenario) == (404, 404)


def test_a_neighbour_cannot_point_your_project_at_their_directory(
    tmp_path: Path,
) -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[int, Any]:
        project_id = (await _new_project(client)).json()["project_id"]
        refused = await client.patch(
            f"/v1/projects/{project_id}",
            json={"root_path": str(tmp_path)},
            headers=_headers(principal_id=NEIGHBOUR),
        )
        stored = await client.get(f"/v1/projects/{project_id}", headers=_headers())
        return refused.status_code, stored.json()["root_path"]

    # The sharpest form of the cross-owner write: it aims somebody else's agent
    # at a directory you chose.
    assert _run(world, scenario) == (404, None)


def test_files_before_a_directory_is_registered_are_not_found(tmp_path: Path) -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> int:
        project_id = (await _new_project(client)).json()["project_id"]
        listed = await client.get(
            f"/v1/projects/{project_id}/files", headers=_headers()
        )
        return listed.status_code

    assert _run(world, scenario) == 404


def test_null_unregisters_and_an_absent_field_leaves_it_alone(tmp_path: Path) -> None:
    world = _World()

    async def scenario(client: httpx.AsyncClient) -> tuple[Any, Any, Any]:
        project_id = (await _new_project(client)).json()["project_id"]
        await client.patch(
            f"/v1/projects/{project_id}",
            json={"root_path": str(tmp_path)},
            headers=_headers(),
        )
        renamed = await client.patch(
            f"/v1/projects/{project_id}", json={"name": "beta"}, headers=_headers()
        )
        cleared = await client.patch(
            f"/v1/projects/{project_id}",
            json={"root_path": None},
            headers=_headers(),
        )
        return (
            renamed.json()["root_path"],
            cleared.json()["root_path"],
            cleared.json()["name"],
        )

    # The distinction the whole PATCH shape exists for, now on a second field:
    # renaming must not silently unregister the directory, and `null` must be
    # able to say "stop pointing at it".
    assert _run(world, scenario) == (str(tmp_path), None, "beta")
