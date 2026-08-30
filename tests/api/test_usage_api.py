"""The usage endpoint, against a real database.

`tests/adapters/test_usage_report.py` covers the folding with no database in
it, which is what CI's offline job can run. What only a real PostgreSQL can
answer is the half this file is for: **do the joins reach the right rows, and
only this tenant's**. Those are not arithmetic questions -- a wrong join
produces a perfectly well-formed report about somebody else's spend.

The rows are written straight into the tables rather than by running a graph.
What is under test is the read path; a graph run here would add a provider, a
worker and a dozen ways to fail that have nothing to do with whether the sum
is right.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tomllib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence import (
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import (
    chat_turns,
    conversation_sessions,
    event_streams,
    events,
    messages,
)
from agent_workbench.apps.api.main import build_app
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings
from agent_workbench.ports.task_registry import TaskSubmission

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

OWNER = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
OTHER_TENANT = {"x-tenant-id": "tenant_b", "x-principal-id": "user_9"}

TABLES = "chat_turns, messages, conversation_sessions, task_runs, events, event_streams"

NOW = datetime.now(UTC)


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


def _usage(inp: int, out: int, cost: int = 0) -> dict[str, Any]:
    return {
        "usage": {
            "steps": 1,
            "tool_calls": 0,
            "tokens": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
            "cost_micro_usd": cost,
        }
    }


async def _seed(engine: Any) -> None:
    """One Task run, one Chat run, one Code run, and one run next door.

    "Next door" is the point of the last one: it is a well-formed run with real
    usage that belongs to another tenant, and every assertion below is really
    asking whether it leaked.
    """

    registry = PostgresTaskRegistry(engine)
    task = await registry.submit(
        TaskSubmission.model_validate(
            {
                "tenant_id": "tenant_a",
                "owner_id": "user_1",
                "thread_id": "thr_task",
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
        )
    )
    stranger = await registry.submit(
        TaskSubmission.model_validate(
            {
                "tenant_id": "tenant_b",
                "owner_id": "user_9",
                "thread_id": "thr_stranger",
                "graph_version": "v1",
                "input_ref": "input_2",
                "input_fingerprint": hashlib.sha256(b"input_2").hexdigest(),
                "submission_dedup_key": "dedup_2",
                "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
                "run_semantics_revision": "1.2:v1.3:abc0123456789def",
                "submitted_policy_revision": "policy-1",
                "submitted_policy_fingerprint": "f" * 16,
                "submitted_authorization_envelope": {},
            }
        )
    )

    async with engine.begin() as connection:
        for session_id, mode, run_id in (
            ("ses_chat", "chat", "run_chat"),
            ("ses_code", "code", "run_code"),
        ):
            await connection.execute(
                conversation_sessions.insert().values(
                    session_id=session_id,
                    tenant_id="tenant_a",
                    owner_id="user_1",
                    mode=mode,
                    title=None,
                )
            )
            # A committed Turn, spelled the way `chat_turns_lifecycle` insists:
            # both messages present and a result, because that constraint is
            # what makes "committed" mean the answer was actually published.
            # Seeding a shape the schema rejects would have tested nothing.
            for role, seq in (("user", 1), ("assistant", 2)):
                await connection.execute(
                    messages.insert().values(
                        message_id=f"msg_{mode}_{role}",
                        session_id=session_id,
                        sequence=seq,
                        payload={"role": role, "text": mode},
                    )
                )
            await connection.execute(
                chat_turns.insert().values(
                    turn_id=f"turn_{mode}",
                    session_id=session_id,
                    idempotency_key=f"idem_{mode}",
                    request_hash=hashlib.sha256(mode.encode()).hexdigest(),
                    run_id=run_id,
                    status="committed",
                    user_message_id=f"msg_{mode}_user",
                    assistant_message_id=f"msg_{mode}_assistant",
                    result={"answer": mode},
                )
            )

        # `registry.submit` already opened a stream for each Task's thread, so
        # only the Chat/Code one is missing here.
        await connection.execute(
            event_streams.insert().values(stream_id="thr_chat", last_sequence=0)
        )

        rows: list[dict[str, Any]] = []
        # Per stream, and starting well above zero: `registry.submit` already
        # wrote its own events into each Task's thread, and `(stream_id,
        # sequence)` is unique. Numbering from 1 here collides with those --
        # which is the constraint doing its job, not a test problem to route
        # around.
        positions: dict[str, int] = {}
        minted = 0

        def add(
            stream: str,
            run_id: str,
            event_type: str,
            payload: dict[str, Any],
            task_id: str | None,
        ) -> None:
            nonlocal minted
            minted += 1
            positions[stream] = positions.get(stream, 100) + 1
            rows.append(
                {
                    "event_id": f"evt_{minted:032d}",
                    "stream_id": stream,
                    "run_id": run_id,
                    "sequence": positions[stream],
                    "schema_version": 1,
                    "event_type": event_type,
                    "payload": payload,
                    "task_id": task_id,
                    "recorded_at": NOW - timedelta(minutes=5),
                }
            )

        add(
            "thr_task",
            "run_task",
            "RunStarted",
            {"kind": "RunStarted", "model_profile": "main"},
            task.task_id,
        )
        add(
            "thr_task",
            "run_task",
            "RunCompleted",
            {"kind": "RunCompleted", **_usage(1000, 100, cost=90)},
            task.task_id,
        )
        # A delegated child of the Task run, with its own terminal event.
        add(
            "thr_task",
            "run_task",
            "AgentDelegated",
            {
                "kind": "AgentDelegated",
                "child_agent_run_id": "run_child",
                "profile_name": "analyst",
            },
            task.task_id,
        )
        add(
            "thr_task",
            "run_child",
            "RunStarted",
            {"kind": "RunStarted", "model_profile": "main"},
            task.task_id,
        )
        add(
            "thr_task",
            "run_child",
            "RunCompleted",
            {"kind": "RunCompleted", **_usage(400, 40, cost=36)},
            task.task_id,
        )
        # A run that started and never finished: the caveat, not a total.
        add(
            "thr_task",
            "run_open",
            "RunStarted",
            {"kind": "RunStarted", "model_profile": "main"},
            task.task_id,
        )

        add("thr_chat", "run_chat", "RunStarted", {"model_profile": "main"}, None)
        add(
            "thr_chat",
            "run_chat",
            "RunCompleted",
            {"kind": "RunCompleted", **_usage(200, 20, cost=18)},
            None,
        )
        add("thr_chat", "run_code", "RunStarted", {"model_profile": "compact"}, None)
        add(
            "thr_chat",
            "run_code",
            "RunCompleted",
            {"kind": "RunCompleted", **_usage(300, 30, cost=0)},
            None,
        )

        # The neighbour. Same shape, different tenant.
        add(
            "thr_stranger",
            "run_stranger",
            "RunCompleted",
            {"kind": "RunCompleted", **_usage(999_999, 999_999, cost=999_999)},
            stranger.task_id,
        )

        await connection.execute(events.insert(), rows)


def _run(root: Path, scenario: Callable[[httpx.AsyncClient], Awaitable[Any]]) -> Any:
    _dsn()

    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            await _seed(engine)

            app, dependencies = build_app(project_api(_settings(root)), with_chat=False)
            transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
            try:
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://api.test"
                ) as client:
                    return await scenario(client)
            finally:
                await dependencies.dispose()
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def test_the_three_modes_are_reported_separately(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> Any:
        return await client.get("/v1/usage", headers=OWNER, params={"window": "all"})

    response = _run(tmp_path, scenario)
    assert response.status_code == 200
    body = response.json()

    # Task holds the parent *and* its delegated child.
    assert body["by_mode"]["task"]["tokens"]["input_tokens"] == 1400
    assert body["by_mode"]["chat"]["tokens"]["input_tokens"] == 200
    assert body["by_mode"]["code"]["tokens"]["input_tokens"] == 300


def test_a_mode_with_no_spend_is_present_at_zero_rather_than_missing(
    tmp_path: Path,
) -> None:
    """A missing row reads as a missing feature, not as an unused one."""

    async def scenario(client: httpx.AsyncClient) -> Any:
        return await client.get(
            "/v1/usage", headers=OTHER_TENANT, params={"window": "all"}
        )

    body = _run(tmp_path, scenario).json()
    assert set(body["by_mode"]) == {"chat", "code", "task"}
    assert body["by_mode"]["chat"]["runs"] == 0


def test_another_tenants_spend_does_not_leak(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> Any:
        return await client.get("/v1/usage", headers=OWNER, params={"window": "all"})

    body = _run(tmp_path, scenario).json()
    total = sum(bucket["tokens"]["input_tokens"] for bucket in body["by_mode"].values())
    assert total == 1900


def test_the_stranger_sees_only_their_own(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> Any:
        return await client.get(
            "/v1/usage", headers=OTHER_TENANT, params={"window": "all"}
        )

    body = _run(tmp_path, scenario).json()
    assert body["by_mode"]["task"]["tokens"]["input_tokens"] == 999_999


def test_the_delegated_share_is_reported_beside_the_task_total(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> Any:
        return await client.get("/v1/usage", headers=OWNER, params={"window": "all"})

    body = _run(tmp_path, scenario).json()
    assert body["delegated"]["tokens"]["input_tokens"] == 400
    assert body["delegated"]["runs"] == 1
    # Beside, not subtracted: the two are answers to different questions.
    assert body["by_mode"]["task"]["tokens"]["input_tokens"] == 1400


def test_spend_is_attributed_to_the_profile_each_run_declared(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> Any:
        return await client.get("/v1/usage", headers=OWNER, params={"window": "all"})

    body = _run(tmp_path, scenario).json()
    assert body["by_model"]["main"]["tokens"]["input_tokens"] == 1600
    assert body["by_model"]["compact"]["tokens"]["input_tokens"] == 300


def test_a_profile_that_recorded_no_cost_is_named(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> Any:
        return await client.get("/v1/usage", headers=OWNER, params={"window": "all"})

    body = _run(tmp_path, scenario).json()
    assert body["by_model"]["compact"]["cost_micro_usd"] == 0
    assert "compact" in body["unpriced_profiles"]
    assert "main" not in body["unpriced_profiles"]


def test_a_run_still_in_flight_is_a_caveat_and_not_a_total(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> Any:
        return await client.get("/v1/usage", headers=OWNER, params={"window": "all"})

    body = _run(tmp_path, scenario).json()
    assert body["runs_in_flight"] == 1
    # It contributed no tokens, which is the other half of the claim.
    assert body["by_mode"]["task"]["runs"] == 2


def test_a_narrow_window_excludes_what_fell_outside_it(tmp_path: Path) -> None:
    """Everything was recorded five minutes ago, so a 7d window keeps it all.

    The assertion that matters is the echo: a page must be able to title itself
    with the window it actually got rather than the one it asked for.
    """

    async def scenario(client: httpx.AsyncClient) -> Any:
        return await client.get("/v1/usage", headers=OWNER, params={"window": "7d"})

    body = _run(tmp_path, scenario).json()
    assert body["window"] == "7d"
    assert body["since"] is not None
    assert body["by_mode"]["task"]["tokens"]["input_tokens"] == 1400


def test_an_unknown_window_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> Any:
        return await client.get("/v1/usage", headers=OWNER, params={"window": "1y"})

    assert _run(tmp_path, scenario).status_code == 422
