"""The two collection routes, and what a keyset page has to guarantee.

These lists are the only way a client discovers anything. Until they existed a
caller had to already know a Task id to ask about it, and had to read a Task's
timeline to find the approval it was parked on -- so a person could not answer a
question they were never told was being asked.

Two properties are load-bearing and neither is visible from a single request.

A page is scoped by tenant *and* owner inside the query. The test for that is
not "another owner gets a 404" -- there is no id to refuse -- it is that their
rows are simply not in the answer, which is the same shape ``/v1/search`` has.

And a keyset page delivers every row exactly once *while the table is being
written to*. That is the whole reason it is not an offset: the test inserts
between the two pages and asserts the second page neither repeats nor skips.

Real PostgreSQL only: ordering, ties and row-value comparison are what is under
test, and an in-memory list would be testing the fake instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence import (
    PostgresApprovalStore,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.apps.api.main import build_app
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings
from agent_workbench.ports.task_registry import TaskSubmission

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

OWNER = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
NEIGHBOUR = {"x-tenant-id": "tenant_a", "x-principal-id": "user_2"}
OTHER_TENANT = {"x-tenant-id": "tenant_b", "x-principal-id": "user_1"}

TABLES = "approvals, task_runs, events, event_streams"


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


def _submission(key: str, **overrides: Any) -> TaskSubmission:
    base: dict[str, Any] = {
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
        "thread_id": f"thr_{key}",
        "graph_version": "v1",
        "input_ref": f"input_{key}",
        "input_fingerprint": hashlib.sha256(key.encode()).hexdigest(),
        "submission_dedup_key": f"dedup_{key}",
        "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
        "run_semantics_revision": "1.2:v1.3:abc0123456789def",
        "submitted_policy_revision": "policy-1",
        "submitted_policy_fingerprint": "f" * 16,
        "submitted_authorization_envelope": {},
    }
    base.update(overrides)
    return TaskSubmission.model_validate(base)


def _run(
    root: Path,
    scenario: Callable[[httpx.AsyncClient, Any], Awaitable[Any]],
) -> Any:
    _dsn()

    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            app, dependencies = build_app(project_api(_settings(root)), with_chat=False)
            transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
            try:
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://api.test"
                ) as client:
                    return await scenario(client, engine)
            finally:
                await dependencies.dispose()
        finally:
            await engine.dispose()

    return asyncio.run(execute())


async def _tasks(
    engine: Any, count: int, *, prefix: str = "a", **overrides: Any
) -> list[str]:
    """Submit ``count`` Tasks in order, oldest first.

    ``prefix`` keeps thread ids and dedup keys distinct across groups; they are
    globally unique, so two groups sharing a prefix would collide rather than
    produce the two independent owners a scope test needs.
    """

    registry = PostgresTaskRegistry(engine)
    ids: list[str] = []
    for index in range(count):
        task = await registry.submit(_submission(f"{prefix}_{index}", **overrides))
        ids.append(task.task_id)
    return ids


async def _approvals(engine: Any, task_ids: list[str], **overrides: Any) -> list[str]:
    store = PostgresApprovalStore(engine)
    ids: list[str] = []
    for index, task_id in enumerate(task_ids):
        record = await store.request(
            task_id=task_id,
            graph_node_operation_id=f"op_{index}",
            tenant_id=overrides.get("tenant_id", "tenant_a"),
            owner_id=overrides.get("owner_id", "user_1"),
        )
        ids.append(record.approval_id)
    return ids


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------


def test_a_task_list_contains_only_the_callers_own(tmp_path: Path) -> None:
    """Not a 404 -- there is no id here to refuse.

    Another owner's Tasks are not hidden from this answer, they were never
    selected into it, because the narrowing is two predicates in the query.
    """

    async def scenario(client: httpx.AsyncClient, engine: Any) -> Any:
        mine = await _tasks(engine, 2, prefix="mine")
        await _tasks(engine, 2, prefix="nb", owner_id="user_2")
        await _tasks(engine, 2, prefix="other", tenant_id="tenant_b")
        return mine, [
            await client.get("/v1/tasks", headers=OWNER),
            await client.get("/v1/tasks", headers=NEIGHBOUR),
            await client.get("/v1/tasks", headers=OTHER_TENANT),
        ]

    mine, (owner, neighbour, other_tenant) = _run(tmp_path, scenario)

    assert owner.status_code == 200
    assert {task["task_id"] for task in owner.json()["tasks"]} == set(mine)
    # A same-tenant neighbour sees its own two, and none of the owner's.
    assert {task["task_id"] for task in neighbour.json()["tasks"]}.isdisjoint(mine)
    # The cross-tenant caller has the *same principal id* as the owner and its
    # own two Tasks. An owner match alone would hand it the owner's as well, so
    # the property is disjointness rather than emptiness.
    other = {task["task_id"] for task in other_tenant.json()["tasks"]}
    assert len(other) == 2
    assert other.isdisjoint(mine)


def test_a_task_list_does_not_reflect_identity(tmp_path: Path) -> None:
    """Same projection as the single read: the caller had to be the owner."""

    async def scenario(client: httpx.AsyncClient, engine: Any) -> Any:
        await _tasks(engine, 1)
        return await client.get("/v1/tasks", headers=OWNER)

    body = _run(tmp_path, scenario).json()

    assert body["tasks"]
    for task in body["tasks"]:
        assert {"tenant_id", "owner_id", "thread_id", "input_ref"}.isdisjoint(task)


def test_an_approval_list_contains_only_the_callers_own(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient, engine: Any) -> Any:
        mine = await _approvals(engine, await _tasks(engine, 2, prefix="mine"))
        await _approvals(
            engine,
            await _tasks(engine, 1, prefix="nb", owner_id="user_2"),
            owner_id="user_2",
        )
        return mine, [
            await client.get("/v1/approvals", headers=OWNER),
            await client.get("/v1/approvals", headers=NEIGHBOUR),
        ]

    mine, (owner, neighbour) = _run(tmp_path, scenario)

    assert {row["approval_id"] for row in owner.json()["approvals"]} == set(mine)
    assert {row["approval_id"] for row in neighbour.json()["approvals"]}.isdisjoint(
        mine
    )


# --------------------------------------------------------------------------
# Order, filter and page
# --------------------------------------------------------------------------


def test_a_task_list_is_newest_first(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient, engine: Any) -> Any:
        submitted = await _tasks(engine, 4)
        return submitted, await client.get("/v1/tasks", headers=OWNER)

    submitted, response = _run(tmp_path, scenario)

    assert [task["task_id"] for task in response.json()["tasks"]] == list(
        reversed(submitted)
    )


def test_a_status_filter_narrows_and_grants_nothing(tmp_path: Path) -> None:
    """A filter is not a lens onto other people's rows."""

    async def scenario(client: httpx.AsyncClient, engine: Any) -> Any:
        await _tasks(engine, 2, prefix="mine")
        await _tasks(engine, 2, prefix="nb", owner_id="user_2")
        registry = PostgresTaskRegistry(engine)
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        return [
            await client.get("/v1/tasks?status=queued", headers=OWNER),
            await client.get("/v1/tasks?status=running", headers=OWNER),
            await client.get("/v1/tasks?status=queued&status=running", headers=OWNER),
            await client.get("/v1/tasks?status=queued", headers=NEIGHBOUR),
        ]

    queued, running, both, neighbour = _run(tmp_path, scenario)

    assert len(both.json()["tasks"]) == 2
    assert len(queued.json()["tasks"]) + len(running.json()["tasks"]) == 2
    # Naming a status does not widen who the rows may belong to.
    assert len(neighbour.json()["tasks"]) == 2
    assert {t["task_id"] for t in neighbour.json()["tasks"]}.isdisjoint(
        {t["task_id"] for t in both.json()["tasks"]}
    )


def test_a_full_page_carries_a_cursor_and_a_short_one_does_not(
    tmp_path: Path,
) -> None:
    """The client stops on an absent cursor, so the two must never disagree."""

    async def scenario(client: httpx.AsyncClient, engine: Any) -> Any:
        await _tasks(engine, 3)
        return [
            await client.get("/v1/tasks?limit=3", headers=OWNER),
            await client.get("/v1/tasks?limit=5", headers=OWNER),
            await client.get("/v1/tasks?limit=5", headers=NEIGHBOUR),
        ]

    exact, short, empty = _run(tmp_path, scenario)

    # Exactly full: there may or may not be more, and only another request can
    # say, so a cursor is offered.
    assert exact.json()["cursor"] is not None
    assert short.json()["cursor"] is None
    # An empty page with a cursor would make "nothing here" look like "keep
    # going".
    assert empty.json()["tasks"] == []
    assert empty.json()["cursor"] is None


def test_paging_delivers_every_row_once_while_rows_are_being_added(
    tmp_path: Path,
) -> None:
    """The property an offset cannot have.

    Between the two pages a newer Task arrives. Ordered newest first, an offset
    of 2 would step over one of the rows the first page already skipped past;
    the keyset asks for "older than the last row I saw", which does not move.
    """

    async def scenario(client: httpx.AsyncClient, engine: Any) -> Any:
        first_four = await _tasks(engine, 4, prefix="early")
        page_one = await client.get("/v1/tasks?limit=2", headers=OWNER)
        # A new Task lands at the newest end, where an offset-based second page
        # would be measuring from.
        await _tasks(engine, 1, prefix="late")
        cursor = page_one.json()["cursor"]
        page_two = await client.get(f"/v1/tasks?limit=2&cursor={cursor}", headers=OWNER)
        return first_four, page_one, page_two

    first_four, page_one, page_two = _run(tmp_path, scenario)

    seen = [t["task_id"] for t in page_one.json()["tasks"]] + [
        t["task_id"] for t in page_two.json()["tasks"]
    ]
    assert len(seen) == len(set(seen)) == 4
    # The four that existed when paging started, newest first, none skipped.
    assert seen == list(reversed(first_four))


def test_an_approval_status_filter_selects_the_pending_queue(
    tmp_path: Path,
) -> None:
    """``status=pending`` is the query a person actually makes.

    It is deliberately not the default: a queue that hid decided approvals
    would make "I already answered that" look like "it is gone".
    """

    async def scenario(client: httpx.AsyncClient, engine: Any) -> Any:
        approvals = await _approvals(engine, await _tasks(engine, 2, prefix="mine"))
        registry = PostgresTaskRegistry(engine)
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        await registry.await_approval(claim.lease)
        await PostgresApprovalStore(engine).decide(
            approvals[0],
            decision="approved",
            decision_version=1,
            decided_by="user_1",
        )
        return approvals, [
            await client.get("/v1/approvals?status=pending", headers=OWNER),
            await client.get("/v1/approvals", headers=OWNER),
        ]

    approvals, (pending, everything) = _run(tmp_path, scenario)

    assert {row["approval_id"] for row in pending.json()["approvals"]} == {approvals[1]}
    assert len(everything.json()["approvals"]) == 2


# --------------------------------------------------------------------------
# Transport input
# --------------------------------------------------------------------------


#: The first three are not base64 at all; the rest decode cleanly and are
#: rejected on their contents, which is what keeps this test from only proving
#: that base64 decoding works.
MALFORMED_CURSORS = [
    "not a cursor",
    "%%%%",
    "\u00e9",
    "bm8tc2VwYXJhdG9yLWhlcmU",
    "bm90LWEtdGltZXN0YW1wfHRhc2tfMQ",
    "MjAyNi0wNy0zMVQwMDowMDowMCswMDowMHw",
    "MjAyNi0wNy0zMVQwMDowMDowMCswMDowMHxub3QgYSB2YWxpZCBpZGVudGlmaWVy",
]


@pytest.mark.parametrize("cursor", MALFORMED_CURSORS)
def test_a_malformed_cursor_is_a_bad_request_not_a_server_error(
    cursor: str, tmp_path: Path
) -> None:
    """Transport input, so a decode failure is the client's problem, not a 500."""

    async def scenario(client: httpx.AsyncClient, _: Any) -> Any:
        return [
            await client.get(f"/v1/tasks?cursor={cursor}", headers=OWNER),
            await client.get(f"/v1/approvals?cursor={cursor}", headers=OWNER),
        ]

    for response in _run(tmp_path, scenario):
        assert response.status_code == 400


def test_a_limit_beyond_the_ceiling_is_refused_rather_than_clamped(
    tmp_path: Path,
) -> None:
    """A silently clamped limit reads as "that is all there is"."""

    async def scenario(client: httpx.AsyncClient, _: Any) -> Any:
        return [
            await client.get("/v1/tasks?limit=10000", headers=OWNER),
            await client.get("/v1/approvals?limit=10000", headers=OWNER),
            await client.get("/v1/tasks?limit=0", headers=OWNER),
        ]

    for response in _run(tmp_path, scenario):
        assert response.status_code == 422


def test_an_unauthenticated_list_is_refused(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient, _: Any) -> Any:
        return [
            await client.get("/v1/tasks"),
            await client.get("/v1/approvals"),
        ]

    for response in _run(tmp_path, scenario):
        assert response.status_code == 401


def test_a_cursor_survives_being_a_query_parameter(tmp_path: Path) -> None:
    """The reason it is base64url and not the plain form.

    An ISO timestamp ends in ``+00:00``, and ``+`` in a query string decodes to
    a space -- so an unencoded cursor would not fail loudly, it would arrive as
    a cursor for a slightly different instant and quietly skip or repeat rows.
    """

    async def scenario(client: httpx.AsyncClient, engine: Any) -> Any:
        submitted = await _tasks(engine, 4, prefix="page")
        first = await client.get("/v1/tasks?limit=2", headers=OWNER)
        cursor = first.json()["cursor"]
        # Sent raw, exactly as a client that did not encode anything would.
        second = await client.get(f"/v1/tasks?limit=2&cursor={cursor}", headers=OWNER)
        return submitted, cursor, first, second

    submitted, cursor, first, second = _run(tmp_path, scenario)

    assert "+" not in cursor and " " not in cursor
    assert second.status_code == 200
    seen = [t["task_id"] for t in first.json()["tasks"]] + [
        t["task_id"] for t in second.json()["tasks"]
    ]
    assert seen == list(reversed(submitted))
