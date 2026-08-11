"""What the watchdog must notice, and what it must stay quiet about.

A watchdog is easy to write badly in two opposite directions, and both look
identical to a test that only checks one of them:

* one that never reports is indistinguishable from a healthy process;
* one that always reports is indistinguishable from a broken one.

So the cases here come in a pair. The loop is blocked on purpose with
``time.sleep`` inside a coroutine -- the exact thing the watchdog exists to
catch -- and the same watchdog is then left on an idle loop for the same
wall-clock span and must say nothing at all.

Cancellation has no pair because it is not a measurement: stopping the watchdog
must leave nothing running. A background task that outlives its own
cancellation is how a process ends up with a loop it cannot close.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from agent_workbench.adapters.telemetry.event_loop_lag import (
    SAMPLE_INTERVAL_SECONDS,
    WARN_THRESHOLD_SECONDS,
    EventLoopLagWatchdog,
)
from agent_workbench.adapters.telemetry.otel import OtelTelemetry
from agent_workbench.apps.api.main import create_app
from agent_workbench.ports.telemetry import EVENT_LOOP_LAG, Telemetry

LOGGER_NAME = "agent_workbench.adapters.telemetry.event_loop_lag"

#: What the API lifespan names the task. Asserted rather than imported: the
#: name is what an operator reads in a traceback or a task dump, so a silent
#: rename is a change worth noticing.
WATCHDOG_TASK_NAME = "event-loop-lag-watchdog"

# The threshold is the load-bearing number here, and the control case is what it
# decides. An idle loop on a busy machine really does come back late, so a
# watchdog that reports it is not wrong -- no test can tell "this process was not
# scheduled" from "something blocked the loop", because from inside the loop they
# are the same event. The threshold is where that line gets drawn, and drawing it
# inside the machine's own noise is what makes the control flap.
#
# It was 20 ms, two sample intervals, which is the size of ordinary jitter rather
# than a margin above it: at load average ~138 the review watched the control
# fail 3 runs out of 9 on a 23 ms sample, and pass 9 of 9 on an idle machine.
#
# The replacement was measured rather than guessed. Replaying this control 260
# times against 96 spinning processes on 8 cores -- load average ~115 -- the
# worst sample per round had a median of 7 ms and a p90 of 9 ms, but a tail that
# twice reached 220-246 ms. 5 of those 260 rounds crossed 20 ms; none crossed
# 250 ms. So 250 ms is not a margin either: it is exactly where a badly loaded
# machine lands.
#
# 500 ms is a margin -- 2x that worst observation -- and the stall length is what
# pays for it. The one sample that spans the stall is late by at least
# ``stall - interval``, i.e. 990 ms, on any machine at any load, because load can
# only push a wake-up later and never earlier. That floor is not a probability,
# so the two directions are 2x above the worst jitter seen and 2x below a lag
# that is guaranteed. The price is half a second more in each of the four
# scenarios below; the interval stays at 10 ms and no longer sets any margin.
_INTERVAL = 0.01
_THRESHOLD = 0.5
_STALL_SECONDS = 1.0
_STALL_MS = _STALL_SECONDS * 1000

# What a report of that stall is allowed to say. The floor is what ties the
# number to the stall: the interval (10 ms), or seconds mislabelled as
# milliseconds (1.0), each land under it, and so does anything that reported a
# different sample. The ceiling only has to catch a unit wrong the other way --
# microseconds would be 1000000 -- or an absolute clock reading, both orders of
# magnitude out, so it can afford a full second of head-room for a loaded machine
# that is slow to reschedule this process once the stall ends. Tight enough to
# fail every wrong answer, loose enough not to fail a right one.
_LAG_FLOOR_MS = _STALL_MS * 0.8
_LAG_CEILING_MS = _STALL_MS + 1000.0


class _Recording:
    """Remembers what it was told, so a test can read it back."""

    def __init__(self) -> None:
        self.counts: list[tuple[str, int]] = []
        self.records: list[tuple[str, float]] = []

    def span(self, name: str, *, attributes: Any = None) -> Any:
        from contextlib import nullcontext

        del name, attributes
        return nullcontext()

    def count(self, name: str, *, value: int = 1, attributes: Any = None) -> None:
        del attributes
        self.counts.append((name, value))

    def record(self, name: str, value: float, *, attributes: Any = None) -> None:
        del attributes
        self.records.append((name, value))


class _Exploding:
    """Recording anything raises. The watchdog has to survive it."""

    def span(self, name: str, *, attributes: Any = None) -> Any:
        del name, attributes
        raise RuntimeError("the collector is unreachable")

    def count(self, name: str, *, value: int = 1, attributes: Any = None) -> None:
        del name, value, attributes
        raise RuntimeError("the collector is unreachable")

    def record(self, name: str, value: float, *, attributes: Any = None) -> None:
        del name, value, attributes
        raise RuntimeError("the collector is unreachable")


def _watchdog(telemetry: Telemetry) -> EventLoopLagWatchdog:
    return EventLoopLagWatchdog(
        telemetry=telemetry,
        interval_seconds=_INTERVAL,
        warn_threshold_seconds=_THRESHOLD,
    )


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _watch_a_blocked_loop(telemetry: Telemetry) -> asyncio.Task[None]:
    """Run the watchdog across one deliberate stall of the whole loop."""

    task = asyncio.create_task(_watchdog(telemetry).run_forever())
    # The watchdog has to be inside a sleep before the stall begins; otherwise
    # there is no promised wake-up for the stall to be late for.
    await asyncio.sleep(_INTERVAL * 3)
    time.sleep(_STALL_SECONDS)
    # One more scheduling turn, so the delayed sample wakes and reports.
    await asyncio.sleep(_INTERVAL * 3)
    return task


async def _watch_an_idle_loop(telemetry: Telemetry) -> asyncio.Task[None]:
    """The control: the same span of wall-clock, never holding the loop."""

    task = asyncio.create_task(_watchdog(telemetry).run_forever())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STALL_SECONDS
    while loop.time() < deadline:
        await asyncio.sleep(_INTERVAL)
    return task


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Only this module's warnings; another logger's noise is not evidence."""

    return [
        record
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno >= logging.WARNING
    ]


# --------------------------------------------------------------------------
# A blocked loop is reported, with a number that matches what was blocked
# --------------------------------------------------------------------------


def test_a_blocked_loop_is_reported_with_the_measured_lag() -> None:
    telemetry = _Recording()

    async def scenario() -> None:
        await _stop(await _watch_a_blocked_loop(telemetry))

    asyncio.run(scenario())

    # Spelled out rather than imported from the implementation. Comparing the
    # constants to themselves asserts only that the module agrees with itself,
    # and passes just as well after somebody renames both -- including a rename
    # that drops the ``_ms`` suffix the OTel adapter reads the unit from.
    assert telemetry.counts == [("runtime.event_loop.stalled", 1)]
    assert [name for name, _ in telemetry.records] == ["runtime.event_loop.lag_ms"]
    lag_ms = telemetry.records[0][1]
    # Milliseconds, and close to what was actually blocked. Reporting the
    # interval, the elapsed time, or seconds-labelled-as-milliseconds would
    # each still be "a stall was reported" and each fail here.
    assert _LAG_FLOOR_MS <= lag_ms <= _LAG_CEILING_MS


def test_the_warning_carries_the_measured_number(
    caplog: pytest.LogCaptureFixture,
) -> None:
    telemetry = _Recording()

    async def scenario() -> None:
        await _stop(await _watch_a_blocked_loop(telemetry))

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        asyncio.run(scenario())

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    # The measurement has to be in the line. A warning that only says "lag
    # exceeded" cannot tell a 30 ms blip from a thirty-second freeze, which is
    # the whole difference between a note and an incident.
    logged_ms = warnings[0].args[0] if warnings[0].args else None
    assert isinstance(logged_ms, float)
    assert _LAG_FLOOR_MS <= logged_ms <= _LAG_CEILING_MS


# --------------------------------------------------------------------------
# The name reaches a collector as a millisecond histogram
# --------------------------------------------------------------------------


class _FakeInstrument:
    def __init__(self) -> None:
        self.values: list[float] = []

    def record(self, value: float, attributes: Any = None) -> None:
        del attributes
        self.values.append(value)


class _FakeMeter:
    """Remembers what unit the adapter asked each instrument to carry."""

    def __init__(self) -> None:
        self.histograms: list[tuple[str, str]] = []
        self.instruments: list[_FakeInstrument] = []

    def create_histogram(self, name: str, unit: str = "") -> _FakeInstrument:
        self.histograms.append((name, unit))
        instrument = _FakeInstrument()
        self.instruments.append(instrument)
        return instrument


def test_the_lag_metric_arrives_as_a_millisecond_histogram() -> None:
    """The suffix is load-bearing, and this is the only test that says so.

    ``OtelTelemetry`` picks a histogram's unit with ``name.endswith("_ms")``.
    That makes the tail of ``EVENT_LOOP_LAG`` the thing that puts milliseconds
    on somebody's axis rather than a bare number -- so a rename that drops it,
    or a move of the constant to a name spelled differently, silently exports a
    unitless histogram. Asserted end to end through the real adapter, because
    the coupling is between two files that never mention each other.
    """

    meter = _FakeMeter()
    OtelTelemetry(tracer=None, meter=meter).record(EVENT_LOOP_LAG, 1234.0)

    assert meter.histograms == [("runtime.event_loop.lag_ms", "ms")]
    # And the measurement arrived. ``OtelTelemetry.record`` swallows every
    # exception on purpose, so a call that did not fit the instrument would be
    # indistinguishable from a healthy one without this line.
    assert meter.instruments[0].values == [1234.0]


# --------------------------------------------------------------------------
# The control: an idle loop produces nothing
# --------------------------------------------------------------------------


def test_an_unblocked_loop_reports_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Without this, an implementation that always reports passes the rest."""

    telemetry = _Recording()

    async def scenario() -> None:
        await _stop(await _watch_an_idle_loop(telemetry))

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        asyncio.run(scenario())

    assert telemetry.counts == []
    assert telemetry.records == []
    assert _warnings(caplog) == []


# --------------------------------------------------------------------------
# Stopping it leaves nothing behind
# --------------------------------------------------------------------------


def test_cancellation_leaves_no_task_behind() -> None:
    telemetry = _Recording()

    async def scenario() -> None:
        before = asyncio.all_tasks()
        task = asyncio.create_task(_watchdog(telemetry).run_forever())
        await asyncio.sleep(_INTERVAL * 2)
        assert task in asyncio.all_tasks()

        await _stop(task)

        assert task.cancelled()
        # Not merely "the watchdog stopped": nothing it spawned outlived it
        # either. A version that offloaded sampling to a helper task, or that
        # shielded its sleep, would leave something here.
        assert asyncio.all_tasks() - before == set()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# A broken collector is not allowed to kill the watchdog
# --------------------------------------------------------------------------


def test_a_failing_collector_does_not_stop_the_watchdog(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The report is the one place an exception could escape the loop."""

    async def scenario() -> None:
        task = await _watch_a_blocked_loop(_Exploding())
        assert not task.done()
        await _stop(task)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        asyncio.run(scenario())

    # Surviving is half of it. The other half is that the measurement still
    # reached somebody: the log line goes out *before* the telemetry call, so it
    # is what is left when the collector is the thing that is broken -- which is
    # the only reason ``_report`` is written in that order. Moving the
    # ``logger.warning`` below the recording, or inside the ``try``, leaves an
    # incident with nothing in the log but "could not record", and fails here.
    warnings = _warnings(caplog)
    assert len(warnings) == 2
    stall, failure = warnings

    assert stall.levelno == logging.WARNING
    logged_ms = stall.args[0] if stall.args else None
    assert isinstance(logged_ms, float)
    assert _LAG_FLOOR_MS <= logged_ms <= _LAG_CEILING_MS

    # And the collector's own failure is reported rather than swallowed, with
    # the traceback -- a silent ``except Exception: pass`` would leave the
    # watchdog looking healthy while recording nothing at all.
    assert failure.levelno == logging.ERROR
    assert failure.exc_info is not None


# --------------------------------------------------------------------------
# What the shipped defaults are, and why they are numbers rather than config
# --------------------------------------------------------------------------


def test_the_defaults_follow_the_heartbeat_derivation() -> None:
    """WP08-12: sample at ``min(1s, heartbeat/4)``, warn at ``heartbeat/2``.

    Pinned because the derivation lives in a comment rather than in settings:
    with ``coordination.heartbeat_interval_seconds = 20`` shipped in
    ``config/config.default.toml``, those are the two numbers it produces.
    Retuning the heartbeat has to be a change here as well, which is the only
    link left between them once the threshold is a constant.
    """

    heartbeat_interval_seconds = 20.0
    assert min(1.0, heartbeat_interval_seconds / 4) == SAMPLE_INTERVAL_SECONDS
    assert heartbeat_interval_seconds / 2 == WARN_THRESHOLD_SECONDS

    default = EventLoopLagWatchdog(telemetry=_Recording())
    assert default.interval_seconds == SAMPLE_INTERVAL_SECONDS
    assert default.warn_threshold_seconds == WARN_THRESHOLD_SECONDS


# --------------------------------------------------------------------------
# It is actually wired into a process
# --------------------------------------------------------------------------


class _StubDependencies:
    """The little of ``ApiDependencies`` the lifespan reads.

    A stub rather than the real assembly: building the latter needs a database,
    an index and a model, none of which this question is about. Both recovery
    workers are absent and neither chat nor search is served, so the watchdog
    is the only task the lifespan has any reason to start -- which is also the
    deployment shape most likely to be thought of as "nothing running here".
    """

    chat_reaper = None
    chat_pending_recovery = None
    serves_chat = False
    serves_search = False
    max_control_request_body_bytes = 1024

    def __init__(self, telemetry: Telemetry) -> None:
        self.telemetry = SimpleNamespace(telemetry=telemetry)
        self.disposed = 0

    async def startup(self) -> None:
        return None

    async def dispose(self) -> None:
        self.disposed += 1


@asynccontextmanager
async def _lifespan(app: Any) -> AsyncIterator[None]:
    """Drive the ASGI lifespan protocol, which ASGITransport does not."""

    inbox: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    sent: list[str] = []

    async def receive() -> dict[str, str]:
        return await inbox.get()

    async def send(message: dict[str, Any]) -> None:
        sent.append(str(message["type"]))

    driver = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await inbox.put({"type": "lifespan.startup"})
    while "lifespan.startup.complete" not in sent:
        await asyncio.sleep(0)
    try:
        yield
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        await driver


def test_the_api_lifespan_starts_the_watchdog_and_stops_it() -> None:
    """Otherwise the watchdog is correct and nothing in the process runs it."""

    dependencies = _StubDependencies(_Recording())
    app = create_app(dependencies)  # pyright: ignore[reportArgumentType]

    async def scenario() -> None:
        before = asyncio.all_tasks()
        async with _lifespan(app):
            started = {task.get_name(): task for task in asyncio.all_tasks() - before}
            assert WATCHDOG_TASK_NAME in started
            # The name alone would pass for any task at all.
            coro = started[WATCHDOG_TASK_NAME].get_coro()
            assert "EventLoopLagWatchdog.run_forever" in getattr(
                coro, "__qualname__", ""
            )
        # Shutdown cancels it and waits, so nothing survives the lifespan.
        assert asyncio.all_tasks() - before == set()
        assert dependencies.disposed == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("interval", "threshold"),
    [(0.0, 1.0), (-1.0, 1.0), (1.0, 0.0), (1.0, -1.0)],
)
def test_a_nonsense_configuration_is_refused_at_construction(
    interval: float, threshold: float
) -> None:
    """A zero interval is a busy loop; a zero threshold reports every sample."""

    with pytest.raises(ValueError):
        EventLoopLagWatchdog(
            telemetry=_Recording(),
            interval_seconds=interval,
            warn_threshold_seconds=threshold,
        )
