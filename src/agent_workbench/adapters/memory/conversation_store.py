"""In-memory conversation store.

Sequences are assigned per session and never reused, so a later PostgreSQL
implementation can keep the same ordering guarantee with a unique constraint
instead of a lock. Tenant scoping is enforced on every operation, including the
append path: a session that cannot be read must not be writable either.
"""

from __future__ import annotations

import asyncio

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import new_message_id
from agent_workbench.domain.messages import Message
from agent_workbench.ports.conversation_store import ConversationSession, StoredMessage


class InMemoryConversationStore:
    """Chat sessions and their messages, held in process memory."""

    def __init__(self) -> None:
        self._sessions: dict[str, ConversationSession] = {}
        self._messages: dict[str, list[StoredMessage]] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        owner_id: str,
        title: str | None = None,
    ) -> ConversationSession:
        session = ConversationSession(
            session_id=session_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            title=title,
        )
        async with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"session already exists: {session_id}")
            self._sessions[session_id] = session
            self._messages[session_id] = []
        return session

    async def append(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        messages: tuple[Message, ...],
    ) -> tuple[StoredMessage, ...]:
        async with self._lock:
            self._require_session(
                session_id=session_id, tenant_id=tenant_id, principal_id=principal_id
            )
            stored = self._messages[session_id]
            appended = tuple(
                StoredMessage(
                    message_id=new_message_id(),
                    session_id=session_id,
                    sequence=len(stored) + offset + 1,
                    message=message,
                )
                for offset, message in enumerate(messages)
            )
            stored.extend(appended)
        return appended

    async def history(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        limit: int | None = None,
    ) -> tuple[StoredMessage, ...]:
        async with self._lock:
            self._require_session(
                session_id=session_id, tenant_id=tenant_id, principal_id=principal_id
            )
            stored = tuple(self._messages[session_id])
        return stored if limit is None else stored[:limit]

    def _require_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
    ) -> ConversationSession:
        session = self._sessions.get(session_id)
        # A wrong tenant, a wrong principal and a missing session answer
        # identically: any difference would confirm somebody else's exists.
        if (
            session is None
            or session.tenant_id != tenant_id
            or session.owner_id != principal_id
        ):
            raise NotFoundError("conversation session not found")
        return session


__all__ = ["InMemoryConversationStore"]
