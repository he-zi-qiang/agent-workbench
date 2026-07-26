"""Chat sessions and their messages, in PostgreSQL.

The in-memory store made the contract executable; this one has to keep it under
concurrency, and the two places that differ are both about ordering.

Positions are assigned while the session row is locked. Two appends to the same
session serialize behind that lock, which is what makes ``sequence`` gap-free
rather than merely unique. The database also holds a unique constraint on
``(session_id, sequence)``: if the lock is ever bypassed, the write fails
instead of quietly reusing a position.

A message is stored as its serialized domain object, schema version included,
and read back through the same model. A row written by a contract this process
does not know therefore fails closed at the boundary rather than arriving
half-understood in a model's context.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.models import conversation_sessions
from agent_workbench.adapters.persistence.models import messages as messages_table
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import new_message_id
from agent_workbench.domain.messages import Message
from agent_workbench.ports.conversation_store import (
    ConversationSession,
    StoredMessage,
)


class PostgresConversationStore:
    """Persistent chat sessions, scoped by tenant on every query."""

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
        messages: tuple[Message, ...],
    ) -> tuple[StoredMessage, ...]:
        if not messages:
            return ()

        async with self._engine.begin() as connection:
            await self._locked_session(connection, session_id, tenant_id)

            next_sequence = await self._next_sequence(connection, session_id)
            stored: list[StoredMessage] = []
            rows: list[dict[str, Any]] = []
            for offset, message in enumerate(messages):
                record = StoredMessage(
                    message_id=new_message_id(),
                    session_id=session_id,
                    sequence=next_sequence + offset,
                    message=message,
                )
                stored.append(record)
                rows.append(
                    {
                        "message_id": record.message_id,
                        "session_id": session_id,
                        "sequence": record.sequence,
                        "payload": message.model_dump(mode="json"),
                    }
                )

            await connection.execute(insert(messages_table), rows)

        return tuple(stored)

    async def history(
        self,
        *,
        session_id: str,
        tenant_id: str,
        limit: int | None = None,
    ) -> tuple[StoredMessage, ...]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")

        async with self._engine.connect() as connection:
            await self._require_session(connection, session_id, tenant_id)

            query = (
                select(
                    messages_table.c.message_id,
                    messages_table.c.sequence,
                    messages_table.c.payload,
                )
                .where(messages_table.c.session_id == session_id)
                .order_by(messages_table.c.sequence)
            )
            if limit is not None:
                query = query.limit(limit)
            result = await connection.execute(query)
            rows = result.all()

        return tuple(
            StoredMessage(
                message_id=cast(str, row.message_id),
                session_id=session_id,
                sequence=cast(int, row.sequence),
                message=Message.model_validate(row.payload),
            )
            for row in rows
        )

    async def _locked_session(
        self,
        connection: AsyncConnection,
        session_id: str,
        tenant_id: str,
    ) -> None:
        """Take the session row for update, so appends to it serialize."""

        result = await connection.execute(
            select(conversation_sessions.c.session_id)
            .where(conversation_sessions.c.session_id == session_id)
            .where(conversation_sessions.c.tenant_id == tenant_id)
            .with_for_update()
        )
        if result.first() is None:
            raise NotFoundError("conversation session not found")

    async def _require_session(
        self,
        connection: AsyncConnection,
        session_id: str,
        tenant_id: str,
    ) -> None:
        result = await connection.execute(
            select(conversation_sessions.c.session_id)
            .where(conversation_sessions.c.session_id == session_id)
            .where(conversation_sessions.c.tenant_id == tenant_id)
        )
        if result.first() is None:
            # A wrong tenant and a missing session are the same answer: telling
            # them apart would confirm that someone else's session exists.
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
