"""The atomic boundary for terminalizing expired Chat executions.

A synchronous Chat execution cannot be resumed safely after its fixed lease
expires.  Expiry therefore records one stable failure in the Turn ledger and
one durable terminal observation.  Those facts must share a transaction:
neither a failed Turn without its event nor an event for a still-running Turn
is a valid state.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_workbench.ports.conversation_store import StoredChatTurn


@runtime_checkable
class ChatExpirationCoordinator(Protocol):
    """Atomically fail one bounded batch of due, still-running Chat Turns."""

    async def expire_due(
        self,
        *,
        limit: int,
    ) -> tuple[StoredChatTurn, ...]:
        """Return the Turns terminalized with their durable expiry events."""
        ...


__all__ = ["ChatExpirationCoordinator"]
