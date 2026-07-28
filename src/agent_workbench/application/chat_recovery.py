"""Traffic-independent recovery for abandoned synchronous Chat executions.

A Chat run has no checkpoint from which another process could safely resume.
The reaper therefore does one thing only: terminalize expired ``running``
Turns. It never renews, transfers or automatically replays a model call.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from agent_workbench.ports.conversation_store import ChatTurnStore, StoredChatTurn

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChatTurnReaper:
    """Periodically converge hard-crash orphans to a stable failed fact."""

    conversations: ChatTurnStore
    poll_seconds: float
    batch_size: int

    def __post_init__(self) -> None:
        if self.poll_seconds <= 0:
            raise ValueError("chat reaper poll_seconds must be positive")
        if self.batch_size < 1:
            raise ValueError("chat reaper batch_size must be positive")

    async def run_once(self) -> tuple[StoredChatTurn, ...]:
        return await self.conversations.reap_expired_running(limit=self.batch_size)

    async def run_forever(self) -> None:
        """Run until the owning process cancels this background task."""

        while True:
            try:
                expired = await self.run_once()
                if expired:
                    logger.warning(
                        "terminalized %d expired chat turn(s)",
                        len(expired),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # One database outage must not silently kill recovery forever.
                logger.exception("chat turn reaper iteration failed")
            await asyncio.sleep(self.poll_seconds)


__all__ = ["ChatTurnReaper"]
