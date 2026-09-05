"""In-memory ``WorkerPresenceStore`` with an injectable clock.

The clock is a callable rather than ``datetime.now`` so a contract test can
move time past a row's expiry without sleeping; PostgreSQL's version reads
``now()`` and the contract is the same either way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.ports.worker_presence import (
    WorkerKind,
    WorkerPresence,
    WorkerPresenceReport,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InMemoryWorkerPresenceStore:
    """``WorkerPresenceStore`` over a dict."""

    __slots__ = ("_clock", "_lock", "_rows")

    def __init__(self, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock
        self._rows: dict[str, WorkerPresence] = {}
        self._lock = asyncio.Lock()

    async def announce(
        self,
        *,
        worker_id: Identifier,
        kind: WorkerKind,
        deployment: str,
        capabilities: dict[str, object],
        started_at: datetime,
        ttl_seconds: float,
    ) -> WorkerPresence:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = self._clock()
        row = WorkerPresence(
            worker_id=worker_id,
            kind=kind,
            deployment=deployment,
            capabilities=dict(capabilities),
            started_at=started_at,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        async with self._lock:
            self._rows[worker_id] = row
        return row

    async def forget(self, worker_id: Identifier) -> None:
        async with self._lock:
            self._rows.pop(worker_id, None)

    async def report(self) -> WorkerPresenceReport:
        async with self._lock:
            rows = tuple(
                sorted(self._rows.values(), key=lambda row: (row.kind, row.worker_id))
            )
        return WorkerPresenceReport(observed_at=self._clock(), workers=rows)


__all__ = ["InMemoryWorkerPresenceStore"]
