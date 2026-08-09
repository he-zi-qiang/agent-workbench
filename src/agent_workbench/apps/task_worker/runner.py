"""The cancellable polling loop(s) around the Task executor."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from agent_workbench.workers.task import TaskOutcome

RunOnce = Callable[[], Awaitable[TaskOutcome | None]]


@dataclass(frozen=True, slots=True)
class TaskWorkerRunner:
    """Poll ``concurrency`` claims at once until cancellation or shutdown.

    A completed Task causes an immediate next claim so a backlog drains without
    artificial spacing. Only an empty queue waits, and that wait listens for a
    shutdown event rather than making SIGTERM wait for the whole poll period.

    Concurrency is *lanes*, not batching (ADR-024). Each lane runs the same
    claim-execute-settle cycle the single lane always ran, and the lanes share
    nothing: ``FOR UPDATE SKIP LOCKED`` hands each one a different Task, the
    guard is acquired per Task, and the execution scope is a ``ContextVar``, so
    an ``asyncio`` task started here sees only the lease its own lane published.
    That is why this is a loop count rather than a rewrite of the executor --
    running two Tasks at once needs nothing from ``TaskWorker`` that running
    them in sequence did not already need.

    ``concurrency=1`` takes the single-lane path rather than a ``TaskGroup`` of
    one. Not an optimisation: the overwhelmingly common deployment should raise
    exactly the exception its executor raised, not that exception wrapped in an
    ``ExceptionGroup``, and every caller and test written against the old loop
    keeps that shape for free.
    """

    run_once: RunOnce
    poll_seconds: float
    #: How many Tasks this process may execute at once. Bounded above by
    #: ``database.guard_connection_budget``, which Settings cross-validates:
    #: every concurrent Task pins its own guard connection, so a lane count
    #: past that budget is a Worker that claims work it cannot guard.
    concurrency: int = 1

    def __post_init__(self) -> None:
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Run until ``stop`` is set or this coroutine is cancelled."""

        shutdown = stop or asyncio.Event()
        if self.concurrency == 1:
            await self._lane(shutdown)
            return
        async with asyncio.TaskGroup() as lanes:
            for _ in range(self.concurrency):
                lanes.create_task(self._lane(shutdown))

    async def _lane(self, shutdown: asyncio.Event) -> None:
        """One claim-execute-settle cycle, repeated."""

        while not shutdown.is_set():
            outcome = await self.run_once()
            if outcome is None:
                await _wait_for_stop(shutdown, self.poll_seconds)


async def _wait_for_stop(stop: asyncio.Event, timeout_seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout_seconds)


__all__ = ["RunOnce", "TaskWorkerRunner"]
