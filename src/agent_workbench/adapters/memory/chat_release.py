"""Deterministic in-memory Chat release coordinator.

The production guarantee lives in PostgreSQL, where source locks, event append
and Turn transition share one transaction. This adapter preserves the same
observable contract for unit tests and offline demos; its single-process
components are not a distributed authorization fence.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.domain.events import AnswerCommitted, AnswerWithheld
from agent_workbench.ports.chat_release import EvidenceRevisionGuard
from agent_workbench.ports.conversation_store import (
    ChatTurnConflictError,
    ChatTurnResult,
    ChatTurnStore,
    StoredChatTurn,
    chat_turn_terminal_event_key,
)
from agent_workbench.ports.event_log import EventSink


@dataclass(frozen=True, slots=True)
class InMemoryChatReleaseCoordinator:
    conversations: ChatTurnStore
    revisions: EvidenceRevisionGuard

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
        if turn.run_id != run_id:
            raise ChatTurnConflictError("chat release run id conflict")
        if stream_id != turn.session_id:
            raise ChatTurnConflictError(
                "a chat answer stream must be its conversation session"
            )
        if turn.status in {"committed", "withheld"}:
            return turn
        if turn.status != "release_pending" or turn.result is None:
            raise ChatTurnConflictError("chat turn is not ready for release")

        result = turn.result
        withheld_result: ChatTurnResult | None = None
        if not result.withheld and not await self.revisions.revisions_unchanged(
            result.authorized_revisions,
            tenant_id=tenant_id,
            principal_id=principal_id,
        ):
            withheld_result = _withheld(result, refusal_text)
            result = withheld_result

        event_key = chat_turn_terminal_event_key(turn.turn_id)
        if result.withheld:
            await sink.emit(
                AnswerWithheld(text=result.answer),
                event_key=event_key,
            )
        else:
            await sink.emit(
                AnswerCommitted(
                    text=result.answer,
                    citations=result.citations,
                ),
                event_key=event_key,
            )
        return await self.conversations.mark_released(
            session_id=turn.session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            turn_id=turn.turn_id,
            withheld_result=withheld_result,
        )


def _withheld(result: ChatTurnResult, refusal_text: str) -> ChatTurnResult:
    return ChatTurnResult(
        outcome=result.outcome.model_copy(
            update={"output_text": "", "output_ref": None, "citations": ()}
        ),
        answer=refusal_text,
        authorized_revisions=(),
        citations=(),
        withheld=True,
    )


__all__ = ["InMemoryChatReleaseCoordinator"]
