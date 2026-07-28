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

from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field, StringConstraints, model_validator

from agent_workbench.domain.context import Citation
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.messages import Message
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.domain.schema import BoundedText, ShortText, VersionedModel

IdempotencyKey = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:+=@/-]{0,127}$",
    ),
]
RequestHash = Annotated[
    str,
    StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
ChatTurnStatus = Literal[
    "running",
    "release_pending",
    "committed",
    "withheld",
    "failed",
    "cancelled",
]


class ChatTurnConflictError(RuntimeError):
    """An idempotency key or state transition disagrees with stored fact."""


class ChatTurnBusyError(RuntimeError):
    """Another unfinished turn already owns this conversation session."""


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


class ChatTurnResult(VersionedModel):
    """The completed model outcome and the answer awaiting release."""

    outcome: AgentOutcome
    answer: BoundedText
    citations: tuple[Citation, ...] = ()
    withheld: bool = False

    @model_validator(mode="after")
    def validate_release_candidate(self) -> ChatTurnResult:
        if self.outcome.status != "completed":
            raise ValueError("a release candidate requires a completed outcome")
        if self.withheld:
            if (
                self.citations
                or self.outcome.output_text
                or self.outcome.output_ref is not None
                or self.outcome.citations
            ):
                raise ValueError(
                    "a withheld result must not retain model output or citations"
                )
        elif self.answer != self.outcome.output_text:
            raise ValueError("a committed answer must match the completed outcome")
        return self


class StoredChatTurn(VersionedModel):
    """One idempotent chat turn and its durable lifecycle state."""

    turn_id: Identifier
    session_id: Identifier
    idempotency_key: IdempotencyKey
    request_hash: RequestHash
    run_id: Identifier
    status: ChatTurnStatus
    user_message_id: Identifier
    assistant_message_id: Identifier | None = None
    result: ChatTurnResult | None = None
    failure_outcome: AgentOutcome | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> StoredChatTurn:
        if self.status == "running":
            if (
                self.assistant_message_id is not None
                or self.result is not None
                or self.failure_outcome is not None
            ):
                raise ValueError("a running turn contains only its user message")
            return self

        if self.status == "release_pending":
            if (
                self.assistant_message_id is not None
                or self.result is None
                or self.failure_outcome is not None
            ):
                raise ValueError(
                    "a release-pending turn stores a result but exposes no assistant"
                )
            if self.result.outcome.agent_run_id != self.run_id:
                raise ValueError("a release result must belong to the turn's run")
            return self

        if self.status in {"committed", "withheld"}:
            if (
                self.assistant_message_id is None
                or self.result is None
                or self.failure_outcome is not None
            ):
                raise ValueError("a released turn requires one assistant result")
            if self.result.outcome.agent_run_id != self.run_id:
                raise ValueError("a release result must belong to the turn's run")
            if self.status == "committed" and self.result.withheld:
                raise ValueError("a committed turn cannot contain a withheld result")
            if self.status == "withheld" and not self.result.withheld:
                raise ValueError("a withheld turn requires a withheld result")
            return self

        if (
            self.assistant_message_id is not None
            or self.result is not None
            or self.failure_outcome is None
            or self.failure_outcome.status != self.status
            or self.failure_outcome.agent_run_id != self.run_id
        ):
            raise ValueError(
                "a failed or cancelled turn contains only its matching outcome"
            )
        return self


class ChatTurnClaim(VersionedModel):
    """The atomically claimed turn and its pre-question history snapshot."""

    turn: StoredChatTurn
    history_before: tuple[StoredMessage, ...] = ()
    newly_claimed: bool

    @model_validator(mode="after")
    def validate_history_scope(self) -> ChatTurnClaim:
        if any(
            stored.session_id != self.turn.session_id for stored in self.history_before
        ):
            raise ValueError("claim history must belong to the claimed session")
        return self


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


@runtime_checkable
class ChatTurnStore(ConversationStore, Protocol):
    """Conversation store with an atomic, idempotent chat-turn ledger.

    This extends the legacy message boundary separately while PostgreSQL gains
    the new tables and transitions. Callers that need turn semantics depend on
    this protocol; callers that only need message history remain compatible
    with the smaller ``ConversationStore`` contract.
    """

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
        """Claim one turn, snapshot history, and append its user exactly once.

        The ownership check precedes idempotency lookup. Reusing a key with the
        same request hash returns the original turn; a different hash raises
        ``ChatTurnConflictError``. Another active key in the same session
        raises ``ChatTurnBusyError``.
        """
        ...

    async def prepare_release(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        result: ChatTurnResult,
    ) -> StoredChatTurn:
        """Store a checked result without exposing an assistant message yet."""
        ...

    async def mark_released(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
    ) -> StoredChatTurn:
        """Append one assistant and commit/withhold the prepared result."""
        ...

    async def finish_failed(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        outcome: AgentOutcome,
    ) -> StoredChatTurn:
        """Finish a running turn as failed or cancelled, with no assistant."""
        ...


__all__ = [
    "ChatTurnBusyError",
    "ChatTurnClaim",
    "ChatTurnConflictError",
    "ChatTurnResult",
    "ChatTurnStatus",
    "ChatTurnStore",
    "ConversationSession",
    "ConversationStore",
    "IdempotencyKey",
    "RequestHash",
    "StoredChatTurn",
    "StoredMessage",
]
