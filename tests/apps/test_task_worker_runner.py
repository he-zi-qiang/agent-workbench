"""Lane behaviour of the Task Worker's polling loop (ADR-024).

The property under test is overlap, so the tests establish it with an
``asyncio.Barrier`` rather than by timing anything. A barrier of three releases
only when three lanes are inside ``run_once`` *simultaneously*; a sequential
runner never gets a second lane there, so the barrier never opens and the test
fails on its ``wait_for`` instead of passing quietly. That is the whole reason
it is a barrier and not a counter: a counter would be satisfied by three calls
one after another, which is exactly the behaviour this change was made to
replace.

`testing.sleep_based_race_tests_forbidden` is why none of these wait for a
duration to prove anything. The only ``sleep`` here is ``sleep(0)``, a
scheduling yield that gives a hypothetical second lane the chance to interleave
-- it is what makes the single-lane control meaningful rather than an artifact
of never having yielded.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.apps.task_worker.runner import TaskWorkerRunner


def test_three_lanes_execute_three_tasks_at_the_same_time() -> None:
    """The point of the change: lane 2 does not wait for lane 1 to settle."""

    async def scenario() -> int:
        gathered = asyncio.Barrier(3)
        stop = asyncio.Event()
        live = 0
        peak = 0

        async def run_once() -> None:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            # Opens only when all three lanes are inside this call at once.
            await gathered.wait()
            live -= 1
            stop.set()
            return None

        runner = TaskWorkerRunner(run_once=run_once, poll_seconds=30, concurrency=3)
        await asyncio.wait_for(runner.run_forever(stop), timeout=5)
        return peak

    assert asyncio.run(scenario()) == 3


def test_one_lane_still_runs_one_task_at_a_time() -> None:
    """The control. Same harness, same yield point, concurrency left at 1.

    Without this, the test above would pass against a runner that overlapped
    when it should not -- it asserts a floor on concurrency, not a ceiling, and
    the ceiling is what the default deployment relies on.
    """

    async def scenario() -> tuple[int, int]:
        stop = asyncio.Event()
        live = 0
        peak = 0
        calls = 0

        async def run_once() -> None:
            nonlocal live, peak, calls
            live += 1
            peak = max(peak, live)
            calls += 1
            # A real claim awaits the database here. If anything else were
            # running a lane, this is where it would show up in `peak`.
            await asyncio.sleep(0)
            live -= 1
            if calls == 3:
                stop.set()
            return None

        runner = TaskWorkerRunner(run_once=run_once, poll_seconds=0.001)
        await asyncio.wait_for(runner.run_forever(stop), timeout=5)
        return peak, calls

    assert asyncio.run(scenario()) == (1, 3)


def test_a_single_lane_raises_what_its_executor_raised() -> None:
    """Why ``concurrency == 1`` skips the TaskGroup instead of using one of one.

    A ``TaskGroup`` re-raises as ``ExceptionGroup``. The default deployment is
    single-lane, and every caller and test written against the old loop expects
    the executor's own exception, so the common path must not change shape.
    """

    async def scenario() -> None:
        async def explode() -> None:
            raise RuntimeError("claim failed")

        runner = TaskWorkerRunner(run_once=explode, poll_seconds=30)
        await runner.run_forever(asyncio.Event())

    with pytest.raises(RuntimeError, match="claim failed"):
        asyncio.run(scenario())


def test_a_failing_lane_stops_the_others() -> None:
    """Multi-lane failure is a group, and it must not leave lanes running.

    The old loop died on the first exception because there was only one lane.
    ``TaskGroup`` keeps that: a lane that raises cancels its siblings, so a
    Worker whose claims start failing stops claiming rather than shedding one
    lane per failure until it is silently single-laned again.
    """

    async def scenario() -> None:
        started = asyncio.Barrier(2)

        async def explode_together() -> None:
            await started.wait()
            raise RuntimeError("claim failed")

        runner = TaskWorkerRunner(
            run_once=explode_together, poll_seconds=30, concurrency=2
        )
        # Bounded, because the barrier is what makes this test meaningful and
        # a barrier is also what makes it hang when the runner regresses to a
        # single lane. Measured: without this, sabotaging `run_forever` back to
        # sequential turned the failure into a 600s hang instead of a red test.
        await asyncio.wait_for(runner.run_forever(asyncio.Event()), timeout=5)

    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(scenario())
    assert all(isinstance(e, RuntimeError) for e in caught.value.exceptions)


def test_a_wakeup_that_arrived_during_a_claim_is_not_cleared_by_the_wait() -> None:
    """The ordering the whole wake-up path rests on: clear, then claim.

    A notification landing while ``run_once`` is querying is the routine case,
    not the exotic one -- it announces a commit that the running query's
    snapshot may already be too old to see. So the flag it leaves up has to
    survive into the wait. A lane that cleared on its way *into* the wait
    instead would drop exactly those wake-ups and sit out the full interval on
    a Task that is committed and claimable.

    The interval here is 30 seconds and a passing run takes none of it. There
    is no PostgreSQL in this test because none is needed: the event is the
    contract, and where it comes from is the listener's business.
    """

    async def scenario() -> int:
        wakeup = asyncio.Event()
        stop = asyncio.Event()
        calls = 0

        async def run_once() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                # What a notification for a Task this claim missed looks like
                # from in here.
                wakeup.set()
            else:
                stop.set()
            return None

        runner = TaskWorkerRunner(run_once=run_once, poll_seconds=30, wakeup=wakeup)
        await asyncio.wait_for(runner.run_forever(stop), timeout=5)
        return calls

    assert asyncio.run(scenario()) == 2


def test_a_wakeup_wired_lane_still_stops_when_shutdown_is_requested() -> None:
    """The control for the test above: the wake-up is not the only way out.

    Same 30 second interval, and nothing ever sets the wake-up. Racing two
    events instead of waiting on one is where a shutdown gets forgotten, and a
    Worker that only stopped when a Task happened to arrive would be a SIGTERM
    that hangs for the poll interval.
    """

    async def scenario() -> int:
        wakeup = asyncio.Event()
        stop = asyncio.Event()
        parked = asyncio.Event()
        calls = 0

        async def run_once() -> None:
            nonlocal calls
            calls += 1
            parked.set()
            return None

        runner = TaskWorkerRunner(run_once=run_once, poll_seconds=30, wakeup=wakeup)
        running = asyncio.create_task(runner.run_forever(stop))
        await parked.wait()
        stop.set()
        await asyncio.wait_for(running, timeout=1)
        return calls

    assert asyncio.run(scenario()) == 1


def test_a_lane_count_below_one_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="concurrency must be at least 1"):
        TaskWorkerRunner(run_once=_never, poll_seconds=1, concurrency=0)


def test_a_non_positive_poll_interval_is_still_refused() -> None:
    with pytest.raises(ValueError, match="poll_seconds must be positive"):
        TaskWorkerRunner(run_once=_never, poll_seconds=0)


async def _never() -> None:  # pragma: no cover - never awaited
    return None
