"""The outbox boundary: work the index still owes the database.

An event is written in the same transaction as the fact it describes. That is
the whole point: a document that committed without its event would be a
document the index is never told about, and an event that committed without its
document would be an index entry for content that does not exist.

Claiming is competitive and skips what another worker already holds, so several
ingestion workers can drain one queue without coordinating. What this port does
not have yet is a lease: a worker that dies holding a claim leaves it held. The
recovery machinery for that -- lease duration, heartbeat, fencing -- belongs to
the coordination work package, and building half of it here would produce
something that looks recoverable and is not.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import JsonObject, VersionedModel

OutboxEventKind = Literal["document_upserted", "document_deleted", "acl_changed"]


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


@runtime_checkable
class OutboxPort(Protocol):
    """Competitive draining of pending ingestion work."""

    async def claim(
        self,
        *,
        worker_id: str,
        limit: int = 10,
    ) -> tuple[OutboxEvent, ...]:
        """Take up to ``limit`` unclaimed events, skipping any already held."""
        ...

    async def ack(self, *, event_id: str) -> None:
        """Mark one claimed event as applied."""
        ...

    async def pending_count(self) -> int:
        """How many events are still unacknowledged."""
        ...


__all__ = ["OutboxEvent", "OutboxEventKind", "OutboxPort"]
