"""``GET /v1/system/workers``: the other process reporting in (ADR-0110).

No database: the router plus a stub identity adapter and the in-memory store,
so this runs in CI. What is under test is the shape the console branches on --
``available``, ``fresh`` and ``seconds_since_heartbeat`` -- not the SQL, which
``tests/contracts/test_worker_presence.py`` covers against both stores.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from agent_workbench.adapters.memory import InMemoryWorkerPresenceStore
from agent_workbench.apps.api.routes import system as system_route
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.domain.policies import PrincipalContext

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
PRINCIPAL = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")


class _StubPrincipals:
    def resolve(self, request: object) -> PrincipalContext:
        del request
        return PRINCIPAL


def _get(store: InMemoryWorkerPresenceStore | None) -> httpx.Response:
    app = FastAPI()
    app.include_router(system_route.router)
    setattr(
        app.state,
        STATE_ATTRIBUTE,
        SimpleNamespace(principals=_StubPrincipals(), worker_presence=store),
    )

    async def execute() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await client.get("/v1/system/workers", headers=HEADERS)

    return asyncio.run(execute())


def test_an_api_without_a_presence_store_says_unavailable_not_empty() -> None:
    """``None`` store means "this API cannot see Workers", not "no Workers"."""

    body = _get(None).json()

    assert body == {"available": False, "observed_at": None, "workers": []}


def test_fresh_and_stale_workers_are_told_apart_on_the_stores_clock() -> None:
    """One live Task Worker and one ingestion Worker that stopped beating.

    The clock is the store's, injected here so the stale row can be produced
    without sleeping. The console reads ``fresh`` and ``seconds_since_heartbeat``
    and never subtracts on its own clock.
    """

    now = datetime(2026, 9, 5, 2, 0, tzinfo=UTC)
    current = {"now": now - timedelta(seconds=120)}
    store = InMemoryWorkerPresenceStore(clock=lambda: current["now"])

    async def seed() -> None:
        await store.announce(
            worker_id="worker_ingest_1",
            kind="ingestion",
            deployment="demo-local",
            capabilities={"demo": False},
            started_at=now - timedelta(minutes=10),
            ttl_seconds=60,
        )
        current["now"] = now
        await store.announce(
            worker_id="worker_task_1",
            kind="task",
            deployment="demo-local",
            capabilities={"demo": True, "tools": ["export_artifact"]},
            started_at=now - timedelta(minutes=5),
            ttl_seconds=60,
        )

    asyncio.run(seed())

    body = _get(store).json()

    assert body["available"] is True
    by_id = {row["worker_id"]: row for row in body["workers"]}
    assert by_id["worker_task_1"]["fresh"] is True
    assert by_id["worker_task_1"]["seconds_since_heartbeat"] == 0.0
    assert by_id["worker_task_1"]["capabilities"] == {
        "demo": True,
        "tools": ["export_artifact"],
    }
    assert by_id["worker_ingest_1"]["fresh"] is False
    assert by_id["worker_ingest_1"]["seconds_since_heartbeat"] == 120.0
