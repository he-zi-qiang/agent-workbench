"""Deterministic in-memory Chat release coordinator.

The production guarantee lives in PostgreSQL, where source locks, event append
and Turn transition share one transaction. This adapter preserves the same
observable contract for unit tests and offline demos; its single-process
components are not a distributed authorization fence.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.domain.events import (
    AnswerCommitted,
    AnswerWithheld,
    UngroundedAnswerCommitted,
)
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
        # The same three-way choice PostgreSQL makes, read from the same fact.
        # This adapter is test-only, which is exactly why the branch has to be
        # here: a double that answered `AnswerCommitted` for every non-withheld
        # turn leaves every suite built on it unable to fail when a coordinator
        # collapses the two events, and ADR-018's distinction between a
        # verified answer and an unverified one would then be pinned only by
        # the service-backed suites -- the ones that run when a database
        # happens to be up.
        payload: AnswerCommitted | UngroundedAnswerCommitted | AnswerWithheld
        if result.withheld:
            payload = AnswerWithheld(text=result.answer)
        elif result.grounded:
            payload = AnswerCommitted(
                text=result.answer,
                citations=result.citations,
            )
        else:
            payload = UngroundedAnswerCommitted(text=result.answer)
        await sink.emit(payload, event_key=event_key)
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
        # Carried, not defaulted, though nothing can observe the difference
        # yet. No ungrounded candidate reaches this function today: an
        # ungrounded result is forbidden to hold authorized revisions, and a
        # revision guard short-circuits to True on an empty tuple, so the one
        # withhold trigger that exists cannot fire for one.
        #
        # The keyword is here because the default is `True` and this value
        # outlives the refusal -- `mark_released` stores the replacement and
        # the API reads `turn.grounded` straight off it. The first withhold
        # trigger that does not depend on revisions would relabel an unverified
        # answer as a verified one at the moment the system refused to publish
        # it, which is the worst direction to be wrong in at the worst moment.
        grounded=result.grounded,
    )


__all__ = ["InMemoryChatReleaseCoordinator"]
