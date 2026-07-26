"""Draining the ingestion outbox, competitively.

``SKIP LOCKED`` is what lets several ingestion workers share one queue without
talking to each other: each claim takes rows nobody else is holding, and two
workers never see the same event. The alternative -- partitioning the queue by
worker -- makes a dead worker's share invisible until somebody notices.

The claim is not a lease. A worker that dies holding one leaves it held, and
nothing here reclaims it. Lease duration, heartbeat and fencing belong to the
coordination work package; half of that machinery would look recoverable
without being it, which is worse than an obvious gap.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence.models import outbox_events
from agent_workbench.domain.schema import JsonObject
from agent_workbench.ports.outbox import OutboxEvent, OutboxEventKind

DEFAULT_CLAIM_LIMIT = 10


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
    ) -> tuple[OutboxEvent, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")

        # Selected in one statement and updated by primary key, so the rows
        # stay locked for the whole claim and no second worker can take them.
        candidates = (
            select(outbox_events.c.sequence)
            .where(outbox_events.c.acked_at.is_(None))
            .where(outbox_events.c.claimed_at.is_(None))
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
                    .values(claimed_by=worker_id, claimed_at=func.now())
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
                    )
                    for row in rows
                ),
                key=lambda event: event.sequence,
            )
        )

    async def ack(self, *, event_id: str) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                update(outbox_events)
                .where(outbox_events.c.event_id == event_id)
                .values(acked_at=func.now())
            )

    async def pending_count(self) -> int:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                select(func.count()).where(outbox_events.c.acked_at.is_(None))
            )
            return result.scalar_one()


__all__ = ["DEFAULT_CLAIM_LIMIT", "PostgresOutbox"]
