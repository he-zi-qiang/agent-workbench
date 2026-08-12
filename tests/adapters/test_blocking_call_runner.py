"""The bound on blocking calls, and the backpressure it makes visible.

ADR-042. Two matched pairs. Each refusal-shaped assertion is accompanied by
one that proves the harness could have observed the other outcome -- otherwise
"never exceeded two" would also pass on a runner that never ran anything, and
"timed out" would also pass on a runner that timed out unconditionally.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from agent_workbench.adapters.concurrency import (
    BlockingCallQueueTimeoutError,
    BlockingCallRunner,
)


class _PeakCounter:
    """Records the high-water mark of genuinely concurrent callables."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.live = 0
        self.peak = 0

    def hold(self, seconds: float) -> int:
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            time.sleep(seconds)
        finally:
            with self._lock:
                self.live -= 1
        return self.peak


def test_the_runner_never_runs_more_calls_at_once_than_it_has_slots() -> None:
    async def scenario() -> None:
        counter = _PeakCounter()
        runner = BlockingCallRunner(slots=2, queue_timeout_seconds=30)
        try:
            await asyncio.gather(
                *(
                    runner.run(lambda: counter.hold(0.15), name=f"work-{index}")
                    for index in range(6)
                )
            )
        finally:
            runner.close()

        assert counter.peak <= 2

    asyncio.run(scenario())


def test_a_wider_runner_actually_reaches_a_higher_peak() -> None:
    """The control for the bound.

    Same callables, same harness, only the pool is wider. Without this, a peak
    of at most two would also be satisfied by a runner that had serialised
    everything -- or by a ``_PeakCounter`` that never saw two threads at once,
    which would make the assertion above a measurement of this test file.
    """

    async def scenario() -> None:
        counter = _PeakCounter()
        runner = BlockingCallRunner(slots=4, queue_timeout_seconds=30)
        try:
            await asyncio.gather(
                *(
                    runner.run(lambda: counter.hold(0.15), name=f"work-{index}")
                    for index in range(6)
                )
            )
        finally:
            runner.close()

        assert counter.peak > 2

    asyncio.run(scenario())


def test_waiting_for_a_slot_gives_up_rather_than_queueing_without_end() -> None:
    """Saturation has to become an error somebody can see.

    A ``ThreadPoolExecutor`` alone cannot produce this: its work queue is an
    unbounded ``SimpleQueue`` with no timeout, so the only symptom of overload
    would be latency that grows until something upstream gives up first.
    """

    async def scenario() -> None:
        runner = BlockingCallRunner(slots=1, queue_timeout_seconds=0.2)
        try:
            occupied = asyncio.create_task(
                runner.run(lambda: time.sleep(1.5), name="holder")
            )
            # Let the holder actually take the slot before crowding in.
            await asyncio.sleep(0.2)
            with pytest.raises(BlockingCallQueueTimeoutError):
                await runner.run(lambda: None, name="crowded-out")
            await occupied
        finally:
            runner.close()

    asyncio.run(scenario())


def test_a_caller_willing_to_wait_long_enough_still_gets_its_slot() -> None:
    """The control for the timeout.

    Identical contention; only the patience differs. This is what separates
    "the pool refuses when saturated" -- which ADR-042 rejected -- from "the
    pool queues, and the queue has a ceiling". If this went red beside a green
    timeout test, the runner would be failing calls rather than delaying them.
    """

    async def scenario() -> None:
        runner = BlockingCallRunner(slots=1, queue_timeout_seconds=30)
        try:
            occupied = asyncio.create_task(
                runner.run(lambda: time.sleep(0.4), name="holder")
            )
            await asyncio.sleep(0.1)
            assert await runner.run(lambda: "served", name="patient") == "served"
            await occupied
        finally:
            runner.close()

    asyncio.run(scenario())
