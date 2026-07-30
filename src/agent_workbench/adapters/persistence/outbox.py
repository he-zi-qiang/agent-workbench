"""Draining the ingestion outbox, competitively.

``SKIP LOCKED`` is what lets several ingestion workers share one queue without
talking to each other: each claim takes rows nobody else is holding, and two
workers never see the same event. The alternative -- partitioning the queue by
worker -- makes a dead worker's share invisible until somebody notices.

A claim is a lease. It expires, so a worker that dies holding one does not take
its share of the queue with it: the next claim finds the lease stale and picks
the work back up.

Expiry alone would be unsafe, which is why the fence is not optional. A worker
that merely stalled -- a long GC pause, a network partition -- is still alive
when its lease expires and the work is reclaimed. Coming back, it would
acknowledge a unit somebody else is now holding, and that acknowledgement would
mark the *other* worker's in-flight work as done. Every claim therefore mints a
token, and acknowledging requires the current one. A stale token matches no row
and is refused.

What is still missing is a heartbeat: a worker doing honest slow work cannot
extend its own lease and will lose it. That belongs with the ingestion worker
itself, and until one exists the lease should simply be set longer than the
slowest unit of work.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence.models import outbox_events
from agent_workbench.domain.errors import StaleExecutionError
from agent_workbench.domain.identifiers import new_id
from agent_workbench.domain.schema import JsonObject
from agent_workbench.ports.outbox import OutboxEvent, OutboxEventKind

DEFAULT_CLAIM_LIMIT = 10

# Long enough that ordinary work finishes inside it, since nothing can extend
# a lease yet. Shorter is not safer here: it reclaims live work.
DEFAULT_LEASE_SECONDS = 60.0

CLAIM_TOKEN_PREFIX = "clm"


class PostgresOutbox:
    """Competitive claim and acknowledgement over ``outbox_events``."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def claim(
        self,
        *,
        worker_id: str,
        limit: int = DEFAULT_CLAIM_LIMIT,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> tuple[OutboxEvent, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        token = new_id(CLAIM_TOKEN_PREFIX)
        lease = func.now() + func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds)

        # Selected in one statement and updated by primary key, so the rows
        # stay locked for the whole claim and no second worker can take them.
        # Expiry is read from the database clock, never a worker's: two
        # workers disagreeing about the time is exactly how the same lease
        # gets held twice.
        candidates = (
            select(outbox_events.c.sequence)
            .where(outbox_events.c.acked_at.is_(None))
            .where(
                or_(
                    outbox_events.c.claimed_at.is_(None),
                    outbox_events.c.lease_until < func.now(),
                )
            )
            .order_by(outbox_events.c.sequence)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )

        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    update(outbox_events)
                    .where(outbox_events.c.sequence.in_(candidates))
                    .values(
                        claimed_by=worker_id,
                        claimed_at=func.now(),
                        lease_until=lease,
                        claim_token=token,
                    )
                    .returning(
                        outbox_events.c.sequence,
                        outbox_events.c.event_id,
                        outbox_events.c.document_id,
                        outbox_events.c.source_revision,
                        outbox_events.c.kind,
                        outbox_events.c.payload,
                    )
                )
            ).all()

        return tuple(
            sorted(
                (
                    OutboxEvent(
                        sequence=cast(int, row.sequence),
                        event_id=cast(str, row.event_id),
                        document_id=cast(str, row.document_id),
                        source_revision=cast(int, row.source_revision),
                        kind=cast(OutboxEventKind, row.kind),
                        payload=cast(JsonObject, row.payload),
                        claim_token=token,
                    )
                    for row in rows
                ),
                key=lambda event: event.sequence,
            )
        )

    async def ack(self, *, event_id: str, claim_token: str) -> None:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                update(outbox_events)
                .where(outbox_events.c.event_id == event_id)
                .where(outbox_events.c.claim_token == claim_token)
                .values(acked_at=func.now())
            )
        if result.rowcount == 0:
            # The row is gone, or its token has moved on. Either way this
            # worker is not the one entitled to close it, and the rowcount is
            # the only thing that can tell us -- an UPDATE that matches
            # nothing succeeds.
            raise StaleExecutionError("the claim on this event is no longer current")

    async def heartbeat(
        self,
        *,
        event_id: str,
        claim_token: str,
        lease_seconds: float,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        lease = func.now() + func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds)
        async with self._engine.begin() as connection:
            result = await connection.execute(
                update(outbox_events)
                .where(outbox_events.c.event_id == event_id)
                .where(outbox_events.c.claim_token == claim_token)
                .where(outbox_events.c.acked_at.is_(None))
                .values(lease_until=lease)
            )
        if result.rowcount == 0:
            raise StaleExecutionError("the claim on this event is no longer current")

    async def release(self, *, event_id: str, claim_token: str) -> None:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                update(outbox_events)
                .where(outbox_events.c.event_id == event_id)
                .where(outbox_events.c.claim_token == claim_token)
                .where(outbox_events.c.acked_at.is_(None))
                .values(
                    claimed_by=None,
                    claimed_at=None,
                    lease_until=None,
                    claim_token=None,
                )
            )
        if result.rowcount == 0:
            raise StaleExecutionError("the claim on this event is no longer current")

    async def pending_count(self) -> int:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                select(func.count()).where(outbox_events.c.acked_at.is_(None))
            )
            return result.scalar_one()


__all__ = [
    "CLAIM_TOKEN_PREFIX",
    "DEFAULT_CLAIM_LIMIT",
    "DEFAULT_LEASE_SECONDS",
    "PostgresOutbox",
]
