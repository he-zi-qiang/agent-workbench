"""Atomic, revision-fenced publication of PostgreSQL-backed Chat answers.

The ordinary retrieval check answers whether evidence is readable at one
instant. Publication needs a stronger guarantee: no compliant content or ACL
writer may move any cited document between that check and the durable answer
event. This coordinator takes the document row locks that those writers also
take, re-checks the exact revisions, and commits the event, assistant message
and Turn transition in the same database transaction.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.conversation_store import (
    PostgresConversationStore,
)
from agent_workbench.adapters.persistence.event_log import PostgresEventLog
from agent_workbench.adapters.persistence.models import document_acl, documents
from agent_workbench.domain.events import (
    AnswerCommitted,
    AnswerWithheld,
    UngroundedAnswerCommitted,
)
from agent_workbench.ports.conversation_store import (
    AuthorizedRevision,
    ChatTurnConflictError,
    ChatTurnResult,
    StoredChatTurn,
    chat_turn_terminal_event_key,
)
from agent_workbench.ports.event_log import EventScope, EventSink


class PostgresChatReleaseCoordinator:
    """Close the Chat authorization-to-publication race in one transaction."""

    __slots__ = ("_conversations", "_engine", "_events")

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conversations = PostgresConversationStore(engine)
        self._events = PostgresEventLog(engine)

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
        """Publish the pending answer or a scrubbed refusal at the lock fence."""

        # PostgreSQL publishes directly through the transaction-aware event
        # helper. The API's SSE path polls the durable log, so invoking ``sink``
        # here would create a second transaction and reopen the very race this
        # coordinator exists to close.
        del sink

        if run_id != turn.run_id:
            raise ChatTurnConflictError(
                "the answer release run does not belong to the chat turn"
            )
        if stream_id != turn.session_id:
            raise ChatTurnConflictError(
                "a chat answer stream must be its conversation session"
            )
        scope = EventScope(stream_id=stream_id, run_id=run_id)

        async with self._engine.begin() as connection:
            # Lock ordering is stable across every release: session, Turn,
            # sorted documents, event stream. Document writers take one
            # document row before changing content, revision or ACL, so their
            # commit is totally ordered with this authorization fence.
            stored = await self._conversations.lock_release_turn_in_transaction(
                connection,
                session_id=turn.session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                turn_id=turn.turn_id,
            )
            self._require_same_turn(stored, turn=turn, run_id=run_id)

            if stored.status in {"committed", "withheld"}:
                # A terminal Turn was produced atomically with its event. A
                # retry therefore returns the fact without re-authorizing a
                # publication that already happened.
                return stored
            if stored.status != "release_pending" or stored.result is None:
                raise ChatTurnConflictError("chat turn is not ready for release")

            authorized = await self._revisions_unchanged(
                connection,
                stored.result.authorized_revisions,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
            await self._after_authorization_locked()

            withheld_result = (
                None
                if authorized
                else self._withheld_result(
                    stored.result,
                    refusal_text=refusal_text,
                )
            )
            result = stored.result if withheld_result is None else withheld_result
            # Three terminal events, chosen from facts on the stored result
            # rather than inferred. `grounded` is read instead of "are there
            # citations", because a retrieval turn that cited nothing and a
            # turn that never retrieved are exactly the two states an auditor
            # must be able to tell apart (ADR-018).
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
            await self._events.append_durable_in_transaction(
                connection,
                scope,
                payload,
                event_key=chat_turn_terminal_event_key(stored.turn_id),
            )
            return await self._conversations.mark_released_in_transaction(
                connection,
                session_id=stored.session_id,
                turn_id=stored.turn_id,
                withheld_result=withheld_result,
            )

    async def _revisions_unchanged(
        self,
        connection: AsyncConnection,
        revisions: tuple[AuthorizedRevision, ...],
        *,
        tenant_id: str,
        principal_id: str,
    ) -> bool:
        """Lock every cited document and validate its exact readable revision."""

        if not revisions:
            return True

        document_ids = tuple(revision.document_id for revision in revisions)
        rows = (
            (
                await connection.execute(
                    select(
                        documents.c.document_id,
                        documents.c.tenant_id,
                        documents.c.owner_id,
                        documents.c.source_revision,
                        documents.c.deleted,
                    )
                    .where(documents.c.document_id.in_(document_ids))
                    .order_by(documents.c.document_id)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        by_id = {cast(str, row["document_id"]): row for row in rows}

        grants = frozenset(
            cast(str, row.document_id)
            for row in (
                await connection.execute(
                    select(document_acl.c.document_id)
                    .where(document_acl.c.document_id.in_(document_ids))
                    .where(document_acl.c.principal_id == principal_id)
                )
            ).all()
        )
        return all(
            (row := by_id.get(revision.document_id)) is not None
            and row["tenant_id"] == tenant_id
            and not cast(bool, row["deleted"])
            and cast(int, row["source_revision"]) == revision.source_revision
            and (row["owner_id"] == principal_id or revision.document_id in grants)
            for revision in revisions
        )

    async def _after_authorization_locked(self) -> None:
        """Test seam reached after authorization while every source is locked."""

    @staticmethod
    def _withheld_result(
        pending: ChatTurnResult,
        *,
        refusal_text: str,
    ) -> ChatTurnResult:
        """Remove every trace of a candidate that failed the final fence."""

        return ChatTurnResult(
            outcome=pending.outcome.model_copy(
                update={
                    "output_text": "",
                    "output_ref": None,
                    "citations": (),
                }
            ),
            answer=refusal_text,
            authorized_revisions=(),
            citations=(),
            withheld=True,
        )

    @staticmethod
    def _require_same_turn(
        stored: StoredChatTurn,
        *,
        turn: StoredChatTurn,
        run_id: str,
    ) -> None:
        if stored.run_id != run_id or stored.session_id != turn.session_id:
            raise ChatTurnConflictError(
                "the stored chat turn does not match the release scope"
            )
        if stored.status == "release_pending" and stored.result != turn.result:
            raise ChatTurnConflictError(
                "the chat turn release candidate changed before publication"
            )


__all__ = ["PostgresChatReleaseCoordinator"]
