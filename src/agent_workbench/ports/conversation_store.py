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

from hashlib import sha256
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from agent_workbench.domain.context import Citation
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.messages import Message
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.domain.schema import (
    AnswerText,
    DomainModel,
    ShortText,
    VersionedModel,
)
from agent_workbench.ports.event_log import EventKey

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


class ChatTurnLeaseExpiredError(RuntimeError):
    """A late result reached a Turn whose fixed execution lease has elapsed."""

    def __init__(self, outcome: AgentOutcome) -> None:
        if (
            outcome.status != "failed"
            or outcome.stop_reason != "deadline"
            or outcome.error is None
            or outcome.error.code != "stale_execution"
            or outcome.error.retryable
        ):
            raise ValueError("lease expiry requires a stale_execution outcome")
        self.outcome = outcome
        super().__init__("chat turn execution lease expired")


def chat_turn_terminal_event_key(turn_id: str) -> EventKey:
    """Return the bounded idempotency key shared by all Chat terminal events."""

    digest = sha256(turn_id.encode("utf-8")).hexdigest()
    return f"chat-turn:{digest}:terminal"


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


class AuthorizedRevision(DomainModel):
    """One document revision on which a pending answer is allowed to rely."""

    document_id: Identifier
    source_revision: int = Field(ge=1)


class ChatTurnResult(VersionedModel):
    """The completed model outcome and the answer awaiting release."""

    outcome: AgentOutcome
    answer: AnswerText
    authorized_revisions: tuple[AuthorizedRevision, ...]
    citations: tuple[Citation, ...] = ()
    withheld: bool = False
    #: Whether this answer was built on retrieved evidence (ADR-018).
    #:
    #: Defaults to ``True`` because every row written before the ungrounded
    #: shape existed came from a path that always retrieved. A default of
    #: ``False`` would silently relabel the entire history as unverified, which
    #: is the more damaging direction to be wrong in.
    #:
    #: This is what the release coordinator reads to choose between
    #: ``AnswerCommitted`` and ``UngroundedAnswerCommitted``. Inferring it from
    #: an empty citation list instead would conflate "retrieved and cited
    #: nothing" with "never retrieved", and those are the two states the whole
    #: distinction exists to keep apart.
    grounded: bool = True

    @model_validator(mode="after")
    def validate_release_candidate(self) -> ChatTurnResult:
        if self.outcome.status != "completed":
            raise ValueError("a release candidate requires a completed outcome")
        revision_ids = tuple(
            revision.document_id for revision in self.authorized_revisions
        )
        if revision_ids != tuple(sorted(set(revision_ids))):
            raise ValueError("authorized revisions must be unique and sorted")
        if not self.grounded and (self.citations or self.authorized_revisions):
            # An ungrounded turn never reached retrieval, so there is nothing
            # for either of these to have come from. Rejecting here rather than
            # trusting the caller matters because both fields are load bearing
            # downstream in opposite directions: citations would present
            # invented sources with this system's authority, and revisions
            # would send the release fence to re-check documents this answer
            # never read -- which can only fail, withholding an answer for a
            # source that was never involved.
            raise ValueError(
                "an ungrounded result must carry no citations and no "
                "authorized revisions"
            )
        if self.withheld:
            if (
                self.citations
                or self.authorized_revisions
                or self.outcome.output_text
                or self.outcome.output_ref is not None
                or self.outcome.citations
            ):
                raise ValueError(
                    "a withheld result must not retain model output or citations"
                )
        elif self.answer != self.outcome.output_text:
            raise ValueError("a committed answer must match the completed outcome")
        elif not {citation.document_id for citation in self.citations} <= set(
            revision_ids
        ):
            # A subset, not an equality. These were equal while citations *were*
            # the retrieval packet -- every retrieved document was reported as a
            # source whether or not the answer used it. Now a citation is
            # offered only when the answer named it, so the fence is legitimately
            # wider than the sources: a passage the model read and did not cite
            # is still a passage whose permission must hold.
            #
            # The containment is the half that matters and it is unchanged. A
            # citation outside the fenced set is a source nobody checked the
            # asker may still read.
            raise ValueError("every cited document must be an authorized revision")
        return self


class StoredChatTurn(VersionedModel):
    """One idempotent chat turn and its durable lifecycle state."""

    turn_id: Identifier
    session_id: Identifier
    idempotency_key: IdempotencyKey
    request_hash: RequestHash
    run_id: Identifier
    status: ChatTurnStatus
    # A fixed execution lease, not a renewable worker claim. Expiry closes the
    # Turn as failed; it never transfers this execution to another process.
    lease_until: AwareDatetime | None = None
    user_message_id: Identifier
    assistant_message_id: Identifier | None = None
    result: ChatTurnResult | None = None
    failure_outcome: AgentOutcome | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> StoredChatTurn:
        if self.status == "running":
            if (
                self.lease_until is None
                or self.assistant_message_id is not None
                or self.result is not None
                or self.failure_outcome is not None
            ):
                raise ValueError(
                    "a running turn requires its user message and execution lease"
                )
            return self

        if self.lease_until is not None:
            raise ValueError("only a running turn may retain an execution lease")

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


class PendingChatRelease(VersionedModel):
    """One prepared Turn plus the session-owner scope needed to release it."""

    turn: StoredChatTurn
    tenant_id: Identifier
    principal_id: Identifier

    @model_validator(mode="after")
    def validate_pending_turn(self) -> PendingChatRelease:
        if self.turn.status != "release_pending":
            raise ValueError("a pending Chat release requires release_pending status")
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
        lease_seconds: int,
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
        withheld_result: ChatTurnResult | None = None,
    ) -> StoredChatTurn:
        """Append one assistant and commit/withhold the prepared result.

        ``withheld_result`` is the only allowed override: an atomic release
        fence uses it when the pending candidate's evidence changed. It must be
        a scrubbed withheld result for the same run; callers cannot replace one
        publishable answer with another.
        """
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
        """Finish a live running turn as failed/cancelled, with no assistant.

        An elapsed execution lease raises ``ChatTurnLeaseExpiredError`` without
        changing the Turn. Only ``ChatExpirationCoordinator`` may persist that
        terminal fact together with its durable event.
        """
        ...

    async def finish_running_if_current(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        outcome: AgentOutcome,
    ) -> StoredChatTurn:
        """Best-effort cleanup that never overwrites pending or terminal facts.

        Returns the durable Turn observed under the transition lock. It
        contains the supplied failure only when this call found a live
        ``running`` Turn; an elapsed lease raises
        ``ChatTurnLeaseExpiredError`` without writing. Otherwise it exposes the
        pending or terminal fact that won the race. Request cancellation and
        exception cleanup use this weaker transition because an atomic release
        may have committed concurrently.
        """
        ...

    async def list_release_pending(
        self,
        *,
        limit: int,
    ) -> tuple[PendingChatRelease, ...]:
        """List prepared Turns with the session-owner scope needed to resume."""
        ...


__all__ = [
    "AuthorizedRevision",
    "ChatTurnBusyError",
    "ChatTurnClaim",
    "ChatTurnConflictError",
    "ChatTurnLeaseExpiredError",
    "ChatTurnResult",
    "ChatTurnStatus",
    "ChatTurnStore",
    "ConversationSession",
    "ConversationStore",
    "IdempotencyKey",
    "PendingChatRelease",
    "RequestHash",
    "StoredChatTurn",
    "StoredMessage",
    "chat_turn_terminal_event_key",
]
