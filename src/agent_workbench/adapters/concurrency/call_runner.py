"""A bounded home for calls that block the thread they run on.

ADR-042. Three model adapters and the local artifact store all do compute- or
IO-bound synchronous work. Two of them already hand it to
``asyncio.to_thread``, which is better than blocking the loop and still not
bounded: ``to_thread`` uses the interpreter's *default* executor, whose size is
``min(32, cpu_count + 4)`` and whose work queue is an unbounded ``SimpleQueue``.
Everything else that reaches for a thread on this process -- notably
``getaddrinfo`` behind every outbound connection -- queues in the same place.
So a burst of embedding batches does not merely slow embedding down; it puts
DNS behind a queue nobody can see the length of.

This runner is the bound. It is deliberately small:

* a dedicated ``ThreadPoolExecutor``, so the queue this work waits in is not
  the one the rest of the process waits in;
* an ``asyncio.Semaphore`` of the *same* size, and both are load-bearing. The
  executor is what actually limits concurrency. The semaphore exists to make
  the waiting observable and, above all, *bounded in time* -- a
  ``ThreadPoolExecutor``'s queue cannot be given a timeout, so without this
  every caller would wait forever and the only symptom would be latency. Two
  equal numbers look redundant; deleting either one removes a different thing.
* a queue timeout that covers **only** the wait for a slot. A legitimately slow
  call is not this class's business, and killing one would turn "the machine is
  busy" into "your embedding failed".

What does *not* belong here is written down as firmly as what does. Only
read-only, idempotent, cancellation-safe work goes through it. A cancelled
caller stops awaiting, but the thread keeps running to completion -- there is
no way to interrupt arbitrary synchronous code -- so anything whose partial
execution is observable must not use this path. ``LocalArtifactStore.put`` is
the concrete example the plan already called out: its quarantine-then-replace
sequence leaves half a file on disk if abandoned midway.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from types import TracebackType

from agent_workbench.domain.errors import ProviderError

logger = logging.getLogger(__name__)


class BlockingCallQueueTimeoutError(ProviderError):
    """No slot became free in time. The work never started.

    Retryable, and truthfully so: nothing ran, so nothing has to be undone.
    This is the backpressure signal the default executor cannot produce -- it
    would simply have kept queueing.
    """

    code = "provider_error"


class BlockingCallRunner:
    """Runs synchronous callables on a bounded, private thread pool."""

    def __init__(
        self,
        *,
        slots: int,
        queue_timeout_seconds: float,
        thread_name_prefix: str = "aw-blocking",
    ) -> None:
        if slots < 1:
            raise ValueError("blocking call runner needs at least one slot")
        if queue_timeout_seconds <= 0:
            # A zero timeout makes every contended call fail instead of wait,
            # which is the refusal semantics ADR-042 §7 rejected on purpose.
            raise ValueError("blocking call queue timeout must be positive")
        self._slots = slots
        self._queue_timeout_seconds = queue_timeout_seconds
        self._executor = ThreadPoolExecutor(
            max_workers=slots, thread_name_prefix=thread_name_prefix
        )
        # Created lazily: a runner is assembled during composition, which may
        # not be inside the loop that will use it, and binding a Semaphore to
        # the wrong loop fails much later and much less clearly.
        self._slots_free: asyncio.Semaphore | None = None
        self._closed = False

    @property
    def slots(self) -> int:
        return self._slots

    @property
    def queue_timeout_seconds(self) -> float:
        return self._queue_timeout_seconds

    async def run[T](self, work: Callable[[], T], *, name: str) -> T:
        """Wait for a slot, then run ``work`` on this runner's own threads.

        ``name`` is for the log line when waiting times out; it should say what
        blocked, not where it was called from -- the traceback already has the
        latter.
        """

        if self._closed:
            raise RuntimeError("blocking call runner is closed")
        semaphore = self._ensure_semaphore()
        try:
            await asyncio.wait_for(
                semaphore.acquire(), timeout=self._queue_timeout_seconds
            )
        except TimeoutError as timed_out:
            logger.warning(
                "blocking call %s waited %.0fs for one of %d slots and gave up; "
                "the pool is saturated",
                name,
                self._queue_timeout_seconds,
                self._slots,
            )
            raise BlockingCallQueueTimeoutError(
                f"no blocking-call slot for {name} within "
                f"{self._queue_timeout_seconds:.0f}s"
            ) from timed_out
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, work)
        finally:
            # Released even when the caller was cancelled mid-await. The thread
            # may still be running -- that is inherent and accepted for the
            # read-only work this runner takes -- but the *slot* must not leak,
            # or a few cancellations would starve the pool permanently.
            semaphore.release()

    def _ensure_semaphore(self) -> asyncio.Semaphore:
        if self._slots_free is None:
            self._slots_free = asyncio.Semaphore(self._slots)
        return self._slots_free

    def close(self) -> None:
        """Stop accepting work and let running threads finish.

        Not ``cancel_futures``: the calls this runner accepts are read-only, so
        letting them end costs a moment and abandoning them costs a thread
        writing into a half-torn-down process.
        """

        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False)

    def __enter__(self) -> BlockingCallRunner:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


async def offload[T](
    runner: BlockingCallRunner | None, work: Callable[[], T], *, name: str
) -> T:
    """Run ``work`` off the loop, through ``runner`` when there is one.

    ``None`` falls back to ``asyncio.to_thread`` -- the interpreter's shared,
    unbounded default executor. That is not a second supported deployment
    shape; it is what keeps a narrow unit test from having to assemble a pool.
    Every production composition passes a runner, and the fallback is spelled
    out here in one place rather than repeated at five call sites, so that
    "this call is unbounded" is a single grep away instead of five.
    """

    if runner is None:
        return await asyncio.to_thread(work)
    return await runner.run(work, name=name)


__all__ = ["BlockingCallQueueTimeoutError", "BlockingCallRunner", "offload"]
