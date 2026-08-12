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

Honest slow work extends its own lease through ``heartbeat``, which renews the
claim rather than an event in it. That distinction is the whole reason the
method takes a token and no event id: a claim leases a batch in one statement,
and a batch renewed one row at a time is a batch whose untouched tail expires
on schedule while its holder is still working through the head.

``DEFAULT_LEASE_SECONDS`` therefore sizes the gap a worker may go *silent*
for, not the time a batch may take. Making it longer no longer buys safety for
slow work -- the heartbeat does that -- and costs reclaim latency after a
worker dies, which is the only thing it still governs.
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

# How long a worker may go silent before its claim is fair game -- not how
# long its work may take, which the heartbeat covers. Shorter is not safer:
# it reclaims work from holders that are merely quiet between renewals.
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
        claim_token: str,
        lease_seconds: float,
    ) -> None:
        """Extend the whole claim this token minted, not one event of it.

        Keyed on the token alone, and that is the fix rather than a
        convenience. A claim leases up to ``limit`` events *in one statement*,
        giving every row the same expiry; the worker then applies them one at
        a time. A heartbeat that named a single event renewed only the row
        being worked on, so the rest of the batch went on expiring against a
        lease sized for one unit of work -- ``IngestionWorker`` claims 32 with
        a 90-second lease, and a batch that takes longer than that has its
        tail quietly become claimable while this worker still intends to
        apply it. Two workers then hold the same event, which is the one
        outcome the fence exists to prevent.

        Renewing by token restores the shape the module docstring describes:
        the claim is the unit of ownership, so it is the unit of renewal.
        Every row moves together -- granted together, renewed together, and,
        if this worker dies, expiring together.

        Raises ``StaleExecutionError`` only when *nothing* is left to renew,
        which is the honest reading of zero matched rows: the lease is gone,
        or the work was reclaimed, or every event in it is already acked. All
        three mean this worker no longer holds anything, and it is that raise
        the ingestion worker treats as "stop writing".
        """

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        lease = func.now() + func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds)
        async with self._engine.begin() as connection:
            result = await connection.execute(
                update(outbox_events)
                .where(outbox_events.c.claim_token == claim_token)
                .where(outbox_events.c.acked_at.is_(None))
                # The strict inverse of what ``claim`` treats as claimable,
                # and the token fence cannot stand in for it. A token only
                # rotates when somebody else claims, so in the window between
                # a lease running out and another worker getting there, the
                # stalled holder's token still matches: without this predicate
                # it extends itself back out of the reclaim queue, and expiry
                # stops meaning anything for exactly the worker it exists for
                # -- one sick enough to stall past its lease but well enough
                # to keep heartbeating. It is also the answer the ingestion
                # worker reads as "the fence still holds" while it writes to
                # the index, and only a raise here cancels that write, so
                # saying yes on a dead lease is what lets two workers apply
                # the same document at once. ``>=`` rather than ``>`` because
                # ``claim`` reclaims on ``lease_until < now()``: the two must
                # not disagree about the instant a lease ends, or a row could
                # be neither claimable by anyone nor renewable by its holder.
                .where(outbox_events.c.lease_until >= func.now())
                .values(lease_until=lease)
            )
        if result.rowcount == 0:
            raise StaleExecutionError("this claim is no longer current")

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
