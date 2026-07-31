"""The two approval routes, and the matrix that must not leak an approval.

The plan's API security regression is run here in full: same tenant, cross
tenant with a *known* id, cross tenant with a random one, and a same-tenant
neighbour. Every non-owner answer has to be identical -- the same status and the
same body -- because a difference between "no such approval" and "not yours" is
the disclosure itself.

The decision route gets the same matrix as the read, and one more property: a
refused decision must leave the ledger untouched. A 404 that had already
requeued somebody's Task would be a leak with consequences.

Real PostgreSQL only: an approval is a row, and what is under test is who may
read and move it.
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
from agent_workbench.workflows.approval import APPROVAL_OPERATION_ID

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

OWNER = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
OTHER_OWNER = {"x-tenant-id": "tenant_a", "x-principal-id": "user_2"}
OTHER_TENANT = {"x-tenant-id": "tenant_b", "x-principal-id": "user_1"}
#: A tenant *and* a principal the approval has nothing to do with.
STRANGER = {"x-tenant-id": "tenant_b", "x-principal-id": "user_9"}

UNKNOWN_APPROVAL_ID = "approval_00000000000000000000000000000000"

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


def _submission(**overrides: Any) -> TaskSubmission:
    base: dict[str, Any] = {
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
        "thread_id": "thr_1",
        "graph_version": "v1",
        "input_ref": "input_1",
        "input_fingerprint": hashlib.sha256(b"input_1").hexdigest(),
        "submission_dedup_key": "dedup_1",
        "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
        "run_semantics_revision": "1.2:v1.3:abc0123456789def",
        "submitted_policy_revision": "policy-1",
        "submitted_policy_fingerprint": "f" * 16,
        "submitted_authorization_envelope": {},
    }
    base.update(overrides)
    return TaskSubmission.model_validate(base)


async def _waiting_approval(engine: Any) -> str:
    """A Task parked on a human, and the approval it is parked on.

    Built through the Registry and the ledger rather than by running a graph:
    what these tests are about is the HTTP boundary, and a graph run here would
    only make the failure modes harder to read.
    """

    registry = PostgresTaskRegistry(engine)
    task = await registry.submit(_submission())
    claim = await registry.claim_next("worker_1", lease_seconds=60)
    assert claim is not None
    await registry.await_approval(claim.lease)
    record = await PostgresApprovalStore(engine).request(
        task_id=task.task_id,
        graph_node_operation_id=APPROVAL_OPERATION_ID,
        tenant_id="tenant_a",
        owner_id="user_1",
    )
    return record.approval_id


def _run(
    root: Path,
    scenario: Callable[[httpx.AsyncClient, str, Any], Awaitable[Any]],
) -> Any:
    _dsn()

    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            approval_id = await _waiting_approval(engine)

            app, dependencies = build_app(project_api(_settings(root)), with_chat=False)
            transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
            try:
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://api.test"
                ) as client:
                    return await scenario(client, approval_id, engine)
            finally:
                await dependencies.dispose()
        finally:
            await engine.dispose()

    return asyncio.run(execute())


# --------------------------------------------------------------------------
# The owner's path
# --------------------------------------------------------------------------


def test_the_owner_can_read_the_approval_without_learning_who_owns_it(
    tmp_path: Path,
) -> None:
    async def scenario(client: httpx.AsyncClient, approval_id: str, _: Any) -> Any:
        return await client.get(f"/v1/approvals/{approval_id}", headers=OWNER)

    response = _run(tmp_path, scenario)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "pending"
    assert body["decision_version"] == 0
    assert body["decided_at"] is None
    # The projection is deliberately narrow: identity never travels back out.
    assert {
        "tenant_id",
        "owner_id",
        "decided_by",
        "graph_node_operation_id",
    }.isdisjoint(body)


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_a_decision_records_the_answer_and_requeues_the_task(
    decision: str, tmp_path: Path
) -> None:
    async def scenario(
        client: httpx.AsyncClient, approval_id: str, engine: Any
    ) -> tuple[Any, ...]:
        answered = await client.post(
            f"/v1/approvals/{approval_id}/decisions",
            headers=OWNER,
            json={"decision": decision, "decision_version": 1},
        )
        registry = PostgresTaskRegistry(engine)
        stored = await PostgresApprovalStore(engine).get(approval_id)
        assert stored is not None
        task = await registry.get(stored.task_id)
        assert task is not None
        return answered, stored, task

    answered, stored, task = _run(tmp_path, scenario)

    assert answered.status_code == 200
    assert answered.json()["status"] == decision
    assert answered.json()["decision_version"] == 1
    assert answered.json()["decided_at"] is not None
    # Taken from the authenticated principal, never from the request body.
    assert stored.decided_by == "user_1"
    assert (task.status, task.resume_kind) == ("queued", "approval")


def test_a_replayed_decision_is_harmless_and_a_later_one_arrives_too_late(
    tmp_path: Path,
) -> None:
    """A double-clicked button is one decision. Changing your mind is not.

    The two POSTs after the first differ only in their version, and they get
    opposite treatment, which is what shows the version is the idempotency key
    rather than decoration. The replay is the same answer and returns the stored
    record; the *newer* version is a second answer, and by then the decision it
    would supersede has already requeued the Task -- so the ledger refuses it
    rather than reopening something a Worker may already be running.
    """

    async def scenario(
        client: httpx.AsyncClient, approval_id: str, engine: Any
    ) -> tuple[Any, ...]:
        first = await client.post(
            f"/v1/approvals/{approval_id}/decisions",
            headers=OWNER,
            json={"decision": "approved", "decision_version": 1},
        )
        replay = await client.post(
            f"/v1/approvals/{approval_id}/decisions",
            headers=OWNER,
            json={"decision": "rejected", "decision_version": 1},
        )
        late = await client.post(
            f"/v1/approvals/{approval_id}/decisions",
            headers=OWNER,
            json={"decision": "rejected", "decision_version": 2},
        )
        stored = await PostgresApprovalStore(engine).get(approval_id)
        assert stored is not None
        return first, replay, late, stored

    first, replay, late, stored = _run(tmp_path, scenario)

    assert first.json()["status"] == "approved"
    # The stored answer, not the one the replay asked for.
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert late.status_code == 409
    assert (stored.status, stored.decision_version) == ("approved", 1)


def test_deciding_a_task_that_is_no_longer_waiting_is_a_conflict(
    tmp_path: Path,
) -> None:
    """Somebody cancelled the work while a human was thinking.

    A 409 rather than a 404: this caller was allowed to see the approval, so
    hiding it at the moment it stops being decidable would be a different lie.
    """

    async def scenario(client: httpx.AsyncClient, approval_id: str, engine: Any) -> Any:
        registry = PostgresTaskRegistry(engine)
        stored = await PostgresApprovalStore(engine).get(approval_id)
        assert stored is not None
        await registry.cancel(stored.task_id, reason="the owner asked")
        refused = await client.post(
            f"/v1/approvals/{approval_id}/decisions",
            headers=OWNER,
            json={"decision": "approved", "decision_version": 1},
        )
        after = await PostgresApprovalStore(engine).get(approval_id)
        assert after is not None
        task = await registry.get(stored.task_id)
        assert task is not None
        return refused, after.status, task.status

    refused, approval_status, task_status = _run(tmp_path, scenario)

    assert refused.status_code == 409
    assert approval_status == "pending"
    assert task_status == "cancelled"


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "maybe", "decision_version": 1},
        {"decision": "approved", "decision_version": 0},
        {"decision": "approved"},
        {"decision": "approved", "decision_version": 1, "decided_by": "somebody_else"},
    ],
)
def test_a_malformed_or_over_reaching_decision_is_refused(
    body: dict[str, Any], tmp_path: Path
) -> None:
    """The last case is the interesting one: a caller naming its own decider.

    ``extra="forbid"`` turns that into a 422 rather than a silently ignored
    field, because an audit trail whose author is client-supplied is not one.
    """

    async def scenario(client: httpx.AsyncClient, approval_id: str, _: Any) -> Any:
        return await client.post(
            f"/v1/approvals/{approval_id}/decisions", headers=OWNER, json=body
        )

    assert _run(tmp_path, scenario).status_code == 422


# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [OTHER_OWNER, OTHER_TENANT, STRANGER])
@pytest.mark.parametrize("known", [True, False])
def test_nobody_else_can_read_an_approval_or_tell_whether_it_exists(
    headers: dict[str, str], known: bool, tmp_path: Path
) -> None:
    """Four rows of the plan's matrix, and the property that ties them together.

    A known id and a random one must produce the same status *and* the same
    body for every non-owner. If they differed, the endpoint would be an oracle
    for which approval ids exist.
    """

    async def scenario(client: httpx.AsyncClient, approval_id: str, _: Any) -> Any:
        target = approval_id if known else UNKNOWN_APPROVAL_ID
        return await client.get(f"/v1/approvals/{target}", headers=headers)

    response = _run(tmp_path, scenario)

    assert response.status_code == 404
    assert response.json() == {"detail": "approval not found"}


@pytest.mark.parametrize("headers", [OTHER_OWNER, OTHER_TENANT, STRANGER])
def test_nobody_else_can_decide_an_approval_or_move_the_task_behind_it(
    headers: dict[str, str], tmp_path: Path
) -> None:
    """The refusal has to happen *before* the ledger, not by it.

    The ledger would refuse a second decision on its own, but by then the first
    one has already requeued a Task its owner never released. So the assertion
    is not only the 404: the approval is still pending and the Task is still
    waiting afterwards.
    """

    async def scenario(
        client: httpx.AsyncClient, approval_id: str, engine: Any
    ) -> tuple[Any, ...]:
        refused = await client.post(
            f"/v1/approvals/{approval_id}/decisions",
            headers=headers,
            json={"decision": "approved", "decision_version": 1},
        )
        stored = await PostgresApprovalStore(engine).get(approval_id)
        assert stored is not None
        task = await PostgresTaskRegistry(engine).get(stored.task_id)
        assert task is not None
        return refused, stored.status, stored.decided_by, task.status, task.resume_kind

    refused, approval_status, decided_by, task_status, resume_kind = _run(
        tmp_path, scenario
    )

    assert refused.status_code == 404
    assert refused.json() == {"detail": "approval not found"}
    assert (approval_status, decided_by) == ("pending", None)
    assert (task_status, resume_kind) == ("waiting_approval", None)


def test_the_owner_and_a_stranger_get_different_answers_for_the_same_id(
    tmp_path: Path,
) -> None:
    """The control group for the whole matrix.

    Every assertion above is a 404, and a 404 is also what a broken route
    returns. This is the one test that shows the probe is pointed at something
    that does answer.
    """

    async def scenario(
        client: httpx.AsyncClient, approval_id: str, _: Any
    ) -> tuple[Any, Any]:
        return (
            await client.get(f"/v1/approvals/{approval_id}", headers=OWNER),
            await client.get(f"/v1/approvals/{approval_id}", headers=OTHER_TENANT),
        )

    owner, stranger = _run(tmp_path, scenario)

    assert owner.status_code == 200
    assert stranger.status_code == 404


def test_an_unauthenticated_caller_is_challenged_rather_than_told_nothing_exists(
    tmp_path: Path,
) -> None:
    async def scenario(client: httpx.AsyncClient, approval_id: str, _: Any) -> Any:
        return await client.get(f"/v1/approvals/{approval_id}")

    assert _run(tmp_path, scenario).status_code == 401


# --------------------------------------------------------------------------
# Finding the approval in the first place
# --------------------------------------------------------------------------


def test_the_task_timeline_is_how_a_client_learns_the_approval_id(
    tmp_path: Path,
) -> None:
    """There is no endpoint that lists approvals, and there must not need to be.

    The id reaches the owner through the Task's own timeline, which is already
    authorized the same way. Without this event the only way to decide an
    approval would be to guess its id.
    """

    async def scenario(client: httpx.AsyncClient, approval_id: str, engine: Any) -> Any:
        stored = await PostgresApprovalStore(engine).get(approval_id)
        assert stored is not None
        return await client.get(f"/v1/tasks/{stored.task_id}/timeline", headers=OWNER)

    timeline = _run(tmp_path, scenario)
    events = timeline.json()["events"]
    requested = [
        event for event in events if event["event_type"] == "TaskApprovalRequested"
    ]

    assert timeline.status_code == 200
    assert len(requested) == 1
    assert requested[0]["payload"]["approval_id"].startswith("approval_")
    assert requested[0]["payload"]["graph_node_operation_id"] == APPROVAL_OPERATION_ID
