"""How late this event loop is running, measured by the loop itself.

Every blocking call in this process -- a synchronous embedding batch, a PDF
parse, a driver that turned out not to be async after all -- stops *every*
coroutine, not just its own. From the inside that failure is invisible: requests
get slower, a Worker's heartbeat goes out late, and nothing anywhere says the
loop was the thing that stalled.

The measurement is the cheapest one that can say so. Note ``loop.time()``, sleep
for a fixed interval, and subtract: what is left over is how long the loop took
to come back for a callback it had already promised to run. It is not an
estimate of CPU or of queue depth, and it deliberately measures nothing else --
a watchdog that gathered statistics would be another thing occupying the loop it
is supposed to be watching.

Two things this is *not*:

* It is not a heartbeat and it cannot renew a lease. A stalled loop is reported
  here and handled by whoever owns the lease; WP08 assigns that to the Worker.
* It is not a second telemetry stack. It reports through ``ports.telemetry``
  like everything else, so a deployment with no collector gets the log line and
  nothing more -- which is the same answer the rest of the system gives.

The design in the implementation plan (WP08-12) puts the probe on a daemon
thread that pokes the loop with ``call_soon_threadsafe``. That version can also
measure a loop that never comes back at all, which this one cannot: a coroutine
that never gets scheduled also never gets to report. What this one does measure
-- a loop that comes back late -- is the case that actually happens, and it
costs one sleeping task instead of a thread.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from agent_workbench.ports.telemetry import (
    EVENT_LOOP_LAG,
    EVENT_LOOP_STALLED,
    Telemetry,
)

logger = logging.getLogger(__name__)

#: How often to sample. WP08-12 specifies ``min(1s, heartbeat_interval / 4)``,
#: which with the shipped ``coordination.heartbeat_interval_seconds = 20`` is
#: one second. A tighter interval would not find anything this one misses -- a
#: stall long enough to matter spans many samples either way -- and would only
#: make the watchdog itself something the loop has to get back to.
SAMPLE_INTERVAL_SECONDS = 1.0

#: When lag stops being jitter. WP08-12 derives the warning level from the
#: heartbeat: ``heartbeat_interval / 2``, so ten seconds against the shipped
#: default of twenty. That is the point at which a renewal is at risk of being
#: late, which is the first moment a stall has a consequence somebody outside
#: this process can see.
#:
#: Not read from configuration on purpose: a field here would have to be
#: declared in ``config/ownership.yaml`` and projected through settings, and
#: this threshold is a property of the heartbeat, not a deployment knob. The
#: cost is that it does not follow ``heartbeat_interval_seconds`` if that is
#: ever retuned -- whoever changes it has to change this too.
WARN_THRESHOLD_SECONDS = 10.0

_MILLISECONDS = 1000.0


@dataclass(frozen=True, slots=True)
class EventLoopLagWatchdog:
    """Sample the loop's own lateness, and say so when it stops being small."""

    telemetry: Telemetry
    interval_seconds: float = SAMPLE_INTERVAL_SECONDS
    warn_threshold_seconds: float = WARN_THRESHOLD_SECONDS

    def __post_init__(self) -> None:
        # Refuse at construction rather than spin. A zero interval is a busy
        # loop that starves the very thing it claims to measure, and a
        # non-positive threshold reports every sample -- both look like a
        # working watchdog from the outside, which is the worst way to fail.
        if self.interval_seconds <= 0:
            raise ValueError("event loop watchdog interval_seconds must be positive")
        if self.warn_threshold_seconds <= 0:
            raise ValueError(
                "event loop watchdog warn_threshold_seconds must be positive"
            )

    async def run_forever(self) -> None:
        """Sample until the owning process cancels this task.

        Cancellation propagates out of the sleep untouched: this holds nothing
        and spawns nothing, so a cancelled watchdog leaves no task behind. The
        loop deliberately has no ``except Exception`` around it either -- the
        only statement that could raise is the report, and that guards itself.
        """

        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            await asyncio.sleep(self.interval_seconds)
            # What the loop owes minus what was asked for. `loop.time()` is
            # monotonic, so a clock adjustment cannot manufacture a stall.
            lag_seconds = loop.time() - started - self.interval_seconds
            if lag_seconds > self.warn_threshold_seconds:
                self._report(lag_seconds)

    def _report(self, lag_seconds: float) -> None:
        """Log the measurement, then record it. In that order, on purpose.

        The log line is the evidence that survives a broken collector, so it
        goes out before anything that depends on one.
        """

        lag_ms = lag_seconds * _MILLISECONDS
        logger.warning(
            "event loop lagged %.0f ms on a %.0f ms sample (threshold %.0f ms): "
            "something is blocking the loop",
            lag_ms,
            self.interval_seconds * _MILLISECONDS,
            self.warn_threshold_seconds * _MILLISECONDS,
        )
        try:
            # Only on a crossing, not every sample. A measurement of "0 ms, as
            # usual" once a second per process is a series nobody reads and a
            # bill somebody pays; the crossing is the event with a reader.
            self.telemetry.count(EVENT_LOOP_STALLED)
            self.telemetry.record(EVENT_LOOP_LAG, lag_ms)
        except Exception:
            # ``Telemetry`` promises not to raise, and the shipped adapters
            # keep that promise. This is here because the alternative to a
            # broken promise is a watchdog that died quietly during the one
            # incident it existed for -- background tasks are gathered with
            # ``return_exceptions=True`` at shutdown, so nothing would ever
            # report the death.
            logger.exception("event loop lag watchdog could not record %.0f ms", lag_ms)


__all__ = [
    "SAMPLE_INTERVAL_SECONDS",
    "WARN_THRESHOLD_SECONDS",
    "EventLoopLagWatchdog",
]
