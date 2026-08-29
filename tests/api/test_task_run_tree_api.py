"""``GET /v1/tasks/{id}/runs`` and the narrowed timeline, over HTTP.

Both have existed since ADR-083 and both were reachable only from
``tests/application`` and ``tests/contracts`` -- the service and the store had
tests, the wire did not. That gap is why they are here: these are the two
routes a deep link into one sub-agent lands on, so what a browser receives is
the thing worth pinning, not what the service returned before a response model
copied it.

Real PostgreSQL and a real delegation written onto the stream, for the same
reason ``test_task_timeline_skips.py`` uses one: a fake service could only
prove the response model copies whatever it was handed.
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
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.adapters.persistence.event_log import PostgresEventLog
from agent_workbench.apps.api.dependencies import ApiDependencies
from agent_workbench.apps.api.main import build_app
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings
from agent_workbench.domain.events import (
    AgentCompleted,
    AgentDelegated,
    RunCompleted,
    RunStarted,
)
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventScope

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
TABLES = "task_runs, events, event_streams"

PARENT_RUN = "run_parent"
CHILD_RUN = "run_child"
BUDGET = RunBudget(max_steps=4, max_tool_calls=4)


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


def _run(
    scenario: Callable[
        [httpx.AsyncClient, ApiDependencies, AsyncEngine], Awaitable[Any]
    ],
    root: Path,
) -> Any:
    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))

            app, dependencies = build_app(project_api(_settings(root)), with_chat=False)
            transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
            try:
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://api.test",
                ) as client:
                    return await scenario(client, dependencies, engine)
            finally:
                await dependencies.dispose()
        finally:
            await engine.dispose()

    return asyncio.run(execute())


async def _a_task_that_delegated_once(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> str:
    """Submit a Task, then write a parent run that delegates and a child run.

    Written straight onto the stream rather than driven through a Worker: what
    is under test is the two read paths, and a real delegation would put thirty
    minutes of model calls in front of them.
    """

    opened = await client.post(
        "/v1/tasks",
        headers={**HEADERS, "Idempotency-Key": "task-runs-1"},
        json={
            "objective": "Compare dense and hybrid retrieval.",
            "max_revisions": 2,
            "knowledge_base_id": "kb_main",
        },
    )
    task_id: str = opened.json()["task_id"]

    timeline = await client.get(f"/v1/tasks/{task_id}/timeline", headers=HEADERS)
    stream_id: str = timeline.json()["events"][0]["stream_id"]

    log = PostgresEventLog(engine)
    parent = EventScope(stream_id=stream_id, run_id=PARENT_RUN)
    child = EventScope(stream_id=stream_id, run_id=CHILD_RUN)

    await log.append(
        parent, RunStarted(run_kind="task", model_profile="main", budget=BUDGET)
    )
    await log.append(
        parent,
        AgentDelegated(child_agent_run_id=CHILD_RUN, profile_name="analyst"),
    )
    # Interleaved on purpose: the child writes between the delegation and the
    # parent's own completion, which is exactly the ordering that makes a flat
    # timeline unreadable and a tree worth serving.
    await log.append(
        child, RunStarted(run_kind="task", model_profile="main", budget=BUDGET)
    )
    await log.append(child, RunCompleted(stop_reason="completed"))
    await log.append(
        parent,
        AgentCompleted(
            child_agent_run_id=CHILD_RUN,
            status="completed",
            stop_reason="completed",
        ),
    )
    await log.append(parent, RunCompleted(stop_reason="completed"))
    return task_id


def test_the_tree_arrives_over_http_with_the_child_under_its_parent(
    tmp_path: Path,
) -> None:
    """The one thing this route exists to say, and the one a flat list cannot.

    Nesting rather than a parent column: "this agent was started *by* that one"
    is the whole claim, and a client that had to match ids to recover it would
    be rebuilding the tree the endpoint is named after.
    """

    async def scenario(
        client: httpx.AsyncClient, _dependencies: ApiDependencies, engine: AsyncEngine
    ) -> dict[str, Any]:
        task_id = await _a_task_that_delegated_once(client, engine)
        response = await client.get(f"/v1/tasks/{task_id}/runs", headers=HEADERS)
        assert response.status_code == 200
        return response.json()

    body = _run(scenario, tmp_path)

    assert body["complete"] is True
    roots = body["roots"]
    assert [node["run_id"] for node in roots] == [PARENT_RUN]
    [parent] = roots
    assert parent["parent_run_id"] is None
    assert [child["run_id"] for child in parent["children"]] == [CHILD_RUN]
    [child] = parent["children"]
    assert child["parent_run_id"] == PARENT_RUN
    # The name the parent gave it when it delegated. Not guessed from the id:
    # a run with no delegation behind it carries `null` here, and the client
    # falls back to the id rather than dressing one up as a name.
    assert child["definition_name"] == "analyst"
    assert child["status"] == "completed"


def test_narrowing_the_timeline_to_one_run_returns_only_that_run(
    tmp_path: Path,
) -> None:
    """The other half of a deep link: the tree names a run, this reads it.

    Asserted against the unnarrowed page rather than against a literal count,
    so a route that ignored the parameter would have to coincide with a filter
    to pass.
    """

    async def scenario(
        client: httpx.AsyncClient, _dependencies: ApiDependencies, engine: AsyncEngine
    ) -> tuple[Any, Any]:
        task_id = await _a_task_that_delegated_once(client, engine)
        everything = await client.get(f"/v1/tasks/{task_id}/timeline", headers=HEADERS)
        narrowed = await client.get(
            f"/v1/tasks/{task_id}/timeline",
            headers=HEADERS,
            params={"run_id": CHILD_RUN},
        )
        assert narrowed.status_code == 200
        return everything.json(), narrowed.json()

    everything, narrowed = _run(scenario, tmp_path)

    assert {event["run_id"] for event in narrowed["events"]} == {CHILD_RUN}
    assert [event["event_type"] for event in narrowed["events"]] == [
        "RunStarted",
        "RunCompleted",
    ]
    # And the control: the child's events are a strict subset of a page that
    # holds the parent's too, so the filter is removing rows rather than the
    # fixture only ever having written the child's.
    assert len(narrowed["events"]) < len(everything["events"])
    assert {event["run_id"] for event in everything["events"]} > {CHILD_RUN}


def test_a_run_id_nobody_ever_wrote_answers_with_an_empty_page(
    tmp_path: Path,
) -> None:
    """Empty, not 404, and the difference is a disclosure decision.

    Narrowing selects among events this principal was already entitled to --
    the Task read is what decided that. So an id that does not exist and an id
    that belongs to somebody else's Task have to answer alike, or the status
    code becomes a way to ask whether a run exists.
    """

    async def scenario(
        client: httpx.AsyncClient, _dependencies: ApiDependencies, engine: AsyncEngine
    ) -> Any:
        task_id = await _a_task_that_delegated_once(client, engine)
        response = await client.get(
            f"/v1/tasks/{task_id}/timeline",
            headers=HEADERS,
            params={"run_id": "run_nobody_wrote"},
        )
        assert response.status_code == 200
        return response.json()

    body = _run(scenario, tmp_path)

    assert body["events"] == []
    assert body["cursor"] is None


def test_a_narrowed_page_never_claims_to_have_lost_rows(tmp_path: Path) -> None:
    """``skipped_sequences`` is empty on a narrowed read, by construction.

    The isolating replay has no narrowed form, and deliberately: a row that
    cannot be decoded has no readable ``run_id``, so it belongs neither to the
    narrowed page nor to the rest. Pinned here because the field is present on
    the response either way, and a reader who saw it populated on a narrowed
    page would take it for damage this narrowing had found.
    """

    async def scenario(
        client: httpx.AsyncClient, _dependencies: ApiDependencies, engine: AsyncEngine
    ) -> Any:
        task_id = await _a_task_that_delegated_once(client, engine)
        response = await client.get(
            f"/v1/tasks/{task_id}/timeline",
            headers=HEADERS,
            params={"run_id": CHILD_RUN},
        )
        return response.json()

    assert _run(scenario, tmp_path)["skipped_sequences"] == []
