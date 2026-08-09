"""The independent Task Worker composition over its real durable stores."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from agent_workbench.application.task_inputs import TaskInputService, TaskInputStore
from agent_workbench.application.tasks import SubmittedSemantics, TaskService
from agent_workbench.apps.task_worker.composition import (
    TaskWorkerDependencies,
    build_task_worker_dependencies,
)
from agent_workbench.bootstrap.projections import (
    ArtifactStoreConfig,
    DatabaseConfig,
    TaskConfig,
    TaskWorkerRuntimeConfig,
)
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.task_inputs import TaskInput

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
TABLES = (
    "task_runs, events, event_streams, workflow_checkpoints, "
    "workflow_checkpoint_blobs, workflow_checkpoint_writes"
)
OWNER = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _config(root: Path) -> TaskWorkerRuntimeConfig:
    return TaskWorkerRuntimeConfig(
        database=DatabaseConfig(
            dsn=SecretStr(_dsn()),
            application_name="agent-workbench-task-worker-tests",
            statement_timeout_ms=30_000,
            pool_size=2,
            max_overflow=0,
        ),
        artifacts=ArtifactStoreConfig(
            backend="local",
            local_root=str(root),
            max_artifact_bytes=1_048_576,
        ),
        task=TaskConfig(
            graph_version="v1",
            claim_poll_seconds=0.01,
            lease_seconds=90,
            heartbeat_seconds=20,
            max_attempts=5,
            retry_base_seconds=2,
            retry_max_seconds=60,
            run_semantics_snapshot={"model": {"provider": "demo"}},
            run_semantics_revision="test-v1",
            policy_revision="policy-v1",
            policy_fingerprint="f" * 16,
            default_authorization_envelope=AuthorizationEnvelope(),
        ),
        worker_id="worker_composition_test",
        worker_concurrency=1,
    )


def _run(
    root: Path,
    scenario: Callable[[TaskWorkerDependencies], Awaitable[Any]],
) -> Any:
    async def execute() -> Any:
        dependencies = await build_task_worker_dependencies(_config(root), demo=True)
        try:
            async with dependencies.engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            return await scenario(dependencies)
        finally:
            await dependencies.dispose()

    return asyncio.run(execute())


def _submission_service(dependencies: TaskWorkerDependencies) -> TaskInputService:
    config = dependencies.config.task
    return TaskInputService(
        inputs=TaskInputStore(dependencies.artifacts),
        tasks=TaskService(
            registry=dependencies.registry,
            events=dependencies.events,
            graph_version=config.graph_version,
            semantics=lambda: SubmittedSemantics(
                run_semantics_snapshot=config.run_semantics_snapshot,
                run_semantics_revision=config.run_semantics_revision,
                policy_revision=config.policy_revision,
                policy_fingerprint=config.policy_fingerprint,
                authorization_envelope=config.default_authorization_envelope,
            ),
        ),
    )


def test_demo_worker_composition_claims_and_completes_a_submitted_task(
    tmp_path: Path,
) -> None:
    async def scenario(dependencies: TaskWorkerDependencies) -> tuple[str, list[str]]:
        # The advisory guard must own a different physical session from the
        # Registry/checkpointer engine even when the configuration falls back
        # to the same DSN. Session locks cannot survive transaction pooling.
        guard = await dependencies.guards.acquire(
            task_id="task_composition_guard",
            worker_id="worker_composition_guard",
            epoch=1,
        )
        try:
            async with dependencies.engine.connect() as connection:
                query_pid = int(
                    (
                        await connection.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                )
            assert guard.backend_pid != query_pid
        finally:
            await guard.release()

        submitted = await _submission_service(dependencies).submit(
            principal=OWNER,
            task_input=TaskInput(objective="Draft a retrieval architecture brief."),
            submission_dedup_key="dedup_1",
        )
        # One claim channel for the process. The Worker publishes into it and
        # the real handler set reads out of it; two objects would be a Worker
        # announcing its lease to nobody, and every node refusing.
        assert dependencies.worker.scope is dependencies.scope
        outcome = await dependencies.worker.run_once()
        assert outcome is not None
        assert outcome.task.task_id == submitted.task_id
        return outcome.final_status, [decision.action for decision in outcome.decisions]

    status, actions = _run(tmp_path, scenario)

    assert status == "succeeded"
    assert actions == ["start", "settle_succeeded"]
