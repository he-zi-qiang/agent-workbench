"""``WorkerPresenceStore`` over the ``worker_presence`` table (ADR-0110).

One upsert per heartbeat and one read per console poll; both stamp time from
``now()`` on the server, never from the process, for the reason the port
states: liveness here is judged by the clock the Task lease is judged by.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence.models import worker_presence
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.ports.worker_presence import (
    WorkerKind,
    WorkerPresence,
    WorkerPresenceReport,
)


class PostgresWorkerPresenceStore:
    """``WorkerPresenceStore`` in PostgreSQL."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

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
        # `now() + interval` in SQL rather than a Python timestamp: the reader
        # compares `expires_at` with the same `now()`, so the two never disagree
        # by however far this process's clock drifts from the server's.
        ttl = func.make_interval(0, 0, 0, 0, 0, 0, float(ttl_seconds))
        statement = pg_insert(worker_presence).values(
            worker_id=worker_id,
            kind=kind,
            deployment=deployment,
            capabilities=dict(capabilities),
            started_at=started_at,
            heartbeat_at=func.now(),
            expires_at=func.now() + ttl,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[worker_presence.c.worker_id],
            set_={
                "kind": statement.excluded.kind,
                "deployment": statement.excluded.deployment,
                "capabilities": statement.excluded.capabilities,
                "started_at": statement.excluded.started_at,
                "heartbeat_at": statement.excluded.heartbeat_at,
                "expires_at": statement.excluded.expires_at,
            },
        ).returning(*worker_presence.c)
        async with self._engine.begin() as connection:
            row = (await connection.execute(statement)).mappings().one()
        return _row(row)

    async def forget(self, worker_id: Identifier) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(worker_presence).where(worker_presence.c.worker_id == worker_id)
            )

    async def report(self) -> WorkerPresenceReport:
        async with self._engine.connect() as connection:
            observed_at = (await connection.execute(select(func.now()))).scalar_one()
            rows = (
                (
                    await connection.execute(
                        select(worker_presence).order_by(
                            worker_presence.c.kind, worker_presence.c.worker_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        return WorkerPresenceReport(
            observed_at=observed_at,
            workers=tuple(_row(row) for row in rows),
        )


def _row(row: Any) -> WorkerPresence:
    return WorkerPresence(
        worker_id=cast(str, row["worker_id"]),
        kind=cast(WorkerKind, row["kind"]),
        deployment=cast(str, row["deployment"]),
        capabilities=cast("dict[str, object]", row["capabilities"] or {}),
        started_at=cast(datetime, row["started_at"]),
        heartbeat_at=cast(datetime, row["heartbeat_at"]),
        expires_at=cast(datetime, row["expires_at"]),
    )


__all__ = ["PostgresWorkerPresenceStore"]
