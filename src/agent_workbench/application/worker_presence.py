"""The heartbeat a Worker process sends about itself (ADR-0110).

**Fail-soft, in every direction.** A Worker exists to claim and run work; the
row it writes here is a readout for the console. So a store that cannot be
written -- the migration not applied, the database briefly away -- is logged
and retried on the next beat, and never stops the process. The lease that
*does* govern liveness is the Registry's, and it is untouched by this module.

**Announce first, then beat.** The first row is written before the caller
starts claiming, so a console polling at the moment a Worker comes up sees it
within one request rather than one interval. On an orderly exit the row is
removed: a clean stop reads as "absent", a crash reads as "stale since HH:MM",
and the console can tell the two apart -- which is the whole reason the reader
keeps stale rows instead of expiring them away.

**TTL is a multiple of the interval, not a separate knob.** Three beats missed
is the same rule the Task lease applies to its own heartbeat lateness; a Worker
that is late by less than that is not yet a Worker that is gone.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from types import TracebackType

from agent_workbench.ports.worker_presence import WorkerKind, WorkerPresenceStore

logger = logging.getLogger(__name__)

#: Beats a Worker may miss before its row reads as stale.
TTL_BEATS = 3


class WorkerPresenceBeacon:
    """Keep one Worker's row fresh for as long as the process runs."""

    def __init__(
        self,
        store: WorkerPresenceStore,
        *,
        worker_id: str,
        kind: WorkerKind,
        deployment: str,
        capabilities: dict[str, object],
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._store = store
        self._worker_id = worker_id
        self._kind: WorkerKind = kind
        self._deployment = deployment
        self._capabilities = dict(capabilities)
        self._interval = float(interval_seconds)
        self._started_at = datetime.now(UTC)
        self._loop: asyncio.Task[None] | None = None

    @property
    def ttl_seconds(self) -> float:
        return self._interval * TTL_BEATS

    async def announce(self) -> bool:
        """Write one beat. ``False`` when the store refused; never raises."""

        try:
            await self._store.announce(
                worker_id=self._worker_id,
                kind=self._kind,
                deployment=self._deployment,
                capabilities=self._capabilities,
                started_at=self._started_at,
                ttl_seconds=self.ttl_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "worker_presence_announce_failed",
                extra={
                    "worker_id": self._worker_id,
                    "kind": self._kind,
                    "error_type": type(error).__name__,
                },
            )
            return False
        return True

    async def start(self) -> None:
        if self._loop is not None:
            return
        await self.announce()
        self._loop = asyncio.create_task(
            self._beat_forever(), name=f"worker-presence:{self._worker_id}"
        )

    async def stop(self) -> None:
        loop, self._loop = self._loop, None
        if loop is not None:
            loop.cancel()
            with suppress(asyncio.CancelledError):
                await loop
        try:
            await self._store.forget(self._worker_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "worker_presence_forget_failed",
                extra={
                    "worker_id": self._worker_id,
                    "error_type": type(error).__name__,
                },
            )

    async def _beat_forever(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.announce()

    async def __aenter__(self) -> WorkerPresenceBeacon:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()


__all__ = ["TTL_BEATS", "WorkerPresenceBeacon"]
