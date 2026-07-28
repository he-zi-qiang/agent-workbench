"""The final, revision-fenced publication boundary for Chat answers.

Checking an ACL and publishing an answer in separate transactions is a
time-of-check/time-of-use bug. A production coordinator therefore owns the
whole commit: lock the source revisions, re-authorize them, append the terminal
answer event, append the visible assistant message and transition the Turn in
one transaction.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_workbench.ports.conversation_store import (
    AuthorizedRevision,
    StoredChatTurn,
)
from agent_workbench.ports.event_log import EventSink


@runtime_checkable
class EvidenceRevisionGuard(Protocol):
    """Read whether an authorization snapshot is still current."""

    async def revisions_unchanged(
        self,
        revisions: tuple[AuthorizedRevision, ...],
        *,
        tenant_id: str,
        principal_id: str,
    ) -> bool: ...


@runtime_checkable
class ChatReleaseCoordinator(Protocol):
    """Atomically publish or safely withhold one release-pending Turn."""

    async def release(
        self,
        *,
        turn: StoredChatTurn,
        tenant_id: str,
        principal_id: str,
        stream_id: str,
        run_id: str,
        refusal_text: str,
        sink: EventSink,
    ) -> StoredChatTurn:
        """Return the terminal Turn produced at the authorization fence.

        A PostgreSQL implementation must commit source authorization, the
        durable answer event and the visible assistant message in one unit of
        work. ``sink`` lets deterministic in-memory adapters preserve the same
        event contract; a database coordinator writes through its own
        transaction-aware EventLog helper instead.
        """
        ...


__all__ = ["ChatReleaseCoordinator", "EvidenceRevisionGuard"]
