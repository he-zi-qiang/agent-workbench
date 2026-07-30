"""The cancellable polling loop around durable ingestion outbox draining."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from agent_workbench.workers.ingestion import DrainResult

logger = logging.getLogger(__name__)

Drain = Callable[[], Awaitable[DrainResult]]


@dataclass(frozen=True, slots=True)
class IngestionWorkerRunner:
    """Drain immediately while work exists; wait only on idle or failure."""

    drain: Drain
    poll_seconds: float
    error_backoff_seconds: float

    def __post_init__(self) -> None:
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.error_backoff_seconds <= 0:
            raise ValueError("error_backoff_seconds must be positive")

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """Run until cancellation or an orderly process shutdown."""

        shutdown = stop or asyncio.Event()
        while not shutdown.is_set():
            try:
                result = await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed item remains unacknowledged and becomes claimable
                # after its database lease.  Keeping the process alive avoids
                # turning one malformed document or transient model failure
                # into an ingestion outage for every other document.
                logger.exception("ingestion outbox drain failed")
                await _wait_for_stop(shutdown, self.error_backoff_seconds)
                continue

            handled = result.indexed + result.superseded + result.skipped
            if handled == 0:
                await _wait_for_stop(shutdown, self.poll_seconds)


async def _wait_for_stop(stop: asyncio.Event, timeout_seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout_seconds)


__all__ = ["Drain", "IngestionWorkerRunner"]
