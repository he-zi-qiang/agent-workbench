"""PostgreSQL session advisory execution guards.

These are intentionally real PostgreSQL tests. SQLite and in-memory fakes have
neither backend sessions nor advisory locks, so they cannot demonstrate the
property this adapter exists to enforce.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence.engine import create_query_engine
from agent_workbench.adapters.persistence.execution_guard import (
    PostgresExecutionGuardFactory,
    stable_signed_int64,
)
from agent_workbench.ports.execution_guard import GuardUnavailableError

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(scenario: Callable[[str], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(scenario(_dsn()))


def test_stable_advisory_key_has_known_vectors_and_is_cross_process_stable() -> None:
    expected = {
        "task_1": 6871657555054507517,
        "task_example": -3157789506621432723,
        "task_7f3a": -2315725975626503035,
    }
    assert {task_id: stable_signed_int64(task_id) for task_id in expected} == expected

    code = (
        "import sys; sys.path.insert(0, 'src'); "
        "from agent_workbench.adapters.persistence.execution_guard "
        "import stable_signed_int64; print(stable_signed_int64('task_example'))"
    )
    other_process = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    assert int(other_process.stdout) == expected["task_example"]


def test_one_task_has_one_guard_and_release_permits_a_later_acquisition() -> None:
    async def scenario(dsn: str) -> bool:
        factory = PostgresExecutionGuardFactory(dsn, healthcheck_seconds=0.05)
        try:
            first = await factory.acquire(
                task_id="task_1", worker_id="worker_a", epoch=1
            )
            with pytest.raises(GuardUnavailableError):
                await factory.acquire(task_id="task_1", worker_id="worker_b", epoch=2)
            await first.release()
            second = await factory.acquire(
                task_id="task_1", worker_id="worker_b", epoch=2
            )
            try:
                return await second.healthcheck()
            finally:
                await second.release()
        finally:
            await factory.dispose()

    assert _run(scenario)


def test_different_tasks_acquire_independent_pinned_backend_sessions() -> None:
    async def scenario(dsn: str) -> tuple[int, int]:
        factory = PostgresExecutionGuardFactory(dsn, healthcheck_seconds=0.05)
        try:
            first, second = await asyncio.gather(
                factory.acquire(task_id="task_1", worker_id="worker_a", epoch=1),
                factory.acquire(task_id="task_2", worker_id="worker_b", epoch=1),
            )
            try:
                assert await first.healthcheck()
                assert await second.healthcheck()
                return first.backend_pid, second.backend_pid
            finally:
                await first.release()
                await second.release()
        finally:
            await factory.dispose()

    first_pid, second_pid = _run(scenario)
    assert first_pid != second_pid


def test_healthchecks_prove_the_backend_pid_remains_pinned() -> None:
    async def scenario(dsn: str) -> int:
        factory = PostgresExecutionGuardFactory(dsn, healthcheck_seconds=0.01)
        try:
            guard = await factory.acquire(
                task_id="task_1", worker_id="worker_a", epoch=1
            )
            try:
                original = guard.backend_pid
                for _ in range(3):
                    assert await guard.healthcheck()
                    assert guard.backend_pid == original
                return original
            finally:
                await guard.release()
        finally:
            await factory.dispose()

    assert _run(scenario) > 0


def test_terminating_the_pinned_backend_sets_the_lost_signal() -> None:
    async def scenario(dsn: str) -> bool:
        factory = PostgresExecutionGuardFactory(dsn, healthcheck_seconds=0.01)
        control = create_query_engine(
            dsn, application_name="agent-workbench-guard-test"
        )
        try:
            guard = await factory.acquire(
                task_id="task_1", worker_id="worker_a", epoch=1
            )
            try:
                async with control.begin() as connection:
                    terminated = bool(
                        (
                            await connection.execute(
                                text(
                                    "SELECT pg_terminate_backend(CAST(:pid AS integer))"
                                ),
                                {"pid": guard.backend_pid},
                            )
                        ).scalar_one()
                    )
                assert terminated
                await asyncio.wait_for(guard.lost.wait(), timeout=3)
                return guard.lost.is_set() and not await guard.healthcheck()
            finally:
                await guard.release()
        finally:
            await control.dispose()
            await factory.dispose()

    assert _run(scenario)
