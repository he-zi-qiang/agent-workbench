"""Chat sessions, messages and idempotent turn facts in PostgreSQL.

Every mutation locks the owning conversation row. That one lock establishes
both message ordering and the non-interleaving turn lifecycle: checking a
request key, snapshotting history, appending a message and moving a turn are
one transaction, not a sequence of independently durable guesses.

The database has matching unique constraints as a last line of defence. A
future writer that bypasses this repository collides instead of silently
creating two active turns or two uses of an idempotency key.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.models import (
    chat_turns,
    conversation_sessions,
)
from agent_workbench.adapters.persistence.models import messages as messages_table
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import new_id, new_message_id
from agent_workbench.domain.messages import Message, assistant_message
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.ports.conversation_store import (
    ChatTurnBusyError,
    ChatTurnClaim,
    ChatTurnConflictError,
    ChatTurnResult,
    ConversationSession,
    StoredChatTurn,
    StoredMessage,
)


class PostgresConversationStore:
    """Persistent conversations, owner-scoped on every query and mutation."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        owner_id: str,
        title: str | None = None,
    ) -> ConversationSession:
        async with self._engine.begin() as connection:
            try:
                await connection.execute(
                    insert(conversation_sessions).values(
                        session_id=session_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        title=title,
                    )
                )
            except IntegrityError as exc:
                # A reused id is a caller mistake, not a race to retry: the
                # second caller would otherwise write into the first one's
                # conversation.
                raise ValueError(f"session {session_id} already exists") from exc

        return ConversationSession(
            session_id=session_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            title=title,
        )

    async def append(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        messages: tuple[Message, ...],
    ) -> tuple[StoredMessage, ...]:
        async with self._engine.begin() as connection:
            # Authenticate even an empty append. Otherwise an empty tuple is a
            # side channel that answers differently from every other method.
            await self._locked_session(connection, session_id, tenant_id, principal_id)
            return await self._append_messages(
                connection,
                session_id=session_id,
                messages=messages,
            )

    async def history(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        limit: int | None = None,
    ) -> tuple[StoredMessage, ...]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")

        async with self._engine.connect() as connection:
            await self._require_session(connection, session_id, tenant_id, principal_id)
            return await self._history(connection, session_id=session_id, limit=limit)

    async def claim_turn(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        run_id: str,
        user_message: Message,
    ) -> ChatTurnClaim:
        """Atomically authenticate, deduplicate, snapshot and append the user."""

        try:
            async with self._engine.begin() as connection:
                # Authorization deliberately precedes the idempotency query. A
                # key must never reveal that another principal's turn exists.
                await self._locked_session(
                    connection,
                    session_id,
                    tenant_id,
                    principal_id,
                )

                existing = await self._turn_for_key(
                    connection,
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                )
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise ChatTurnConflictError("chat turn idempotency conflict")
                    return ChatTurnClaim(
                        turn=existing,
                        history_before=await self._history_before(
                            connection,
                            existing,
                        ),
                        newly_claimed=False,
                    )

                if await self._has_active_turn(connection, session_id=session_id):
                    raise ChatTurnBusyError(
                        "conversation already has an unfinished chat turn"
                    )
                if user_message.role != "user":
                    raise ValueError("claim_turn requires a user message")
                if await self._run_id_exists(connection, run_id=run_id):
                    raise ChatTurnConflictError("chat turn run id conflict")

                history_before = await self._history(
                    connection,
                    session_id=session_id,
                )
                stored_user = (
                    await self._append_messages(
                        connection,
                        session_id=session_id,
                        messages=(user_message,),
                    )
                )[0]
                turn = StoredChatTurn(
                    turn_id=new_id("turn"),
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    run_id=run_id,
                    status="running",
                    user_message_id=stored_user.message_id,
                )
                await connection.execute(
                    insert(chat_turns).values(
                        turn_id=turn.turn_id,
                        session_id=turn.session_id,
                        idempotency_key=turn.idempotency_key,
                        request_hash=turn.request_hash,
                        run_id=turn.run_id,
                        status=turn.status,
                        user_message_id=turn.user_message_id,
                    )
                )
                return ChatTurnClaim(
                    turn=turn,
                    history_before=history_before,
                    newly_claimed=True,
                )
        except IntegrityError as exc:
            # The session lock serialises claims inside one conversation. The
            # remaining realistic race is the globally unique run id being
            # claimed in another session between our check and insert.
            raise ChatTurnConflictError("chat turn claim conflict") from exc

    async def prepare_release(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        result: ChatTurnResult,
    ) -> StoredChatTurn:
        """Persist a release candidate without making it conversation history."""

        async with self._engine.begin() as connection:
            await self._locked_session(connection, session_id, tenant_id, principal_id)
            turn = await self._locked_turn(
                connection,
                session_id=session_id,
                turn_id=turn_id,
            )
            if turn.status in {"release_pending", "committed", "withheld"}:
                if turn.result == result:
                    return turn
                raise ChatTurnConflictError("chat turn release result conflict")
            if turn.status != "running":
                raise ChatTurnConflictError(
                    "chat turn cannot prepare release from its current state"
                )

            # Constructing through Pydantic first proves the result belongs to
            # this run before either JSONB or lifecycle state is changed.
            prepared = self._updated_turn(
                turn,
                status="release_pending",
                result=result,
            )
            await self._write_turn(connection, prepared)
            return prepared

    async def mark_released(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        withheld_result: ChatTurnResult | None = None,
    ) -> StoredChatTurn:
        """Append the visible assistant only after publication has succeeded."""

        async with self._engine.begin() as connection:
            await self._locked_session(connection, session_id, tenant_id, principal_id)
            return await self.mark_released_in_transaction(
                connection,
                session_id=session_id,
                turn_id=turn_id,
                withheld_result=withheld_result,
            )

    async def mark_released_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        session_id: str,
        turn_id: str,
        withheld_result: ChatTurnResult | None = None,
    ) -> StoredChatTurn:
        """Release a turn using a transaction whose session is already locked.

        Only the PostgreSQL release coordinator should normally use this
        method. It exists so the answer event and assistant history row share
        the authorization-fence transaction; callers using the ordinary port
        method still get their own authenticated transaction above.
        """

        turn = await self._locked_turn(
            connection,
            session_id=session_id,
            turn_id=turn_id,
        )
        if turn.status in {"committed", "withheld"}:
            if withheld_result is not None and turn.result != withheld_result:
                raise ChatTurnConflictError(
                    "chat turn withheld result conflicts with its terminal fact"
                )
            return turn
        if turn.status != "release_pending" or turn.result is None:
            raise ChatTurnConflictError(
                "chat turn cannot be released from its current state"
            )
        result = (
            self._validated_withheld_result(turn, withheld_result)
            if withheld_result is not None
            else turn.result
        )

        stored_assistant = (
            await self._append_messages(
                connection,
                session_id=session_id,
                messages=(assistant_message(text=result.answer),),
            )
        )[0]
        released = self._updated_turn(
            turn,
            status="withheld" if result.withheld else "committed",
            assistant_message_id=stored_assistant.message_id,
            result=result,
        )
        await self._write_turn(connection, released)
        return released

    async def lock_release_turn_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
    ) -> StoredChatTurn:
        """Authenticate and lock the session and Turn for atomic publication."""

        await self._locked_session(
            connection,
            session_id,
            tenant_id,
            principal_id,
        )
        return await self._locked_turn(
            connection,
            session_id=session_id,
            turn_id=turn_id,
        )

    @staticmethod
    def _validated_withheld_result(
        turn: StoredChatTurn,
        result: ChatTurnResult,
    ) -> ChatTurnResult:
        if not result.withheld:
            raise ChatTurnConflictError(
                "a release result override must be a safe withheld result"
            )
        if result.outcome.agent_run_id != turn.run_id:
            raise ChatTurnConflictError(
                "a withheld result must belong to the chat turn's run"
            )
        return result

    async def finish_failed(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        outcome: AgentOutcome,
    ) -> StoredChatTurn:
        """Finish a failed/cancelled run without an assistant history row."""

        async with self._engine.begin() as connection:
            await self._locked_session(connection, session_id, tenant_id, principal_id)
            turn = await self._locked_turn(
                connection,
                session_id=session_id,
                turn_id=turn_id,
            )
            if outcome.status not in {"failed", "cancelled"}:
                raise ValueError("finish_failed requires a failed or cancelled outcome")
            if turn.status in {"failed", "cancelled"}:
                if turn.failure_outcome == outcome:
                    return turn
                raise ChatTurnConflictError("chat turn failure outcome conflict")
            if turn.status != "running":
                raise ChatTurnConflictError(
                    "chat turn cannot fail from its current state"
                )

            failed = self._updated_turn(
                turn,
                status=outcome.status,
                failure_outcome=outcome,
            )
            await self._write_turn(connection, failed)
            return failed

    async def _append_messages(
        self,
        connection: AsyncConnection,
        *,
        session_id: str,
        messages: tuple[Message, ...],
    ) -> tuple[StoredMessage, ...]:
        if not messages:
            return ()

        next_sequence = await self._next_sequence(connection, session_id)
        stored = tuple(
            StoredMessage(
                message_id=new_message_id(),
                session_id=session_id,
                sequence=next_sequence + offset,
                message=message,
            )
            for offset, message in enumerate(messages)
        )
        await connection.execute(
            insert(messages_table),
            [
                {
                    "message_id": record.message_id,
                    "session_id": session_id,
                    "sequence": record.sequence,
                    "payload": record.message.model_dump(mode="json"),
                }
                for record in stored
            ],
        )
        return stored

    async def _history(
        self,
        connection: AsyncConnection,
        *,
        session_id: str,
        limit: int | None = None,
        before_sequence: int | None = None,
    ) -> tuple[StoredMessage, ...]:
        query = (
            select(
                messages_table.c.message_id,
                messages_table.c.sequence,
                messages_table.c.payload,
            )
            .where(messages_table.c.session_id == session_id)
            .order_by(messages_table.c.sequence)
        )
        if before_sequence is not None:
            query = query.where(messages_table.c.sequence < before_sequence)
        if limit is not None:
            query = query.limit(limit)
        rows = (await connection.execute(query)).all()
        return tuple(
            StoredMessage(
                message_id=cast(str, row.message_id),
                session_id=session_id,
                sequence=cast(int, row.sequence),
                message=Message.model_validate(row.payload),
            )
            for row in rows
        )

    async def _history_before(
        self,
        connection: AsyncConnection,
        turn: StoredChatTurn,
    ) -> tuple[StoredMessage, ...]:
        sequence = (
            await connection.execute(
                select(messages_table.c.sequence)
                .where(messages_table.c.session_id == turn.session_id)
                .where(messages_table.c.message_id == turn.user_message_id)
            )
        ).scalar_one_or_none()
        if sequence is None:
            raise ChatTurnConflictError("chat turn user message is missing")
        return await self._history(
            connection,
            session_id=turn.session_id,
            before_sequence=cast(int, sequence),
        )

    async def _turn_for_key(
        self,
        connection: AsyncConnection,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> StoredChatTurn | None:
        row = (
            (
                await connection.execute(
                    select(chat_turns)
                    .where(chat_turns.c.session_id == session_id)
                    .where(chat_turns.c.idempotency_key == idempotency_key)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        return None if row is None else self._turn_from_row(row)

    async def _locked_turn(
        self,
        connection: AsyncConnection,
        *,
        session_id: str,
        turn_id: str,
    ) -> StoredChatTurn:
        row = (
            (
                await connection.execute(
                    select(chat_turns)
                    .where(chat_turns.c.session_id == session_id)
                    .where(chat_turns.c.turn_id == turn_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFoundError("chat turn not found")
        return self._turn_from_row(row)

    async def _has_active_turn(
        self,
        connection: AsyncConnection,
        *,
        session_id: str,
    ) -> bool:
        result = await connection.execute(
            select(chat_turns.c.turn_id)
            .where(chat_turns.c.session_id == session_id)
            .where(chat_turns.c.status.in_(("running", "release_pending")))
            .limit(1)
        )
        return result.first() is not None

    async def _run_id_exists(
        self,
        connection: AsyncConnection,
        *,
        run_id: str,
    ) -> bool:
        result = await connection.execute(
            select(chat_turns.c.turn_id).where(chat_turns.c.run_id == run_id).limit(1)
        )
        return result.first() is not None

    async def _write_turn(
        self,
        connection: AsyncConnection,
        turn: StoredChatTurn,
    ) -> None:
        await connection.execute(
            update(chat_turns)
            .where(chat_turns.c.turn_id == turn.turn_id)
            .values(
                status=turn.status,
                assistant_message_id=turn.assistant_message_id,
                result=(
                    None if turn.result is None else turn.result.model_dump(mode="json")
                ),
                failure_outcome=(
                    None
                    if turn.failure_outcome is None
                    else turn.failure_outcome.model_dump(mode="json")
                ),
                updated_at=func.now(),
            )
        )

    @staticmethod
    def _turn_from_row(row: RowMapping) -> StoredChatTurn:
        """Strictly reconstruct nested JSONB through its Pydantic contracts."""

        raw_result = row["result"]
        raw_failure = row["failure_outcome"]
        return StoredChatTurn.model_validate(
            {
                "turn_id": row["turn_id"],
                "session_id": row["session_id"],
                "idempotency_key": row["idempotency_key"],
                "request_hash": row["request_hash"],
                "run_id": row["run_id"],
                "status": row["status"],
                "user_message_id": row["user_message_id"],
                "assistant_message_id": row["assistant_message_id"],
                "result": (
                    None
                    if raw_result is None
                    else ChatTurnResult.model_validate(raw_result)
                ),
                "failure_outcome": (
                    None
                    if raw_failure is None
                    else AgentOutcome.model_validate(raw_failure)
                ),
            }
        )

    @staticmethod
    def _updated_turn(
        turn: StoredChatTurn,
        **updates: Any,
    ) -> StoredChatTurn:
        payload = turn.model_dump(mode="python")
        payload.update(updates)
        return StoredChatTurn.model_validate(payload)

    async def _locked_session(
        self,
        connection: AsyncConnection,
        session_id: str,
        tenant_id: str,
        principal_id: str,
    ) -> None:
        """Authenticate and lock the session, serialising all its mutations."""

        result = await connection.execute(
            select(conversation_sessions.c.session_id)
            .where(conversation_sessions.c.session_id == session_id)
            .where(conversation_sessions.c.tenant_id == tenant_id)
            .where(conversation_sessions.c.owner_id == principal_id)
            .with_for_update()
        )
        if result.first() is None:
            raise NotFoundError("conversation session not found")

    async def _require_session(
        self,
        connection: AsyncConnection,
        session_id: str,
        tenant_id: str,
        principal_id: str,
    ) -> None:
        result = await connection.execute(
            select(conversation_sessions.c.session_id)
            .where(conversation_sessions.c.session_id == session_id)
            .where(conversation_sessions.c.tenant_id == tenant_id)
            .where(conversation_sessions.c.owner_id == principal_id)
        )
        if result.first() is None:
            # A wrong tenant, principal and missing id intentionally answer the
            # same way; distinguishing them confirms another session exists.
            raise NotFoundError("conversation session not found")

    async def _next_sequence(
        self,
        connection: AsyncConnection,
        session_id: str,
    ) -> int:
        result = await connection.execute(
            select(func.coalesce(func.max(messages_table.c.sequence), 0)).where(
                messages_table.c.session_id == session_id
            )
        )
        return cast(int, result.scalar_one()) + 1


__all__ = ["PostgresConversationStore"]
