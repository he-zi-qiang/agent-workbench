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

    ``wakeup`` shortens the idle wait and changes nothing else. Absent, the loop
    is the poll loop it always was; present, an empty-queue wait ends as soon as
    somebody says a Task became claimable -- but it still ends on ``poll_seconds``
    when nobody does, which is the only reason a wake-up is allowed to be lost.
    """

    run_once: RunOnce
    poll_seconds: float
    #: How many Tasks this process may execute at once. Bounded above by
    #: ``database.guard_connection_budget``, which Settings cross-validates:
    #: every concurrent Task pins its own guard connection, so a lane count
    #: past that budget is a Worker that claims work it cannot guard.
    concurrency: int = 1
    #: Set by something that heard a Task became claimable -- in the deployed
    #: Worker, ``TaskReadyListener``. Deliberately a bare event and not a queue
    #: of ids: this loop's next move is a claim either way, so anything more
    #: specific would only be an opportunity to trust it.
    #:
    #: Optional because it must be. A Worker whose listener never connected, or
    #: whose connection dropped an hour ago, is a Worker running exactly the
    #: loop below with this left at ``None``.
    wakeup: asyncio.Event | None = None

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
            if self.wakeup is not None:
                # Cleared before the claim, never after the wait. A wake-up that
                # lands while `run_once` is querying announces a row that query's
                # snapshot may already be too old to see; clearing afterwards
                # would drop it and park this lane for the whole poll period on
                # work that is committed and waiting. Clearing first can only
                # cost one extra claim -- the claim that follows sees everything
                # committed before the clear, and anything after it leaves the
                # flag up for the wait below.
                self.wakeup.clear()
            outcome = await self.run_once()
            if outcome is None:
                await self._wait_while_idle(shutdown)

    async def _wait_while_idle(self, shutdown: asyncio.Event) -> None:
        """Wait out the poll interval, unless shutdown or a wake-up beats it."""

        if self.wakeup is None:
            await _wait_for_stop(shutdown, self.poll_seconds)
            return
        await _wait_for_first(shutdown, self.wakeup, timeout_seconds=self.poll_seconds)


async def _wait_for_stop(stop: asyncio.Event, timeout_seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout_seconds)


async def _wait_for_first(
    stop: asyncio.Event,
    wakeup: asyncio.Event,
    *,
    timeout_seconds: float,
) -> None:
    """Return on whichever happens first: shutdown, a wake-up, or the timeout.

    Two tasks and ``asyncio.wait`` rather than a single ``wait_for`` over a
    combined event, because the two events have different owners: ``stop`` is
    the process's and ``wakeup`` is shared by every lane, so neither can be
    consumed here on the other's behalf.
    """

    stopped = asyncio.ensure_future(stop.wait())
    woken = asyncio.ensure_future(wakeup.wait())
    try:
        await asyncio.wait(
            (stopped, woken),
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Both, always: the loser is still parked on an event that may never be
        # set, and one abandoned waiter per idle poll is a leak that only shows
        # up on a Worker that has been idle for a day.
        stopped.cancel()
        woken.cancel()


__all__ = ["RunOnce", "TaskWorkerRunner"]
