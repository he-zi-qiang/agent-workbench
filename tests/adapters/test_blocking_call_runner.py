"""The bound on blocking calls, and the backpressure it makes visible.

ADR-042. Three matched pairs. Each refusal-shaped assertion is accompanied by
one that proves the harness could have observed the other outcome -- otherwise
"never exceeded two" would also pass on a runner that never ran anything,
"timed out" would also pass on a runner that timed out unconditionally, and
"a cancelled caller still holds its slot" would also pass on a runner that
never gave the slot back at all.
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


class _AbandonedThread:
    """A callable that occupies a worker thread until the test lets it go."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self._may_finish = threading.Event()

    def __call__(self) -> None:
        self.started.set()
        # Bounded so a broken runner fails the suite instead of hanging it.
        self._may_finish.wait(10)

    def let_it_finish(self) -> None:
        self._may_finish.set()


async def _abandon_one_call(runner: BlockingCallRunner) -> _AbandonedThread:
    """Start a call, wait until its thread is really running, then cancel it."""

    work = _AbandonedThread()
    caller = asyncio.create_task(runner.run(work, name="abandoned"))
    await asyncio.to_thread(work.started.wait, 10)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    return work


def test_a_cancelled_caller_keeps_its_slot_until_its_thread_really_stops() -> None:
    """Cancellation must not hand the slot on while the thread still has it.

    A cancelled caller stops awaiting, but there is no way to interrupt the
    synchronous callable, so the thread runs on. If the slot went back at the
    moment the coroutine unwound, the next caller would take it instantly --
    never waiting on the semaphore, never timing out -- and would then queue
    inside the ``ThreadPoolExecutor`` instead, whose ``SimpleQueue`` has no
    timeout and no length anybody can read. That is exactly the unbounded,
    invisible wait the semaphore exists to prevent (ADR-042 §6), so a handful
    of cancellations would quietly delete the backpressure signal rather than
    trip it.
    """

    async def scenario() -> None:
        runner = BlockingCallRunner(slots=1, queue_timeout_seconds=0.05)
        abandoned = await _abandon_one_call(runner)
        try:
            with pytest.raises(BlockingCallQueueTimeoutError):
                # The outer deadline is the harness, not the assertion: on a
                # runner that leaks the slot this await never returns at all,
                # and a hung test says much less than a failed one.
                await asyncio.wait_for(
                    runner.run(lambda: None, name="next-in-line"), timeout=5
                )
        finally:
            abandoned.let_it_finish()
            runner.close()

    asyncio.run(scenario())


def test_the_slot_of_a_cancelled_caller_returns_when_its_thread_ends() -> None:
    """The control for the one above.

    Same abandoned thread, same single slot; only this time it is allowed to
    finish. Without this, "the next caller times out" would also be satisfied
    by a runner that had stopped returning slots altogether -- which would
    turn one cancellation into a permanently narrower pool, a worse failure
    than the one being fixed.
    """

    async def scenario() -> None:
        runner = BlockingCallRunner(slots=1, queue_timeout_seconds=30)
        abandoned = await _abandon_one_call(runner)
        try:
            abandoned.let_it_finish()
            served = await asyncio.wait_for(
                runner.run(lambda: "served", name="next-in-line"), timeout=5
            )
            assert served == "served"
        finally:
            abandoned.let_it_finish()
            runner.close()

    asyncio.run(scenario())
