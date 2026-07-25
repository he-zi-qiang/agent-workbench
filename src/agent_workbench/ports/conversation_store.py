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
        messages: tuple[Message, ...],
    ) -> tuple[StoredMessage, ...]:
        """Append a turn and return the stored messages with their positions.

        Raises ``NotFoundError`` when the session does not exist for this
        tenant, so a wrong tenant cannot append to a session it cannot see.
        """
        ...

    async def history(
        self,
        *,
        session_id: str,
        tenant_id: str,
        limit: int | None = None,
    ) -> tuple[StoredMessage, ...]:
        """Return messages in sequence order, oldest first."""
        ...


__all__ = [
    "ConversationSession",
    "ConversationStore",
    "StoredMessage",
]
