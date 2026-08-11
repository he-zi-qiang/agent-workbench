"""What ``GET /v1/tasks/{id}/timeline`` says about the rows it could not read.

The isolating replay stops one undecodable row from making a whole Task
unreachable, and ``TaskService.timeline`` already reports which positions it
set aside. These tests are about the last hop: whether an HTTP caller is told,
because a slice that lost rows and a slice that reached the end of the stream
produce the same shorter list of events, and a partial history presented as a
whole one is the failure this path introduces.

Real PostgreSQL, and real damage written into the row. The corruption has its
own tests against the event log; what is under test here is that the field
survives the trip through the response model rather than being recomputed,
defaulted or dropped, and a fake service could only prove the response model
copies whatever it was handed.
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
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.adapters.persistence.event_log import PostgresEventLog
from agent_workbench.adapters.persistence.models import events as events_table
from agent_workbench.apps.api.dependencies import ApiDependencies
from agent_workbench.apps.api.main import build_app
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings
from agent_workbench.domain.events import RunCompleted, RunStarted
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventScope

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
OWNER = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")

TABLES = "task_runs, events, event_streams"

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
    """One scenario against a truncated database, on one event loop.

    The scenario is handed the assembled dependencies as well as the client:
    the point of these tests is that HTTP agrees with the service behind it, so
    both answers have to be taken from the same process and the same rows.
    """

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


def _submission(*, key: str = "task-1") -> dict[str, Any]:
    return {
        "headers": {**HEADERS, "Idempotency-Key": key},
        "json": {
            "objective": "Compare dense and hybrid retrieval.",
            "max_revisions": 2,
            "knowledge_base_id": "kb_main",
        },
    }


async def _open_a_task_with_three_events(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> tuple[str, str, str]:
    """Submit a Task, then put two more durable events on its stream.

    Submission alone leaves a one-event stream, and one event cannot show a
    skip *between* events -- the case where a short list is least distinguish-
    able from the end of the stream. Returns the Task, its stream, and the
    ``event_id`` of the middle row, which is the one the damage lands on.
    """

    opened = await client.post("/v1/tasks", **_submission())
    task_id: str = opened.json()["task_id"]

    timeline = await client.get(f"/v1/tasks/{task_id}/timeline", headers=HEADERS)
    stream_id: str = timeline.json()["events"][0]["stream_id"]

    log = PostgresEventLog(engine)
    scope = EventScope(stream_id=stream_id, run_id="run_1")
    second = await log.append(
        scope, RunStarted(run_kind="task", model_profile="main", budget=BUDGET)
    )
    await log.append(scope, RunCompleted(stop_reason="completed"))
    return task_id, stream_id, second.event_id


async def _damage(engine: AsyncEngine, event_id: str) -> None:
    """Leave a payload with nothing but a discriminator nobody recognises.

    The shape a partially written row or a bad hand-edit leaves behind, and the
    one the event log's own tests use, so this stays a row the decoder rejects
    rather than a row this test invented a private failure mode for.
    """

    async with engine.begin() as connection:
        await connection.execute(
            update(events_table)
            .where(events_table.c.event_id == event_id)
            .values(payload={"kind": "RunStarted"})
        )


def test_a_skipped_row_is_reported_on_the_wire_exactly_as_the_service_saw_it(
    tmp_path: Path,
) -> None:
    """The whole point: an HTTP caller can tell a short slice from a full one.

    The service already knows which position it set aside. Asserting the
    response against *its* answer, and not just against the literal ``[2]``,
    is what makes this a passthrough test: a route that recomputed the field,
    rounded it to a boolean, or reported its own guess would have to coincide
    with the service to pass.
    """

    async def scenario(
        client: httpx.AsyncClient, dependencies: ApiDependencies, engine: AsyncEngine
    ) -> tuple[Any, ...]:
        task_id, _, middle = await _open_a_task_with_three_events(client, engine)
        await _damage(engine, middle)

        response = await client.get(f"/v1/tasks/{task_id}/timeline", headers=HEADERS)
        served = await dependencies.task_service.timeline(OWNER, task_id)
        return response, served

    response, served = _run(scenario, tmp_path)
    body = response.json()

    assert response.status_code == 200
    # The damage is real: the middle row did not come back, and the readable
    # events either side of it did. Without this the assertion below could pass
    # on a stream nothing was wrong with.
    assert [event["sequence"] for event in body["events"]] == [1, 3]
    assert body["skipped_sequences"] == [2]
    # ... and it is the service's answer, not a second opinion.
    assert body["skipped_sequences"] == list(served.skipped_sequences)
    assert len(body["skipped_sequences"]) == served.skipped
    # The cursor still moves past the damage, so this is a caller that can be
    # told about the skip *and* keep polling, not one wedged in front of it.
    assert body["cursor"] == f"{body['events'][0]['stream_id']}:3"


def test_a_clean_timeline_answers_exactly_as_it_did_before_the_field_existed(
    tmp_path: Path,
) -> None:
    """The control group. An implementation that always cries "skipped" dies here.

    Two claims, because the field can be got wrong in two directions. The
    response must still *claim* completeness when the slice is complete -- an
    empty list, present, rather than a count nobody can act on or a skip that
    was never there. And nothing a client already read may have moved: the key
    set gains this one name and no other, and every other field is byte-for-
    byte the response this endpoint returned before.
    """

    async def scenario(
        client: httpx.AsyncClient, dependencies: ApiDependencies, engine: AsyncEngine
    ) -> tuple[Any, ...]:
        task_id, stream_id, _ = await _open_a_task_with_three_events(client, engine)

        response = await client.get(f"/v1/tasks/{task_id}/timeline", headers=HEADERS)
        served = await dependencies.task_service.timeline(OWNER, task_id)
        return response, served, task_id, stream_id

    response, served, task_id, stream_id = _run(scenario, tmp_path)
    body = response.json()

    assert response.status_code == 200
    # Nothing was skipped, and the response says so rather than staying silent:
    # an absent field would read the same as a server that never looked.
    assert body["skipped_sequences"] == []
    assert served.skipped_sequences == ()
    # Exactly one new name on the wire: nothing was renamed or dropped, and no
    # second field (a count, a boolean) arrived that could later disagree.
    assert set(body) == {"task_id", "events", "cursor", "skipped_sequences"}
    # And every field a client already read still says what it said before this
    # change: the same Task, three decodable events in order, and a cursor at
    # the last position the slice reached.
    assert body["task_id"] == task_id
    assert body["cursor"] == f"{stream_id}:3"
    assert [event["sequence"] for event in body["events"]] == [1, 2, 3]
    assert [event["event_type"] for event in body["events"]] == [
        "TaskSubmitted",
        "RunStarted",
        "RunCompleted",
    ]
