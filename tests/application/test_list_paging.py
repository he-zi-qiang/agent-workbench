"""Two paging properties the HTTP tests cannot reach.

A route caps ``limit`` in its own signature, so an HTTP client can never ask for
more than the ceiling and the service's own ``min`` is unobservable from there.
It is not redundant: the CLI, a script and a test all call the service, and the
comment claiming the ceiling holds for them needs something that fails when it
does not. That is the first test here.

The second is the keyset's tie-break. Two rows can share a ``created_at`` --
PostgreSQL's ``now()`` is per-transaction, so a batch inserted together does --
and a cursor ordered on the timestamp alone would then either repeat a row or
step over one. Sequential inserts in an HTTP test never collide, so the tie has
to be constructed on purpose.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence import (
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.application.tasks import (
    MAX_PAGE_LIMIT,
    SubmittedSemantics,
    TaskService,
)
from agent_workbench.domain.pagination import ListCursor
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.ports.task_registry import TaskRegistry, TaskRun, TaskSubmission

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
PRINCIPAL = PrincipalContext(tenant_id="tenant_a", principal_id="user_1")
TABLES = "approvals, task_runs, events, event_streams"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _semantics() -> SubmittedSemantics:
    return SubmittedSemantics(
        run_semantics_snapshot={"model": {"provider": "deepseek"}},
        run_semantics_revision="1.2:v1.3:abc0123456789def",
        policy_revision="policy-1",
        policy_fingerprint="f" * 16,
        authorization_envelope=AuthorizationEnvelope(),
    )


def _submission(key: str) -> TaskSubmission:
    return TaskSubmission.model_validate(
        {
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
    )


class _CountingRegistry:
    """Records the limit it was asked for, which is the thing under test."""

    def __init__(self) -> None:
        self.limits: list[int] = []

    async def list_for_owner(self, *, limit: int, **_: Any) -> tuple[TaskRun, ...]:
        self.limits.append(limit)
        return ()


def test_the_service_clamps_a_limit_no_http_client_could_send() -> None:
    """The route's own ``le=`` makes this unreachable over HTTP.

    It is still the boundary the CLI and every script call, so the ceiling is
    asserted where those callers meet it rather than only where FastAPI does.
    """

    registry = _CountingRegistry()
    service = TaskService(
        registry=registry,  # pyright: ignore[reportArgumentType]
        semantics=_semantics,
    )

    asyncio.run(service.list(PRINCIPAL, limit=10_000))

    assert registry.limits == [MAX_PAGE_LIMIT]


def test_the_service_refuses_a_limit_below_one() -> None:
    registry = _CountingRegistry()
    service = TaskService(
        registry=registry,  # pyright: ignore[reportArgumentType]
        semantics=_semantics,
    )

    with pytest.raises(ValueError, match="limit must be positive"):
        asyncio.run(service.list(PRINCIPAL, limit=0))

    assert registry.limits == []


def test_rows_sharing_a_timestamp_are_paged_exactly_once() -> None:
    """The tie-break, made observable by forcing the tie.

    Ordered on ``created_at`` alone, a cursor at a shared timestamp asks for
    "strictly older", which skips every sibling; asking for "older or equal"
    repeats them instead. Neither is visible until two rows collide.
    """

    dsn = _dsn()

    async def scenario() -> tuple[list[str], list[str]]:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            registry: TaskRegistry = PostgresTaskRegistry(engine)
            submitted = [
                (await registry.submit(_submission(f"tie_{index}"))).task_id
                for index in range(4)
            ]
            # One instant for all four, which sequential inserts never produce.
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE task_runs SET created_at = :moment"),
                    {"moment": datetime(2026, 7, 31, 12, 0, tzinfo=UTC)},
                )

            service = TaskService(registry=registry, semantics=_semantics)
            seen: list[str] = []
            after: ListCursor | None = None
            for _ in range(4):
                page = await service.list(PRINCIPAL, limit=2, after=after)
                seen.extend(task.task_id for task in page.tasks)
                after = page.cursor
                if after is None:
                    break
            return submitted, seen
        finally:
            await engine.dispose()

    submitted, seen = asyncio.run(scenario())

    assert len(seen) == len(set(seen)) == 4
    assert set(seen) == set(submitted)
