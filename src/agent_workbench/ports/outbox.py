"""The outbox boundary: work the index still owes the database.

An event is written in the same transaction as the fact it describes. That is
the whole point: a document that committed without its event would be a
document the index is never told about, and an event that committed without its
document would be an index entry for content that does not exist.

Claiming is competitive and skips what another worker already holds, so several
ingestion workers can drain one queue without coordinating.

A claim is a lease. It expires, so a worker that dies holding one does not take
its share of the queue with it, and the next claim picks the work back up. What
makes that safe is the fence: every claim mints a token, and an acknowledgement
carries the token it was given. A worker that stalled past its lease, had the
work reclaimed and then came back finds its token no longer current and is
refused -- rather than marking as done a unit of work somebody else is now
holding, which is the failure a bare lease introduces.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import JsonObject, VersionedModel

OutboxEventKind = Literal[
    "document_upserted",
    "document_deleted",
    "acl_changed",
    # The second pass (ADR-037 §2.6). Enqueued by the first one in the same
    # transaction that records the version as indexed, so a graph is only ever
    # asked for over content the index already holds -- and claimed, leased,
    # heartbeaten and retried by the machinery the other kinds already use,
    # rather than by a scheduler of its own.
    "graph_extraction_requested",
]


class OutboxEvent(VersionedModel):
    """One unit of work the ingestion side has not applied yet."""

    sequence: int = Field(ge=1)
    event_id: Identifier
    document_id: Identifier
    # The document revision this event describes. An event whose revision is
    # older than what the worker last applied describes a past state and is
    # superseded rather than replayed over newer content.
    source_revision: int = Field(ge=1)
    kind: OutboxEventKind
    payload: JsonObject
    # The fence for this claim. Acknowledging requires it, so a worker whose
    # lease expired cannot close work that has since moved on.
    claim_token: Identifier


@runtime_checkable
class OutboxPort(Protocol):
    """Competitive draining of pending ingestion work."""

    async def claim(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: float = 60.0,
    ) -> tuple[OutboxEvent, ...]:
        """Lease up to ``limit`` events, skipping any lease still current.

        Events whose lease has expired are claimable again, under a new token.
        """
        ...

    async def ack(self, *, event_id: str, claim_token: str) -> None:
        """Mark one leased event as applied.

        Raises ``StaleExecutionError`` when the token is not the current one:
        the lease expired and the work was reclaimed, so this worker is no
        longer the one entitled to close it.
        """
        ...

    async def heartbeat(
        self,
        *,
        event_id: str,
        claim_token: str,
        lease_seconds: float,
    ) -> None:
        """Extend one current claim using the database clock.

        Raises ``StaleExecutionError`` when the event has been reclaimed.
        """
        ...

    async def release(self, *, event_id: str, claim_token: str) -> None:
        """Yield a current claim without acknowledging the work.

        Used when the per-document writer guard is already held elsewhere.
        The token fence prevents a stale Worker from releasing a newer claim.
        """
        ...

    async def pending_count(self) -> int:
        """How many events are still unacknowledged."""
        ...


__all__ = ["OutboxEvent", "OutboxEventKind", "OutboxPort"]
