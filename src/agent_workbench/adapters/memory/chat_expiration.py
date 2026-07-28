"""Deterministic in-memory execution-lease expiration.

The production adapter proves atomicity with one PostgreSQL transaction. This
adapter preserves the observable contract for unit tests and offline demos:
one serialized coordinator selects due Turns, closes them, and appends one
idempotent ``ChatTurnExpired`` event per Turn. Process-memory state is not a
durability claim.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from agent_workbench.adapters.memory.conversation_store import (
    InMemoryConversationStore,
)
from agent_workbench.domain.events import ChatTurnExpired
from agent_workbench.ports.conversation_store import (
    StoredChatTurn,
    chat_turn_terminal_event_key,
)
from agent_workbench.ports.event_log import EventLogPort, EventScope

logger = logging.getLogger(__name__)


def _empty_turn_ids() -> set[str]:
    return set()


@dataclass(slots=True)
class InMemoryChatExpirationCoordinator:
    """Serialize due-Turn closure and its terminal observation in one process."""

    conversations: InMemoryConversationStore
    events: EventLogPort
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _quarantined: set[str] = field(
        default_factory=_empty_turn_ids,
        init=False,
        repr=False,
    )

    async def expire_due(self, *, limit: int) -> tuple[StoredChatTurn, ...]:
        if limit < 1:
            raise ValueError("expiration limit must be positive")
        async with self._lock:
            quarantine_before = frozenset(self._quarantined)

            async def publish(turn: StoredChatTurn) -> bool:
                try:
                    await self.events.append(
                        EventScope(stream_id=turn.session_id, run_id=turn.run_id),
                        ChatTurnExpired(turn_id=turn.turn_id),
                        event_key=chat_turn_terminal_event_key(turn.turn_id),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._quarantined.add(turn.turn_id)
                    logger.exception(
                        "isolated failed in-memory Chat expiration candidate %s",
                        turn.turn_id,
                    )
                    return False
                return True

            expired = await self.conversations.expire_due_for_coordinator(
                limit=limit,
                publish=publish,
                excluding=quarantine_before,
            )
            if (
                not expired
                and self._quarantined
                and self._quarantined == set(quarantine_before)
            ):
                # A full scan found only quarantined candidates. Retry them on
                # a later round without letting the oldest poison starve newer
                # Turns when batch_size is one.
                self._quarantined.clear()
            return expired


__all__ = ["InMemoryChatExpirationCoordinator"]
