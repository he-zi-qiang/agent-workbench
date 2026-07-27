"""The conversation boundary.

Chat history has one owner, and it is not the event log. Events record what was
observed; this store records what was said, which is the only thing replayed
into a later model call. Keeping them apart is why a redacted or compacted
context never rewrites the audit trail, and why an event retention policy can
never silently truncate a conversation.

Every method takes the tenant explicitly. A repository that infers the tenant
from ambient state is one refactor away from returning another tenant's rows.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.messages import Message
from agent_workbench.domain.schema import ShortText, VersionedModel


class ConversationSession(VersionedModel):
    """A multi-turn chat, owned by exactly one principal in one tenant."""

    session_id: Identifier
    tenant_id: Identifier
    owner_id: Identifier
    title: ShortText | None = None


class StoredMessage(VersionedModel):
    """A message with its position in the session."""

    message_id: Identifier
    session_id: Identifier
    sequence: int = Field(ge=1)
    message: Message


@runtime_checkable
class ConversationStore(Protocol):
    """Persistent chat sessions and their messages."""

    async def create_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        owner_id: str,
        title: str | None = None,
    ) -> ConversationSession: ...

    async def append(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        messages: tuple[Message, ...],
    ) -> tuple[StoredMessage, ...]:
        """Append a turn and return the stored messages with their positions.

        A session answers to the principal that created it. Raises
        ``NotFoundError`` for an unknown id, another tenant's, and another
        principal's alike -- appending to somebody else's conversation puts
        words in it that they will read back as their own history.
        """
        ...

    async def history(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        limit: int | None = None,
    ) -> tuple[StoredMessage, ...]:
        """Messages in sequence order, oldest first, for their owner only.

        A conversation is the most personal thing this system stores. Scoping
        it to a tenant says whose database it is, not whose conversation it
        is, and a session id travels through URLs and logs like any other.
        """
        ...


__all__ = [
    "ConversationSession",
    "ConversationStore",
    "StoredMessage",
]
