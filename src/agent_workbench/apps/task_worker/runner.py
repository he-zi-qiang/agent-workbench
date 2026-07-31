"""The cancellable polling loop around the single-Worker Task executor."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from agent_workbench.workers.task import TaskOutcome

RunOnce = Callable[[], Awaitable[TaskOutcome | None]]


@dataclass(frozen=True, slots=True)
class TaskWorkerRunner:
    """Poll one Worker until cancellation or a process shutdown signal.

    A completed Task causes an immediate next claim so a backlog drains without
    artificial spacing. Only an empty queue waits, and that wait listens for a
    shutdown event rather than making SIGTERM wait for the whole poll period.
    """

    run_once: RunOnce
    poll_seconds: float

    def __post_init__(self) -> None:
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Run until ``stop`` is set or this coroutine is cancelled."""

        shutdown = stop or asyncio.Event()
        while not shutdown.is_set():
            outcome = await self.run_once()
            if outcome is None:
                await _wait_for_stop(shutdown, self.poll_seconds)


async def _wait_for_stop(stop: asyncio.Event, timeout_seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout_seconds)


__all__ = ["RunOnce", "TaskWorkerRunner"]
