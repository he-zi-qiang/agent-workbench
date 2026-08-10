"""Task HTTP control-plane integration tests over PostgreSQL.

The Worker is intentionally absent.  These tests prove the API can durably
accept, authorize and observe a Task even when optional Chat dependencies are
not assembled; execution belongs to the separate single-Worker process.
"""

from __future__ import annotations

import asyncio
import os
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.apps.api.main import build_app
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
OTHER_OWNER_HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_2"}
OTHER_TENANT_HEADERS = {"x-tenant-id": "tenant_b", "x-principal-id": "user_1"}

TABLES = "task_runs, events, event_streams"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _settings(root: Path) -> Settings:
    with DEFAULT_CONFIG_FILE.open("rb") as handle:
        payload: dict[str, Any] = tomllib.load(handle)
    dsn = _dsn()
    payload["database"].update(dsn=dsn, guard_dsn=dsn, listen_dsn=dsn)
    payload["model"]["main"]["model_id"] = "unit-main"
    payload["model"]["compact"]["model_id"] = "unit-compact"
    payload["artifact_store"]["local_root"] = str(root)
    payload["secrets"] = {"deepseek_api_key": "unit-test-key"}
    return Settings(**payload)


def _run(scenario: Callable[[httpx.AsyncClient], Awaitable[Any]], root: Path) -> Any:
    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        finally:
            await engine.dispose()

        app, dependencies = build_app(project_api(_settings(root)), with_chat=False)
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://api.test",
            ) as client:
                return await scenario(client)
        finally:
            await dependencies.dispose()

    return asyncio.run(execute())


def _submission(
    *, objective: str = "Compare dense and hybrid retrieval.", key: str = "task-1"
) -> dict[str, Any]:
    return {
        "headers": {**HEADERS, "Idempotency-Key": key},
        "json": {
            "objective": objective,
            "max_revisions": 2,
            "knowledge_base_id": "kb_main",
        },
    }


def test_task_api_submits_reads_replays_and_cancels_without_chat(
    tmp_path: Path,
) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[Any, ...]:
        opened = await client.post("/v1/tasks", **_submission())
        task_id = opened.json()["task_id"]
        fetched = await client.get(f"/v1/tasks/{task_id}", headers=HEADERS)
        timeline = await client.get(f"/v1/tasks/{task_id}/timeline", headers=HEADERS)
        cursor = timeline.json()["cursor"]
        resumed = await client.get(
            f"/v1/tasks/{task_id}/timeline",
            headers=HEADERS,
            params={"cursor": cursor},
        )
        cancelled = await client.post(
            f"/v1/tasks/{task_id}/cancel",
            headers=HEADERS,
            json={"reason": "the requester no longer needs this report"},
        )
        return opened, fetched, timeline, resumed, cancelled

    opened, fetched, timeline, resumed, cancelled = _run(scenario, tmp_path)

    assert opened.status_code == 201
    assert opened.json()["status"] == "queued"
    assert {"tenant_id", "owner_id", "run_semantics_snapshot"}.isdisjoint(opened.json())
    assert fetched.json() == opened.json()
    assert [event["event_type"] for event in timeline.json()["events"]] == [
        "TaskSubmitted"
    ]
    assert timeline.json()["cursor"] is not None
    assert resumed.json()["events"] == []
    assert resumed.json()["cursor"] == timeline.json()["cursor"]
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert (
        cancelled.json()["status_detail"] == "the requester no longer needs this report"
    )


def test_equal_task_retries_return_the_first_task_despite_a_new_input_artifact(
    tmp_path: Path,
) -> None:
    async def scenario(
        client: httpx.AsyncClient,
    ) -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        first = await client.post("/v1/tasks", **_submission())
        retry = await client.post("/v1/tasks", **_submission())
        timeline = await client.get(
            f"/v1/tasks/{first.json()['task_id']}/timeline",
            headers=HEADERS,
        )
        return first, retry, timeline

    first, retry, timeline = _run(scenario, tmp_path)

    assert (first.status_code, retry.status_code) == (201, 201)
    assert first.json()["task_id"] == retry.json()["task_id"]
    # The stream is the stored workflow thread.  A fresh candidate thread is
    # minted on retry, so this one durable event proves the Registry retained
    # the first Task's thread rather than appending the retry to a new stream.
    assert len(timeline.json()["events"]) == 1
    assert timeline.json()["events"][0]["stream_id"].startswith("thr_")


def test_a_submission_may_choose_the_general_graph_and_the_row_freezes_it(
    tmp_path: Path,
) -> None:
    """PR-5.2 on the wire: the optional field reaches the stored row.

    Asserted against ``task_runs`` directly because ``graph_version`` is
    deliberately not in the caller-facing view -- it is the Worker's routing
    fact, and the row is where the freeze lives (ADR-031 §2.3). The absent-
    field control submits alongside so "unchosen means v1" is checked against
    the same database rather than remembered.
    """

    async def scenario(client: httpx.AsyncClient) -> tuple[str, str]:
        chosen = await client.post(
            "/v1/tasks",
            headers={**HEADERS, "Idempotency-Key": "task-general"},
            json={
                "objective": "Convert the attached ledger into a cleaned CSV.",
                "graph": "general",
            },
        )
        defaulted = await client.post("/v1/tasks", **_submission())
        assert (chosen.status_code, defaulted.status_code) == (201, 201)
        return chosen.json()["task_id"], defaulted.json()["task_id"]

    chosen_id, defaulted_id = _run(scenario, tmp_path)

    async def stored_versions() -> dict[str, str]:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text("SELECT task_id, graph_version FROM task_runs")
                )
                return {row.task_id: row.graph_version for row in rows}
        finally:
            await engine.dispose()

    versions = asyncio.run(stored_versions())
    assert versions[chosen_id] == "v2_general"
    assert versions[defaulted_id] == "v1"


def test_a_graph_the_service_does_not_offer_is_a_validation_error(
    tmp_path: Path,
) -> None:
    """A version string is not a shape. ``v2_general`` is what the row stores,
    and exactly the kind of value a caller must not send: it names a deployment
    fact, and accepting it would let a client pin itself to one."""

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/v1/tasks",
            headers={**HEADERS, "Idempotency-Key": "task-bad-graph"},
            json={"objective": "Do the thing.", "graph": "v2_general"},
        )

    response = _run(scenario, tmp_path)

    assert response.status_code == 422


def test_reusing_a_task_key_for_different_input_is_a_conflict(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[int, int, str]:
        first = await client.post("/v1/tasks", **_submission())
        conflicting = await client.post(
            "/v1/tasks",
            **_submission(objective="Prepare a market-entry plan."),
        )
        return first.status_code, conflicting.status_code, conflicting.json()["detail"]

    status, conflict, detail = _run(scenario, tmp_path)

    assert (status, conflict) == (201, 409)
    assert detail == "task submission conflicts with the idempotency key"
    assert "user_1" not in detail


@pytest.mark.parametrize("headers", [OTHER_OWNER_HEADERS, OTHER_TENANT_HEADERS])
def test_another_principal_cannot_read_timeline_or_cancel_a_task(
    tmp_path: Path,
    headers: dict[str, str],
) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[int, int, int, str, str]:
        opened = await client.post("/v1/tasks", **_submission())
        task_id = opened.json()["task_id"]
        absent = await client.get("/v1/tasks/task_missing", headers=headers)
        fetched = await client.get(f"/v1/tasks/{task_id}", headers=headers)
        timeline = await client.get(f"/v1/tasks/{task_id}/timeline", headers=headers)
        cancelled = await client.post(
            f"/v1/tasks/{task_id}/cancel",
            headers=headers,
            json={"reason": "not authorized"},
        )
        return (
            fetched.status_code,
            timeline.status_code,
            cancelled.status_code,
            fetched.json()["detail"],
            absent.json()["detail"],
        )

    fetched, timeline, cancelled, detail, missing_detail = _run(scenario, tmp_path)

    assert (fetched, timeline, cancelled) == (404, 404, 404)
    assert detail == missing_detail


def test_a_malformed_task_timeline_cursor_is_a_bad_request(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> int:
        opened = await client.post("/v1/tasks", **_submission())
        task_id = opened.json()["task_id"]
        response = await client.get(
            f"/v1/tasks/{task_id}/timeline",
            headers=HEADERS,
            params={"cursor": "not-a-cursor"},
        )
        return response.status_code

    assert _run(scenario, tmp_path) == 400
