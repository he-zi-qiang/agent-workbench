"""Framework-neutral execution guard boundary."""

from __future__ import annotations

import asyncio

from agent_workbench.ports.execution_guard import ExecutionGuard, GuardFactory


class _Guard:
    task_id = "task_1"
    worker_id = "worker_1"
    epoch = 1
    backend_pid = 42
    lock_key = 7

    def __init__(self) -> None:
        self._lost = asyncio.Event()

    @property
    def lost(self) -> asyncio.Event:
        return self._lost

    async def healthcheck(self) -> bool:
        return not self._lost.is_set()

    async def release(self) -> None:
        return None


class _Factory:
    async def acquire(self, *, task_id: str, worker_id: str, epoch: int) -> _Guard:
        return _Guard()


def test_a_structural_guard_and_factory_satisfy_the_port() -> None:
    assert isinstance(_Guard(), ExecutionGuard)
    assert isinstance(_Factory(), GuardFactory)
