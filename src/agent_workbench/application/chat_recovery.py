"""Traffic-independent recovery for abandoned synchronous Chat executions.

A Chat run has no checkpoint from which another process could safely resume.
The reaper therefore does one thing only: terminalize expired ``running``
Turns. It never renews, transfers or automatically replays a model call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from agent_workbench.ports.chat_release import ChatReleaseCoordinator
from agent_workbench.ports.conversation_store import ChatTurnStore, StoredChatTurn
from agent_workbench.ports.event_log import EventSink

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


@dataclass(frozen=True, slots=True)
class ChatPendingReleaseRecovery:
    """Finish prepared answers without depending on the original HTTP client."""

    conversations: ChatTurnStore
    releaser: ChatReleaseCoordinator
    sink_for: Callable[[str, str], EventSink]
    refusal_text: str
    poll_seconds: float
    batch_size: int

    def __post_init__(self) -> None:
        if self.poll_seconds <= 0:
            raise ValueError("chat pending recovery poll_seconds must be positive")
        if self.batch_size < 1:
            raise ValueError("chat pending recovery batch_size must be positive")

    async def run_once(self) -> tuple[StoredChatTurn, ...]:
        """Attempt one stable batch; a bad row cannot poison later candidates."""

        candidates = await self.conversations.list_release_pending(
            limit=self.batch_size
        )
        recovered: list[StoredChatTurn] = []
        for candidate in candidates:
            try:
                recovered.append(
                    await self.releaser.release(
                        turn=candidate.turn,
                        tenant_id=candidate.tenant_id,
                        principal_id=candidate.principal_id,
                        stream_id=candidate.turn.session_id,
                        run_id=candidate.turn.run_id,
                        refusal_text=self.refusal_text,
                        sink=self.sink_for(
                            candidate.turn.session_id,
                            candidate.turn.run_id,
                        ),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # The candidate remains release_pending and will be retried.
                # Isolating rows keeps one corrupt or temporarily locked Turn
                # from aborting the remaining candidates in this batch.
                logger.exception(
                    "failed to recover release-pending chat turn %s",
                    candidate.turn.turn_id,
                )
        return tuple(recovered)

    async def run_forever(self) -> None:
        while True:
            try:
                recovered = await self.run_once()
                if recovered:
                    logger.info(
                        "recovered %d release-pending chat turn(s)",
                        len(recovered),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("chat pending-release recovery iteration failed")
            await asyncio.sleep(self.poll_seconds)


__all__ = ["ChatPendingReleaseRecovery", "ChatTurnReaper"]
