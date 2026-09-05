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
from agent_workbench.domain.runs import AgentOutcome, BudgetUsage
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
#: Which API a conversation session answers to.
#:
#: Chat and Code share a session's identity and its message history; they do
#: not share a lifecycle. Chat publishes an answer through a turn ledger, and
#: Code writes no turn row at all. So the mode is not a label on a session --
#: it decides which set of operations the session even has.
SessionMode = Literal["chat", "code"]
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


class WorkspacePointerConflictError(RuntimeError):
    """A workspace advance was based on a version the session no longer holds.

    The bytes and the manifest it wrote are already stored; what was refused is
    the session's claim that they follow from the version it read. Losing that
    race is the whole point of writing through: two runs on one session would
    otherwise each publish a manifest naming only its own files, and the loser
    would silently delete the winner's -- not by removing bytes, but by
    replacing the only name that reaches them.
    """


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
    #: Defaults to chat because every session written before this field existed
    #: was created by the Chat API. The other direction would relabel the whole
    #: history as something no Chat request may touch.
    mode: SessionMode = "chat"
    #: The manifest id this session's working set is currently at, or ``None``
    #: for a session that has never written a file.
    #:
    #: A Task carries its version through graph state, so a node that dies
    #: commits nothing and the retry re-reads the entry version. A session has
    #: no graph to carry it: the pointer is the only thing that survives the
    #: run, which is why it lives on the row rather than in whatever object was
    #: executing.
    workspace_version: Identifier | None = None
    #: When something was last said in this session, not when it was made.
    #:
    #: Optional on the model rather than required, because two of the three
    #: things that construct a `ConversationSession` are refusals and lookups
    #: that have no reason to carry it. Where it does matter -- the list -- the
    #: store fills it, and the column behind it is NOT NULL.
    last_activity_at: AwareDatetime | None = None
    #: Which project this session was opened for, or none (ADR-071). A label,
    #: not an authorization fact, and ``None`` is the normal state.
    project_id: Identifier | None = None


class StoredMessage(VersionedModel):
    """A message with its position in the session."""

    message_id: Identifier
    session_id: Identifier
    sequence: int = Field(ge=1)
    message: Message
    #: 这一条助手消息所属的那一轮烧了多少，`None` 表示这个问题在这里问不出答案。
    #:
    #: 三种情况都是 `None`，而它们是同一个答案：用户说的那一条（一条提问没有自己
    #: 的花销）、还没落定的那一轮（终局还没写下）、以及**调用方没有要**这个数——
    #: `history()` 才去 join，构造模型上下文的那条内部路径不去，因为那条路每轮都
    #: 走一遍而上下文构造器根本不读这个字段。
    #:
    #: 用 `None` 而不是零值 `BudgetUsage()`：一轮真的没花 token 和一轮的花销这里
    #: 答不上来，在屏幕上必须长得不一样，而零是个看起来像答案的答案。
    usage: BudgetUsage | None = None
    #: 这条助手消息属于哪一轮。用户那一条、以及早于回合台账的历史行是 `None`。
    #:
    #: 2026-09-04 评审 A 项：实时回合有引用、turn_id 和 grounded，历史投影却只有
    #: role/text/usage，于是刷新之后引用全部消失——恰好削弱这个项目最值得展示的
    #: 那一段。三个字段一起补：`turn_id` 让「点开引用」仍然走
    #: `turns/{turn_id}/citations/{chunk_id}` 那条**每次重新授权**的路（ADR-067），
    #: 历史里的引用不是一份缓存的原文，只是一个还要再问一次的指针。
    turn_id: Identifier | None = None
    #: 那一轮发布时的引用。只有 committed 的回合才有；扣下的、失败的、还没落定
    #: 的都是空——扣下那一轮的结果已经被擦掉，本来就没有东西可给。
    citations: tuple[Citation, ...] = ()
    #: 那一轮是否基于检索到的证据（ADR-018）。不是某一轮产出的、或那一轮没有
    #: committed 的，是 `None`——不是 `False`：`False` 是「没查资料就答了」这个
    #: 需要贴警告的事实，而 `None` 是「这里答不出」。
    grounded: bool | None = None


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
        mode: SessionMode = "chat",
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
        mode: SessionMode | None = None,
    ) -> tuple[StoredMessage, ...]:
        """Messages in sequence order, oldest first, for their owner only.

        A conversation is the most personal thing this system stores. Scoping
        it to a tenant says whose database it is, not whose conversation it
        is, and a session id travels through URLs and logs like any other.

        ``mode`` is the same kind of scope. A caller that names one refuses a
        session of the other mode with ``NotFoundError`` -- the identical
        answer given to another tenant's id, because "exists, wrong mode" is a
        fact about somebody else's session that no asker is owed.
        """
        ...

    async def session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        mode: SessionMode | None = None,
    ) -> ConversationSession:
        """The session row itself, scoped exactly like its history.

        Reading the row is how a new run learns which workspace version it
        continues from. It is a separate method rather than a field on
        ``history`` because a run that has not asked for any messages still
        needs the pointer, and a pointer that cannot be read is a pointer
        nobody can write against.
        """
        ...

    async def list_sessions(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        mode: SessionMode,
        limit: int = 50,
    ) -> tuple[ConversationSession, ...]:
        """This principal's sessions of one kind, most recently spoken in first.

        ``mode`` is required and has no default. Every other read here treats
        the mode as a door -- see :meth:`history` -- and a list is the one place
        where "everything I own" would walk straight through it, handing a Chat
        client a Code session's title because both are rows in one table.

        Ordered by activity rather than by creation: a list sorted by creation
        puts a session untouched for a month above the one you were in five
        minutes ago, which is backwards for a list whose whole job is getting
        you back to where you were.

        Bounded by ``limit`` rather than paged. This is one person's recent
        sessions; a keyset cursor over a list bounded by human memory is
        machinery with no reader.
        """
        ...

    async def set_title_if_unset(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        title: str,
        mode: SessionMode | None = None,
    ) -> None:
        """Name a session, but only if nothing has named it yet.

        Its own method rather than a flag on :meth:`rename_session`, because the
        two mean opposite things: this one cannot destroy anything a person
        typed, and renaming exists in order to. A boolean parameter would put
        both meanings behind one call site, where the wrong argument silently
        overwrites a name somebody chose.

        The condition is in the statement, not in the caller. A read-then-write
        would race a concurrent first turn and would also re-apply on a retry;
        ``WHERE title IS NULL`` makes first-instruction-wins true rather than
        likely.

        Updating no rows is **not** an error. It means either that a title is
        already there -- the expected case from the second turn onwards -- or
        that the session is not this principal's, which the caller has already
        established by reading it.
        """
        ...

    async def rename_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        title: str,
        mode: SessionMode | None = None,
    ) -> ConversationSession:
        """Give a session the name a person chose, replacing whatever was there.

        The only overwrite. Raises :class:`NotFoundError` when the session is
        not this principal's, in this tenant, in this mode -- the same
        three-axis refusal, and the same single answer, as every other read
        here.
        """
        ...

    async def delete_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        mode: SessionMode | None = None,
    ) -> None:
        """Remove one session and everything that belonged only to it.

        The whole stream or none of it (ADR-056). Messages, turns, and the
        session's own event stream go together; what stays is anything a
        *second* thing also points at -- which today means the workspace
        artifacts, unreachable afterwards rather than deleted, exactly as an
        overwritten workspace version already is.

        Raises :class:`NotFoundError` on the same three-axis miss as every read
        here, so "not yours" and "not there" stay one answer. Raises
        :class:`ChatTurnBusyError` when a turn is still running: that turn holds
        a coroutine which is about to write, and deleting the session out from
        under it would leave the write with nowhere to land. The same error the
        second concurrent turn gets, because it is the same fact -- this session
        is busy -- and it already answers 409.
        """
        ...

    async def advance_workspace_version(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        expected: Identifier | None,
        next_version: Identifier,
    ) -> None:
        """Move the pointer, but only if it is still where the caller left it.

        ``expected`` is the version the new manifest was built from, and
        ``None`` is a legitimate value rather than a missing argument: it says
        "this session had written nothing", which is the state every session
        starts in and therefore a state the comparison has to be able to name.
        An implementation whose comparison cannot express it would refuse the
        first write of every session.

        A comparison that fails raises ``WorkspacePointerConflictError`` and
        leaves the stored version alone. Ownership is checked the same way
        every other method checks it, and answers ``NotFoundError``.
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

        That ownership check also refuses a ``code`` session with
        ``NotFoundError``. There is no ``mode`` argument to pass, because this
        ledger is the Chat lifecycle itself and Code writes no row in it: a
        caller wanting to claim a turn against a code session does not exist,
        and a parameter would state that one might. Refusing here is what makes
        the refusal total -- every other method of this protocol is reached
        through a ``turn_id``, and no turn id can name a code session once none
        can be claimed for one.
        """
        ...

    async def turn(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
    ) -> StoredChatTurn:
        """One stored turn, by id, for the principal whose session it is.

        The first read on this protocol. Every other method here writes, or
        scans for a coordinator; a turn's own record could be reached only by
        the coroutine that was executing it, which is why the console could
        show which chunks an answer cited and nothing else about them.

        Exists for one caller: the route that serves the passage behind a
        citation (ADR-067). That route needs the turn to answer a question it
        must not take from the requester -- *did this answer actually cite this
        chunk* -- and the answer has to come from the ledger rather than from
        the request, or the endpoint would read any chunk id a caller cared to
        name.

        Refuses like every other method: a wrong tenant, a wrong principal, a
        turn in another session and a turn that does not exist all raise
        ``NotFoundError``, because any difference between them would confirm
        that somebody else's turn exists.

        Returns the turn in whatever state it is in. A caller wanting only
        published citations must check ``status`` itself -- a running turn has
        no result, and a withheld one has a scrubbed one, and neither is this
        method's business to interpret.
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
    "SessionMode",
    "StoredChatTurn",
    "StoredMessage",
    "WorkspacePointerConflictError",
    "chat_turn_terminal_event_key",
]
