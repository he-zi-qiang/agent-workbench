"""The consumer half of ``task_ready``: what it is for, and what it must not be.

What it is for is one number -- the gap between "a Task became claimable" and
"a Worker looked". ``test_a_notification_ends_an_idle_wait_far_short_of_the_
poll_interval`` is the test this whole package exists for, and it is why the
runner here is configured with a 30 second poll interval that no passing run
ever waits out: a test that only checked "the event got set" would stay green
against a listener wired to nothing, because the poll would deliver the same
outcome a second later and nothing would look different.

What it must not be is load-bearing. So the same runner, the same Task and the
same claim appear again in
:func:`test_a_lost_notification_still_gets_the_task_claimed`, with the listener
shut down before the Task is submitted -- every notification from that point on
is thrown away, and the Task is still claimed. Between them the two tests pin
both halves of the contract in the module docstring of ``notifications``: the
wake-up is worth having, and losing it costs latency and nothing else.

The third group is about staying up. Two independent detectors decide that a
session is gone -- the driver's termination callback, and a periodic round trip
for the failure that reports nothing -- and each is tested with the *other one
switched off*, because together they cover for each other: whichever fires
first produces the same reconnection, so a run in which both work is a run in
which either could have been dead for months. The fourth,
:func:`test_one_misjudged_healthcheck_does_not_become_a_reconnect_storm`, is
about the opposite failure: one detector saying "gone" about a session that is
fine must cost exactly one reconnection and not a reconnection every five
seconds until the process is restarted.

Last, the function a deployed Worker actually reaches all of this through.
``_start_task_ready_listener`` is monkeypatched out of every test in
``tests/apps``, which left the body itself executed by nothing: with ``return
None`` as its first statement the whole suite stayed green and every deployed
Worker quietly ran on its poll interval.
:func:`test_the_worker_entry_point_opens_a_channel_that_really_delivers` calls
it against this database instead.

Real PostgreSQL only. What is under test is delivery at commit, connection loss
and reconnection; a fake would be asserting this file against itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence import (
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.persistence.notifications import (
    TASK_READY_CHANNEL,
    TaskReadyListener,
    notify_task_ready,
)
from agent_workbench.apps.task_worker.main import _start_task_ready_listener
from agent_workbench.apps.task_worker.runner import TaskWorkerRunner
from agent_workbench.ports.task_registry import TaskSubmission

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = "approvals, task_runs, events, event_streams"

#: The idle wait every timing test here is measured against. Deliberately far
#: longer than any of them may take: it is the value the poll fallback would
#: produce, so an assertion that comes in under it cannot have been satisfied by
#: polling. Nothing waits it out except a regression.
POLL_SECONDS = 30.0

#: What "immediately" is allowed to cost -- one commit, one notification and one
#: scheduling round trip on a local database. Fifteen times under
#: ``POLL_SECONDS``, so the two outcomes are never a close call.
WAKEUP_BUDGET_SECONDS = 2.0

#: How long a reconnection is given. It is a fresh connection to a database that
#: is already up, spaced by ``reconnect_seconds`` below.
RECOVERY_TIMEOUT_SECONDS = 10.0

#: A healthcheck interval for the tests that are not about the healthcheck.
#: Longer than any run of this file, so a reconnection observed underneath it
#: cannot have been the healthcheck's doing -- and the counter on
#: :class:`_InstrumentedListener` says so out loud rather than by arithmetic.
SLEEPING_HEALTHCHECK_SECONDS = 300.0

#: How long the storm test watches a listener nobody is touching. Forty
#: ``reconnect_seconds`` and six healthcheck intervals: a supervisor looping on
#: a stale loss report gets through tens of sessions in here, and a healthy one
#: gets through none.
STORM_WINDOW_SECONDS = 2.0

#: Whose Task the claiming test is allowed to end on. Distinct from the
#: ``user_1`` every other test in this directory submits under, so a row left in
#: this database by anything else is claimed and ignored rather than mistaken
#: for the one under test.
OWNER_ID = "user_lost_notification"

#: One ``application_name`` per test that terminates backends, because that is
#: what the terminating statement matches on. A shared name would make a second
#: process running this same file -- another agent, a rerun, CI beside a laptop
#: -- kill the session this test is watching, and the failure would read as a
#: listener that never came back.
DEFAULT_APPLICATION_NAME = "agent-workbench-tests-listener"
TERMINATION_APPLICATION_NAME = "agent-workbench-tests-termination-only"
HEALTHCHECK_APPLICATION_NAME = "agent-workbench-tests-healthcheck-only"
STORM_APPLICATION_NAME = "agent-workbench-tests-storm"
ENTRY_POINT_APPLICATION_NAME = "agent-workbench-tests-entry-point"

#: A configured channel name that disagrees with the constant the sender writes.
#: The Worker's entry point is required to subscribe to the constant anyway, so
#: this is what proves configuration cannot quietly redirect the subscription
#: into a channel nobody sends on.
DISAGREEING_CHANNEL = "agent_task_ready"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


class _InstrumentedListener(TaskReadyListener):
    """The real listener with its two loss detectors made observable.

    A session that came back says nothing about *which* detector noticed it was
    gone, and the two normally race: the driver's termination callback fires
    within milliseconds of a backend dying, so the round trip is never what
    reports it and could be broken for months without a test noticing. The
    switches here take one detector out at a time; the counters make "the other
    one did not run either" an assertion rather than an inference.

    Nothing else is changed. Both overrides call up, and the one that lies does
    it exactly once.
    """

    def __init__(
        self,
        listen_dsn: str,
        *,
        application_name: str,
        healthcheck_seconds: float,
        reconnect_seconds: float,
        misjudge_one_healthcheck: bool = False,
        deaf_to_terminations: bool = False,
    ) -> None:
        super().__init__(
            listen_dsn,
            application_name=application_name,
            healthcheck_seconds=healthcheck_seconds,
            reconnect_seconds=reconnect_seconds,
        )
        self.healthchecks = 0
        self.notifications = 0
        self._misjudge_one_healthcheck = misjudge_one_healthcheck
        self._deaf_to_terminations = deaf_to_terminations

    async def _still_listening(self) -> bool:
        self.healthchecks += 1
        if self._misjudge_one_healthcheck:
            # Once, and never again -- a transient verdict, which is the whole
            # point. Everything after this returns whatever the real round trip
            # returns, so a reconnection observed later is the listener's doing
            # and not this instrument's.
            self._misjudge_one_healthcheck = False
            return False
        return await super()._still_listening()

    def _on_terminated(self, connection: object) -> None:
        if self._deaf_to_terminations:
            return
        super()._on_terminated(connection)

    def _on_notify(
        self,
        connection: object,
        pid: object,
        channel: object,
        payload: object,
    ) -> None:
        self.notifications += 1
        super()._on_notify(connection, pid, channel, payload)


def _listener(
    *,
    application_name: str = DEFAULT_APPLICATION_NAME,
    # Short, because the tests that lose a connection have to observe the
    # recovery inside a test run rather than inside a deployment.
    healthcheck_seconds: float = 0.3,
    reconnect_seconds: float = 0.05,
    misjudge_one_healthcheck: bool = False,
    deaf_to_terminations: bool = False,
) -> Callable[[], _InstrumentedListener]:
    """A listener recipe, deferred so ``_run`` builds it inside the loop."""

    def build() -> _InstrumentedListener:
        return _InstrumentedListener(
            _dsn(),
            application_name=application_name,
            healthcheck_seconds=healthcheck_seconds,
            reconnect_seconds=reconnect_seconds,
            misjudge_one_healthcheck=misjudge_one_healthcheck,
            deaf_to_terminations=deaf_to_terminations,
        )

    return build


def _run(
    scenario: Callable[[Any, Any], Awaitable[Any]],
    *,
    build_listener: Callable[[], _InstrumentedListener] | None = None,
) -> Any:
    """One truncated database, one started listener, and the scenario.

    The listener is started before anything is written, for the same reason the
    sender's tests subscribe first: a listener that attached afterwards would
    miss the message it is here to observe, and every test would read as "not
    delivered".
    """

    _dsn()

    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        listener = (build_listener or _listener())()
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            await listener.start()
            return await scenario(engine, listener)
        finally:
            await listener.aclose()
            await engine.dispose()

    return asyncio.run(execute())


async def _terminate(engine: Any, application_name: str) -> int:
    """Kill every backend this listener owns, the way a failover would."""

    async with engine.begin() as connection:
        terminated = (
            (
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE application_name = :name"
                    ),
                    {"name": application_name},
                )
            )
            .scalars()
            .all()
        )
    return len(terminated)


def _submission(**overrides: Any) -> TaskSubmission:
    base: dict[str, Any] = {
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
        "thread_id": "thr_1",
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
    base.update(overrides)
    return TaskSubmission.model_validate(base)


async def _until(predicate: Callable[[], bool], *, timeout_seconds: float) -> None:
    """Wait for a condition to hold, or fail the wait.

    Not a sleep-based race test: nothing here is *proved* by the passage of
    time. This is the wait for a background reconnection to finish, and the
    assertions the tests make come after it.
    """

    async with asyncio.timeout(timeout_seconds):
        while not predicate():
            await asyncio.sleep(0.01)


class _Lane:
    """A ``TaskWorkerRunner`` claim function that records when it was called."""

    def __init__(self, *, stop_after: int) -> None:
        self.at: list[float] = []
        self.stop = asyncio.Event()
        self.claimed = asyncio.Event()
        self._stop_after = stop_after

    async def run_once(self) -> None:
        """An empty queue, every time. The Task's own claim is tested below."""

        self.at.append(time.monotonic())
        if len(self.at) >= self._stop_after:
            self.claimed.set()
            self.stop.set()
        return None


# --------------------------------------------------------------------------
# The wake-up arrives
# --------------------------------------------------------------------------


def test_a_committed_notification_raises_the_listener_s_flag() -> None:
    """The floor: a real ``NOTIFY`` reaches a real ``LISTEN`` in this process.

    Its own control group, in the first assertion: the flag is down before the
    transaction commits, so "it was set" cannot be a listener that starts up
    already raised.
    """

    async def scenario(engine: Any, listener: TaskReadyListener) -> tuple[bool, bool]:
        before = listener.woken.is_set()
        async with engine.begin() as connection:
            await notify_task_ready(connection, task_id="task_flag")
        await asyncio.wait_for(listener.woken.wait(), timeout=WAKEUP_BUDGET_SECONDS)
        return before, listener.woken.is_set()

    assert _run(scenario) == (False, True)


def test_a_notification_ends_an_idle_wait_far_short_of_the_poll_interval() -> None:
    """The one this package exists for: the latency actually drops.

    The runner is given a 30 second poll interval and then asked to notice
    something within two. Only the wake-up can do that -- see the control below,
    which is the same scenario with the wake-up left unwired and which waits the
    full interval out.
    """

    async def scenario(engine: Any, listener: TaskReadyListener) -> float:
        lane = _Lane(stop_after=2)
        runner = TaskWorkerRunner(
            run_once=lane.run_once,
            poll_seconds=POLL_SECONDS,
            wakeup=listener.woken,
        )
        running = asyncio.create_task(runner.run_forever(lane.stop))
        try:
            # The first claim found nothing, so the lane is now in -- or about
            # to enter -- the idle wait. Either is fine: the runner clears the
            # flag before it claims, so a notification that lands in the gap
            # leaves it raised and the wait ends immediately.
            await _until(lambda: len(lane.at) == 1, timeout_seconds=5)
            sent = time.monotonic()
            async with engine.begin() as connection:
                await notify_task_ready(connection, task_id="task_wakeup")
            # Generous on purpose. A runner that ignored the wake-up would still
            # get here, one poll interval later, and the assertion below then
            # says the interesting thing rather than "timed out".
            await asyncio.wait_for(
                lane.claimed.wait(),
                timeout=POLL_SECONDS + RECOVERY_TIMEOUT_SECONDS,
            )
        finally:
            lane.stop.set()
            await asyncio.wait_for(running, timeout=5)
        return lane.at[1] - sent

    woken_within = _run(scenario)

    assert woken_within < WAKEUP_BUDGET_SECONDS


def test_without_the_wakeup_the_same_notification_changes_nothing() -> None:
    """The control for the test above, and the reason it means anything.

    Same runner, same interval, same committed notification -- only the wiring
    is gone. The second claim does not happen, which is what makes the two
    second measurement above attributable to the listener rather than to a
    scheduler that happened to be quick.
    """

    async def scenario(engine: Any, listener: TaskReadyListener) -> tuple[int, bool]:
        lane = _Lane(stop_after=2)
        runner = TaskWorkerRunner(
            run_once=lane.run_once,
            poll_seconds=POLL_SECONDS,
            wakeup=None,
        )
        running = asyncio.create_task(runner.run_forever(lane.stop))
        try:
            await _until(lambda: len(lane.at) == 1, timeout_seconds=5)
            async with engine.begin() as connection:
                await notify_task_ready(connection, task_id="task_unwired")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    lane.claimed.wait(),
                    timeout=WAKEUP_BUDGET_SECONDS,
                )
            # The listener still heard it. Nothing about the delivery differed;
            # only the runner it was not attached to.
            heard = listener.woken.is_set()
        finally:
            lane.stop.set()
            await asyncio.wait_for(running, timeout=5)
        return len(lane.at), heard

    assert _run(scenario) == (1, True)


# --------------------------------------------------------------------------
# The wake-up does not arrive
# --------------------------------------------------------------------------


def test_a_lost_notification_still_gets_the_task_claimed() -> None:
    """The contract the accelerator must not break, against the real registry.

    The listener is closed before the Task is submitted, so its notification is
    delivered to nobody -- the same outcome as a dropped connection, a coalesced
    message or a Worker that never subscribed. The Task is claimed anyway,
    because the row was always the fact and the poll was always underneath.
    """

    async def scenario(
        engine: Any, listener: _InstrumentedListener
    ) -> tuple[list[str], str, bool, int]:
        await listener.aclose()
        # Read here rather than at the end. The runner clears this flag before
        # every claim, so the value it holds after the loop has stopped is a
        # statement about the last clear and about nothing else -- a listener
        # that came up with the flag already raised would pass that reading.
        # This one is taken while nothing has run that could clear it.
        raised_at_shutdown = listener.woken.is_set()
        registry = PostgresTaskRegistry(engine)
        claimed: list[str] = []
        stop = asyncio.Event()
        started = asyncio.Event()
        settled = asyncio.Event()

        async def run_once() -> None:
            claim = await registry.claim_next("worker_polling", lease_seconds=60)
            started.set()
            if claim is None:
                return None
            # Only this test's own Task ends the run. `claim_next` takes
            # whatever is on the queue, and this suite's database is not
            # guaranteed to be this suite's alone -- a Task another run left
            # behind would otherwise stop the loop before the Task under test
            # was ever submitted, and the assertion would read as a lost row.
            if claim.task.owner_id != OWNER_ID:
                return None
            claimed.append(claim.task.task_id)
            settled.set()
            stop.set()
            return None

        runner = TaskWorkerRunner(
            run_once=run_once,
            # Short, because polling is the mechanism under test here rather
            # than the fallback being measured against.
            poll_seconds=0.05,
            wakeup=listener.woken,
        )
        running = asyncio.create_task(runner.run_forever(stop))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            task = await registry.submit(_submission(owner_id=OWNER_ID))
            await asyncio.wait_for(settled.wait(), timeout=RECOVERY_TIMEOUT_SECONDS)
        finally:
            stop.set()
            await asyncio.wait_for(running, timeout=5)
        return claimed, task.task_id, raised_at_shutdown, listener.notifications

    claimed, task_id, raised_at_shutdown, notifications = _run(
        scenario, build_listener=_listener()
    )

    assert claimed == [task_id]
    # And it really was the poll. Two readings, because neither says it alone:
    # the flag was down at the moment the listener stopped, and the delivery
    # counter -- which nothing in the runner touches -- never moved, so the
    # Task's own notification reached this process not at all.
    assert (raised_at_shutdown, notifications) == (False, 0)


# --------------------------------------------------------------------------
# Staying up
# --------------------------------------------------------------------------


def test_a_terminated_session_reconnects_and_wakes_again() -> None:
    """A dropped ``LISTEN`` session is a slower Worker, not a broken one.

    The backend is terminated from another session -- what a failover, an idle
    timeout or a restart looks like from in here. Nothing is caught up on
    afterwards, because nothing was missed: the Task rows never depended on this
    connection. What has to come back is only the acceleration, so that is what
    is asserted, with a notification the reconnected session hears.

    Its control group is the first notification, before the kill: without it,
    "the flag rose" would not distinguish a session that reconnected from one
    that was never disturbed.

    The disconnect is *also* logged, and that is deliberately not asserted here.
    ``test_migrations`` in this same directory builds an Alembic ``Config``,
    which runs ``fileConfig`` with ``disable_existing_loggers=True`` and takes
    pytest's capturing handler off the root logger for the rest of the process
    -- so a ``caplog`` assertion in this file passes alone and is vacuous in a
    full run. The session counter is the observable that cannot be switched off
    by another test.
    """

    async def scenario(engine: Any, listener: TaskReadyListener) -> tuple[bool, ...]:
        async with engine.begin() as connection:
            await notify_task_ready(connection, task_id="task_before_kill")
        await asyncio.wait_for(listener.woken.wait(), timeout=WAKEUP_BUDGET_SECONDS)
        before = listener.connected
        killed_session = listener.sessions

        terminated = await _terminate(engine, DEFAULT_APPLICATION_NAME)
        assert terminated, "no listener session was found to terminate"

        # A *new* session, not merely a connected one. `connected` still reports
        # True for the moments between the backend going away and this process
        # noticing, so waiting on it returns immediately and the notification
        # below is then sent into the dead session -- measured: that is exactly
        # how this test failed before the counter existed.
        await _until(
            lambda: listener.sessions > killed_session and listener.connected,
            timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
        )
        listener.woken.clear()
        async with engine.begin() as connection:
            await notify_task_ready(connection, task_id="task_after_kill")
        await asyncio.wait_for(listener.woken.wait(), timeout=WAKEUP_BUDGET_SECONDS)
        return (
            before,
            listener.connected,
            listener.woken.is_set(),
            listener.sessions > killed_session,
        )

    # A wake-up before the kill, a wake-up after it, and a second session in
    # between. Without the last one this would also pass against a listener
    # whose backend was never actually terminated.
    assert _run(scenario) == (True, True, True, True)


def test_the_termination_callback_alone_brings_the_session_back() -> None:
    """The test above, with the round trip taken out of the race.

    The healthcheck interval is three hundred seconds -- longer than this file
    takes to run -- so the only thing left that can notice a dead backend is the
    driver's termination callback. The counter says so rather than the
    arithmetic: ``healthchecks`` is still ``0`` when the replacement session is
    already listening, so nothing here reconnected because a round trip failed.

    Without this, ``_on_terminated`` could be an empty method and every test in
    this file would still pass -- measured: with the callback stubbed out, the
    default 0.3 second healthcheck reconnects fast enough that no assertion
    elsewhere can tell which detector did it.
    """

    async def scenario(
        engine: Any, listener: _InstrumentedListener
    ) -> tuple[bool, int, int, bool]:
        killed_session = listener.sessions

        terminated = await _terminate(engine, TERMINATION_APPLICATION_NAME)
        assert terminated, "no listener session was found to terminate"

        await _until(
            lambda: listener.sessions > killed_session and listener.connected,
            timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
        )
        listener.woken.clear()
        async with engine.begin() as connection:
            await notify_task_ready(connection, task_id="task_after_termination")
        await asyncio.wait_for(listener.woken.wait(), timeout=WAKEUP_BUDGET_SECONDS)
        return (
            listener.connected,
            listener.sessions,
            listener.healthchecks,
            listener.woken.is_set(),
        )

    assert _run(
        scenario,
        build_listener=_listener(
            application_name=TERMINATION_APPLICATION_NAME,
            healthcheck_seconds=SLEEPING_HEALTHCHECK_SECONDS,
        ),
    ) == (True, 2, 0, True)


def test_the_healthcheck_alone_brings_the_session_back() -> None:
    """The other half, with the driver's report thrown away instead.

    ``deaf_to_terminations`` swallows the callback, so a terminated backend
    reaches this listener as nothing at all -- which is exactly the shape of the
    failure the round trip exists for: a session that stopped delivering and
    never said so. What has to happen is that the next healthcheck notices and
    reconnects.

    Its own control is the delivery at the end. A reconnection that produced a
    session which is not actually subscribed would still move the counter, and
    the counter is all the ``_until`` above waits on.
    """

    async def scenario(
        engine: Any, listener: _InstrumentedListener
    ) -> tuple[bool, int, bool, bool]:
        killed_session = listener.sessions

        terminated = await _terminate(engine, HEALTHCHECK_APPLICATION_NAME)
        assert terminated, "no listener session was found to terminate"

        await _until(
            lambda: listener.sessions > killed_session and listener.connected,
            timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
        )
        listener.woken.clear()
        async with engine.begin() as connection:
            await notify_task_ready(connection, task_id="task_after_healthcheck")
        await asyncio.wait_for(listener.woken.wait(), timeout=WAKEUP_BUDGET_SECONDS)
        return (
            listener.connected,
            listener.sessions,
            listener.healthchecks >= 1,
            listener.woken.is_set(),
        )

    assert _run(
        scenario,
        build_listener=_listener(
            application_name=HEALTHCHECK_APPLICATION_NAME,
            deaf_to_terminations=True,
        ),
    ) == (True, 2, True, True)


def test_one_misjudged_healthcheck_does_not_become_a_reconnect_storm() -> None:
    """One wrong verdict costs one reconnection, not the process's whole life.

    A healthcheck is a round trip with a deadline, so it is allowed to be wrong
    about a session that is fine -- a slow moment on a loaded box says the same
    thing a dead backend does. What must not happen is what did: the reconnect
    closes the healthy session, asyncpg reports *that* close through the same
    termination callback a died-on-us backend uses, a tick later and therefore
    after the replacement is already listening, and the supervisor tears the new
    session down too. Measured, from exactly this injection: 67 sessions in six
    seconds, every one of them dropping whatever arrived in between, and the
    healthcheck never running again because the supervisor's wait never reached
    its timeout.

    Both halves are asserted, because either alone can be satisfied by an
    accident. A listener that stopped reconnecting because its supervisor died
    would hold the session count still; one that reconnected once and then went
    deaf would keep the count at two and never check anything again. So: two
    sessions after a two second window -- forty ``reconnect_seconds`` -- *and* a
    healthcheck counter that kept moving inside it.

    The verdict is injected once and never again, so everything observed after
    the first reconnection is the real listener's behaviour. Removing
    ``_on_terminated``'s identity check turns this red on both counts.
    """

    async def scenario(
        engine: Any, listener: _InstrumentedListener
    ) -> tuple[int, int, int, bool]:
        # The injected verdict lands on the first healthcheck and costs one
        # reconnection. Waited for rather than slept through: it is the point a
        # healthy listener and a storming one both pass, so the window below
        # starts at the same place in either.
        await _until(
            lambda: listener.sessions > 1,
            timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
        )
        settled_sessions = listener.sessions
        settled_healthchecks = listener.healthchecks

        await asyncio.sleep(STORM_WINDOW_SECONDS)

        # Nothing was touched for two seconds, so a session that is still
        # listening now is one nobody replaced. Reported rather than awaited:
        # a storming listener drops this delivery too, and a test that died on
        # the timeout would name the symptom furthest from the cause instead of
        # the session count below.
        listener.woken.clear()
        async with engine.begin() as connection:
            await notify_task_ready(connection, task_id="task_after_storm")
        with suppress(TimeoutError):
            await asyncio.wait_for(listener.woken.wait(), timeout=WAKEUP_BUDGET_SECONDS)
        return (
            settled_sessions,
            listener.sessions,
            listener.healthchecks - settled_healthchecks,
            listener.woken.is_set(),
        )

    first, after_the_window, healthchecks_since, heard = _run(
        scenario,
        build_listener=_listener(
            application_name=STORM_APPLICATION_NAME,
            misjudge_one_healthcheck=True,
        ),
    )

    # One bad verdict, one new session -- and still one new session two seconds
    # and forty reconnect intervals later.
    assert (first, after_the_window) == (2, 2)
    # Six healthcheck intervals fit in that window. Three is the floor a
    # scheduler having a bad day still clears, and zero is what the storm gives.
    assert healthchecks_since >= 3
    assert heard is True


# --------------------------------------------------------------------------
# What the deployed Worker actually starts
# --------------------------------------------------------------------------


def test_the_worker_entry_point_opens_a_channel_that_really_delivers() -> None:
    """``_start_task_ready_listener`` called for real, once, on a real database.

    Everywhere else this function is monkeypatched away, which is how it can be
    both the only code path a deployed Worker takes to a wake-up and reachable
    by nothing -- measured: with ``return None`` as its first statement, the
    whole suite stays green and every deployed Worker silently falls back to its
    poll interval.

    The configured channel is deliberately the *disagreeing* one. Settings
    accepts any channel name, the sender writes a constant, and a Worker that
    honoured the configuration here would connect, subscribe, log nothing and
    never be woken again. So what is asserted is not that a listener object came
    back but that a committed ``NOTIFY`` on the constant's channel reaches it.
    """

    async def execute() -> tuple[bool, bool]:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        listener: TaskReadyListener | None = None
        try:
            listener = await _start_task_ready_listener(
                listen_dsn=_dsn(),
                application_name=ENTRY_POINT_APPLICATION_NAME,
                healthcheck_seconds=SLEEPING_HEALTHCHECK_SECONDS,
                configured_channel=DISAGREEING_CHANNEL,
            )
            if listener is None:
                return (False, False)
            async with engine.begin() as connection:
                await notify_task_ready(connection, task_id="task_entry_point")
            await asyncio.wait_for(listener.woken.wait(), timeout=WAKEUP_BUDGET_SECONDS)
            return (True, listener.woken.is_set())
        finally:
            if listener is not None:
                await listener.aclose()
            await engine.dispose()

    _dsn()

    assert asyncio.run(execute()) == (True, True)
    # And the disagreement really was one, so the paragraph above is not
    # describing a branch this test walked past.
    assert DISAGREEING_CHANNEL != TASK_READY_CHANNEL


# --------------------------------------------------------------------------
# What is refused, and when
# --------------------------------------------------------------------------


def test_a_dsn_for_another_driver_is_refused_at_construction() -> None:
    """Loudly, and before a Worker is running.

    A blocking driver inside the event loop would not fail here; it would stall
    every lane in the process at some later moment nobody connects to this line.
    """

    with pytest.raises(ValueError, match="requires a postgresql\\+asyncpg:// DSN"):
        TaskReadyListener("postgresql+psycopg://user:pw@localhost/agent_workbench")


def test_the_driverless_dsn_spelling_is_accepted() -> None:
    """The control: ``database.listen_dsn`` validates both spellings.

    Refusing this one would reject a configuration Settings already accepted --
    a Worker that starts everywhere except where the DSN omits ``+asyncpg``.
    """

    assert (
        TaskReadyListener("postgresql://user:pw@localhost/agent_workbench").connected
        is False
    )
