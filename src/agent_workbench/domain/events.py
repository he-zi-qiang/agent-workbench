"""The unified event protocol.

CLI output, SSE, the audit trail and OpenTelemetry consume these events; none
of them invents its own callback. Events describe what happened, they do not
decide where execution stands: the conversation store owns chat history, the
LangGraph checkpointer owns workflow position, and this log owns observation.

Two properties are structural rather than conventional.

Durability is a property of the event type, not a caller's choice. Token deltas
and high-frequency tool progress are transient: they stream to a subscriber and
are never written to PostgreSQL, because per-token rows would turn a chat into
a write-amplification problem. Everything else is durable and replayable.

Only durable events carry a sequence. An SSE cursor is ``(stream_id,
sequence)``, so an event without a persisted position must not claim one; a
reconnecting client resumes from the last durable event instead.

Event payloads describe rather than reproduce. Tool arguments appear as a size
and a digest, not as their content: the log is read by operators and shipped to
tracing backends, and prompts, documents and credentials do not belong there.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from typing import Annotated, Final, Literal

from pydantic import (
    AwareDatetime,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from agent_workbench.domain.artifacts import ArtifactRef, Sha256
from agent_workbench.domain.context import Citation
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.identifiers import Identifier, new_event_id
from agent_workbench.domain.policies import PolicyEffect
from agent_workbench.domain.runs import (
    BudgetUsage,
    ModelProfileName,
    RunBudget,
    RunKind,
    RunStatus,
    StopReason,
    TokenUsage,
)
from agent_workbench.domain.schema import (
    AnswerText,
    BoundedText,
    DomainModel,
    ShortText,
    ThinkingText,
    VersionedModel,
)
from agent_workbench.domain.task_intent import TaskIntent
from agent_workbench.domain.tools import (
    PermissionScope,
    ProposedToolName,
    ToolName,
    ToolRisk,
)
from agent_workbench.domain.workspace import WorkspaceName

EventType = Literal[
    "TaskSubmitted",
    "TaskApprovalRequested",
    "TaskApprovalDecided",
    "TaskClaimed",
    "TaskRetryScheduled",
    "TaskDeadLettered",
    "TaskAwaitingApproval",
    "TaskSucceeded",
    "TaskFailed",
    "TaskCancelled",
    "TaskParkedForMigration",
    "RunStarted",
    "ContextBuilt",
    "RetrievalRejected",
    "ModelStarted",
    "ModelDelta",
    "ModelThinkingDelta",
    "ModelCompleted",
    "AnswerCommitted",
    "UngroundedAnswerCommitted",
    "AnswerWithheld",
    "ChatTurnExpired",
    "ToolProposed",
    "PermissionRequested",
    "PermissionResolved",
    "ToolApprovalDecided",
    "ToolStarted",
    "ToolProgress",
    "ToolCompleted",
    "ToolFailed",
    "ContextCompacted",
    "AgentDelegated",
    "AgentCompleted",
    "RunPaused",
    "RunCompleted",
    "RunFailed",
    "RunCancelled",
]

Durability = Literal["durable", "transient"]

ModelFinishReason = Literal["stop", "tool_use", "max_tokens", "cancelled", "error"]
PauseReason = Literal["approval", "migration"]
#: What a human may answer when a call is held for approval.
#:
#: ``approve_for_session`` is a standing answer, and a standing answer is only
#: safe if it is about *this* call rather than about the tool: the policy
#: engine decides approval from the tool's declared risk alone and never reads
#: the arguments, so "approve this tool from now on" would let one harmless
#: invocation stand in for every later one.
ApprovalDecision = Literal["approve_once", "approve_for_session", "deny"]
#: What produced the decision, which is not the same question as what the
#: decision was. Every value below refuses except the first two, and collapsing
#: them would make four different stories about a held call read as one: a
#: person said no; a rule said no on their behalf; nobody was there; the run
#: stopped waiting; the place we ask broke. Only the first is a decision to
#: refuse -- the rest are refusals for want of one, and an operator who cannot
#: tell them apart is looking for a slow human every time.
ApprovalDecidedBy = Literal[
    "human",
    "session_rule",
    "timeout",
    "cancelled",
    "gate_failed",
]
#: The proposed arguments as shown to whoever is being asked to allow them.
#:
#: Its own ceiling rather than ``BoundedText``: this is the one preview written
#: without ``runtime.record_step_inputs``, so what bounds it is the only thing
#: bounding what an unconditional field puts on the stream. Smaller than
#: ``BoundedText`` on purpose -- it is read by a person deciding now, not by an
#: operator reconstructing a run later.
APPROVAL_PREVIEW_LIMIT: Final[int] = 2048
ApprovalPreview = Annotated[str, StringConstraints(max_length=APPROVAL_PREVIEW_LIMIT)]


class TaskSubmitted(DomainModel):
    """A Task was opened, and this is the request it was opened for.

    Written in the same transaction as the ``task_runs`` row, so a Task can
    never exist without the event that says why -- and the event can never
    describe a Task that was rolled back.

    It carries what the submission decided and nothing the submission merely
    referenced: the objective lives behind ``input_ref``, because an event is
    replayed into timelines and SSE frames where a caller-supplied body has no
    business being repeated.
    """

    kind: Literal["TaskSubmitted"] = "TaskSubmitted"
    graph_version: ShortText
    input_ref: Identifier
    # Who decided the submission's shape, and in the triage case why -- the
    # timeline is where "why is this Task shaped like this" is answered, and
    # this is the earliest entry (ADR-036). Absent on events written before
    # the field existed and on clients that never say.
    intent: TaskIntent | None = None


class TaskLifecycleEvent(DomainModel):
    """A safe, replayable Task Registry transition fact.

    Free-form ``status_detail`` can originate in a model or provider exception.
    It remains on the product row and never enters this operator-visible stream.
    """

    task_id: Identifier
    epoch: int = Field(ge=0)
    attempt: int = Field(ge=0)


class TaskClaimed(TaskLifecycleEvent):
    kind: Literal["TaskClaimed"] = "TaskClaimed"
    status: Literal["running"] = "running"


class TaskRetryScheduled(TaskLifecycleEvent):
    kind: Literal["TaskRetryScheduled"] = "TaskRetryScheduled"
    status: Literal["queued"] = "queued"
    reason_code: Literal["lease_expired", "retry_requested"]
    delay_seconds: int = Field(ge=0)


class TaskDeadLettered(TaskLifecycleEvent):
    kind: Literal["TaskDeadLettered"] = "TaskDeadLettered"
    status: Literal["dead_letter"] = "dead_letter"
    #: Which writer gave up, and why. Two values because there are now two
    #: writers: the reaper, which finds a lease that expired once too often,
    #: and the invocation budget (ADR-040), which finds a Task that has already
    #: paid for everything it was allowed. Telling them apart is the whole
    #: point -- an operator who cannot is looking at a gate that might as well
    #: be destroying Tasks silently.
    #:
    #: No default, deliberately, and the same way ``TaskRetryScheduled`` has
    #: none: a default here would let a third writer appear and be filed under
    #: whichever reason happened to be listed first. Widening the set keeps
    #: every stored event valid, because the old value stays in it.
    reason_code: Literal["lease_expired", "invocation_budget_exhausted"]


class TaskAwaitingApproval(TaskLifecycleEvent):
    kind: Literal["TaskAwaitingApproval"] = "TaskAwaitingApproval"
    status: Literal["waiting_approval"] = "waiting_approval"


class TaskSucceeded(TaskLifecycleEvent):
    kind: Literal["TaskSucceeded"] = "TaskSucceeded"
    status: Literal["succeeded"] = "succeeded"


class TaskFailed(TaskLifecycleEvent):
    kind: Literal["TaskFailed"] = "TaskFailed"
    status: Literal["failed"] = "failed"
    reason_code: Literal["execution_failed"] = "execution_failed"


class TaskCancelled(TaskLifecycleEvent):
    kind: Literal["TaskCancelled"] = "TaskCancelled"
    status: Literal["cancelled"] = "cancelled"
    reason_code: Literal["cancel_requested"] = "cancel_requested"


class TaskParkedForMigration(TaskLifecycleEvent):
    kind: Literal["TaskParkedForMigration"] = "TaskParkedForMigration"
    status: Literal["waiting_migration"] = "waiting_migration"
    reason_code: Literal["migration_required"] = "migration_required"


class TaskApprovalRequested(DomainModel):
    """A graph node paused and opened an approval for a human to answer.

    This is how the approval becomes findable. The id lives in the checkpoint's
    interrupt and in the ledger, and neither is something a client may read, so
    without this event the only way to decide an approval would be to guess its
    id. It carries no draft, no evidence and no reason text -- what is being
    approved is the Task, which the reader already has.

    Written by the request that opened the approval, and keyed by it, so a node
    re-entered after a crash asks the same question and leaves one event.
    """

    kind: Literal["TaskApprovalRequested"] = "TaskApprovalRequested"
    task_id: Identifier
    approval_id: Identifier
    graph_node_operation_id: Identifier


class TaskApprovalDecided(DomainModel):
    """A human decided an approval, and the Task was requeued for it.

    Both outcomes are recorded and both requeue: a rejection is a path through
    the graph, not an absence of one, so the node that resumes decides what it
    means. ``decision_version`` is what makes replaying the same decision a
    no-op rather than a second event.
    """

    kind: Literal["TaskApprovalDecided"] = "TaskApprovalDecided"
    task_id: Identifier
    approval_id: Identifier
    decision: Literal["approved", "rejected"]
    decision_version: int = Field(ge=1)


class RunStarted(DomainModel):
    kind: Literal["RunStarted"] = "RunStarted"
    run_kind: RunKind
    model_profile: ModelProfileName
    tool_names: tuple[ToolName, ...] = ()
    budget: RunBudget


class ContextBuilt(DomainModel):
    kind: Literal["ContextBuilt"] = "ContextBuilt"
    chunk_count: int = Field(ge=0)
    citation_count: int = Field(ge=0)
    token_estimate: int = Field(ge=0)
    retrieval_trace_id: Identifier | None = None


class ModelStarted(DomainModel):
    kind: Literal["ModelStarted"] = "ModelStarted"
    model_call_id: Identifier
    model_profile: ModelProfileName
    model_id: ShortText
    # What was sent to the model, when `runtime.record_step_inputs` is on
    # (ADR-019). Empty otherwise, and empty is the default so a deployment that
    # never opted in emits byte-identical payloads. Bounded for the reason every
    # other free text here is: this is a database row and an SSE frame.
    prompt_preview: BoundedText = ""


class ModelDelta(DomainModel):
    """A coalesced slice of streamed text. Never persisted."""

    kind: Literal["ModelDelta"] = "ModelDelta"
    model_call_id: Identifier
    text: BoundedText


class ModelThinkingDelta(DomainModel):
    """A coalesced slice of streamed reasoning. Never persisted.

    A sibling of ``ModelDelta`` rather than a field on it, because the two
    texts diverge at the publication fence: a delta of answer text is redacted
    whenever the answer itself could still be withheld, and reasoning follows
    the same rule for the same cause -- the model reasons *about* the evidence
    it was shown, so its thinking can quote exactly what a withheld answer
    must not have shown (ADR-061).
    """

    kind: Literal["ModelThinkingDelta"] = "ModelThinkingDelta"
    model_call_id: Identifier
    text: BoundedText


class ModelCompleted(DomainModel):
    kind: Literal["ModelCompleted"] = "ModelCompleted"
    model_call_id: Identifier
    finish_reason: ModelFinishReason
    usage: TokenUsage = TokenUsage()
    # The answer, not a summary of it (ADR-035 §3.1). ADR-019 chose to bound
    # what is recorded *about* a run, and noted in passing that the event
    # stream already carried the model's own words because that is how an
    # answer reaches the asker. Those are the two halves: `prompt_preview`
    # below is a preview and stays at the preview ceiling; this is the product.
    text: AnswerText = ""
    # How the model got there. Still an excerpt rather than the whole chain:
    # the full text streamed live as ``ModelThinkingDelta`` and was never owed
    # the durable log ("describe, don't copy") -- this is what a reader arriving
    # after the fact, or a Task timeline with no live channel at all, gets to
    # see of the process (ADR-061). Empty when the call did not think.
    #
    # On its own ceiling, not the preview one, and cut from the middle rather
    # than the end (ADR-064). Two separate corrections to the same field:
    #
    #   * A preview is a summary a reader consults and an account of reasoning
    #     is not, so `BOUNDED_TEXT_LIMIT` was the wrong bound to share -- and it
    #     could not simply be raised, because `argument_preview` and
    #     `output_preview` share it and ADR-063 argues from its being 4096.
    #   * `bounded()` keeps the head. For reasoning that reliably discards the
    #     conclusion, which is the half a reader came for; `bounded_thinking()`
    #     keeps both ends and names the gap.
    #
    # One row per model call is what makes 16,384 affordable here, and also why
    # it is not the answer ceiling, which is one row per run.
    thinking_preview: ThinkingText = ""
    output_ref: ArtifactRef | None = None
    tool_call_ids: tuple[Identifier, ...] = ()


class AnswerCommitted(DomainModel):
    """An answer that cleared its final evidence and authorization checks.

    ``ModelCompleted`` records what the provider did. It is deliberately not
    the publication boundary for retrieval-backed chat: a grant can be
    withdrawn after the provider finishes and before the evidence is checked
    again. Consumers may display answer text only from this event.
    """

    kind: Literal["AnswerCommitted"] = "AnswerCommitted"
    text: AnswerText
    citations: tuple[Citation, ...] = ()


class UngroundedAnswerCommitted(DomainModel):
    """An answer produced without evidence, and recorded as such (ADR-018).

    Deliberately not an ``AnswerCommitted`` with an empty citation list. That
    event means "this text rested on these revisions and they were re-checked
    before release"; there is nothing here to re-check, so borrowing it would
    make the audit log unable to distinguish a verified answer from an
    unverified one -- and a record whose provenance a reader cannot recover is
    worse than no record.

    There is no ``citations`` field rather than an empty one. An empty tuple is
    a list that could have been non-empty; this path has no ``ContextPacket``,
    so nothing could ever populate it. Absent says that; empty invites somebody
    to fill it in.
    """

    kind: Literal["UngroundedAnswerCommitted"] = "UngroundedAnswerCommitted"
    text: AnswerText


class RetrievalRejected(DomainModel):
    """Retrieval ran, and the turn chose not to answer from what came back.

    The routed shape (ADR-018) decides between a grounded and an ungrounded
    answer by retrieving and scoring, then falling back when nothing was
    relevant enough. Without this event that decision leaves no trace at all:
    ``ContextBuilt`` is only emitted when context reaches the model, so a turn
    that searched for a minute and rejected the result looks identical in the
    log to one that never searched. "Why is this answer ungrounded?" is then
    unanswerable after the fact, which is the question the grounded/ungrounded
    split exists to make answerable.

    The scores are recorded rather than a verdict, because the threshold is
    configuration: an operator lowering it needs to know what the scores
    actually were on the turns that fell back.

    ``chunk_count`` counts what survived authorization, which is what keeps
    this event compatible with ADR-018's deliberate non-disclosure: a corpus
    whose matching documents this asker may not read reports zero here, exactly
    as an empty corpus does. Counting candidates before the ACL check would
    tell the asker that documents they cannot read exist.
    """

    kind: Literal["RetrievalRejected"] = "RetrievalRejected"
    chunk_count: int = Field(ge=0)
    #: The best cross-encoder score, or ``None`` when no reranker answered --
    #: which is itself a reason to reject, and a different one from a low score.
    top_relevance: float | None = None
    threshold: float


class AnswerWithheld(DomainModel):
    """A safe replacement for an answer that failed its publication check."""

    kind: Literal["AnswerWithheld"] = "AnswerWithheld"
    reason_code: Literal["sources_changed"] = "sources_changed"
    # Stays at the preview ceiling while the answer events moved past it, and
    # the difference is the point: what goes here is the refusal this system
    # wrote, never the model output it replaced.
    text: BoundedText


class ChatTurnExpired(DomainModel):
    """A fixed Chat execution lease expired before publication completed.

    This is the Chat ledger's terminal observation, not a second verdict on
    the runtime. A provider run may already have emitted ``RunCompleted`` while
    its answer was still waiting to cross the publication boundary. Every
    terminal attribute is therefore fixed, and candidate output has no field
    through which it could enter the event stream.
    """

    kind: Literal["ChatTurnExpired"] = "ChatTurnExpired"
    turn_id: Identifier
    status: Literal["failed"] = "failed"
    stop_reason: Literal["deadline"] = "deadline"
    error_code: Literal["stale_execution"] = "stale_execution"
    retryable: Literal[False] = False


class ToolProposed(DomainModel):
    """A model proposed a call. ``risk`` is unknown for an unknown tool."""

    kind: Literal["ToolProposed"] = "ToolProposed"
    tool_call_id: Identifier
    tool_name: ProposedToolName
    argument_bytes: int = Field(ge=0)
    argument_sha256: Sha256
    # The call the model actually proposed, when `runtime.record_step_inputs`
    # is on (ADR-019). The digest above stays the identity either way: it is
    # taken over the whole canonical arguments, while this may be truncated.
    argument_preview: BoundedText = ""
    risk: ToolRisk | None = None


class PermissionRequested(DomainModel):
    """A call is held because a human has to allow it.

    ``approval_preview`` is the single exception to this module's rule that
    payloads describe rather than reproduce, and it is deliberately narrow.
    Everywhere else the arguments appear as a size and a digest because the
    reader is an operator reconstructing what happened; here the reader is a
    person being asked to permit something that has not happened yet, and for
    a tool that runs a script the arguments are not a detail of the request,
    they are the request. A digest cannot be consented to.

    So it is written whenever there is someone to ask, and not written when
    there is not: a deployment with no approval facility refuses these calls
    and gains nothing from having recorded them. That, rather than
    ``runtime.record_step_inputs``, is the gate -- the flag governs a record
    kept for later, and this is a question asked now. Opening the flag instead
    would also open ``ModelStarted.prompt_preview``, which is a different
    decision about a different body of text (ADR-019).
    """

    kind: Literal["PermissionRequested"] = "PermissionRequested"
    tool_call_id: Identifier
    required_scopes: tuple[PermissionScope, ...] = ()
    risk: ToolRisk | None = None
    approval_id: Identifier | None = None
    # The tool's name is not repeated here: ToolProposed is durable, carries it
    # ungated, and is emitted for this same tool_call_id before anything can
    # ask for approval.
    approval_preview: ApprovalPreview = ""


class ToolApprovalDecided(DomainModel):
    """How a held call was answered, and by whom.

    Separate from ``PermissionResolved``, which says what the policy engine
    decided. This says what happened to the question the policy engine's
    decision raised, and the two can disagree: a call the policy allowed can
    still end here as ``deny``.

    Emitted for every outcome including ``timeout``, because "nobody answered"
    is a fact about the run that a reader cannot otherwise recover -- the
    refusal alone looks exactly like a policy denial.
    """

    kind: Literal["ToolApprovalDecided"] = "ToolApprovalDecided"
    tool_call_id: Identifier
    approval_id: Identifier
    decision: ApprovalDecision
    decided_by: ApprovalDecidedBy


class PermissionResolved(DomainModel):
    kind: Literal["PermissionResolved"] = "PermissionResolved"
    tool_call_id: Identifier
    effect: PolicyEffect
    reason_code: ShortText
    approval_id: Identifier | None = None


class ToolStarted(DomainModel):
    kind: Literal["ToolStarted"] = "ToolStarted"
    tool_call_id: Identifier
    tool_name: ProposedToolName


class ToolProgress(DomainModel):
    """Handler-reported progress. Streamed only, never persisted."""

    kind: Literal["ToolProgress"] = "ToolProgress"
    tool_call_id: Identifier
    message: ShortText
    percent: int | None = Field(default=None, ge=0, le=100)


class ToolCompleted(DomainModel):
    """A call returned. ``output_preview`` is what it returned, when recorded.

    The symmetric half of ``ToolProposed.argument_preview``, added because
    without it a step could be opened and still not answer the question a
    reader opens it for. ``output_bytes`` says a tool returned 4 kilobytes; it
    cannot say the grep found nothing, or that the file read back was the one
    the next step then edited. For a workspace tool there is no artifact
    either -- ``artifact`` is ``None`` for all five -- so the size was the only
    thing a console could show, and "读取工作区 · 4.1 KB" is a receipt rather
    than a transcript.

    Under the same ``runtime.record_step_inputs`` gate as the argument half,
    and for the same reason: both reproduce content that a deployment may not
    want in its event log. A deployment that declines to record what a tool was
    asked has not agreed to record what it answered.

    ``workspace_writes`` sits **outside** that gate, next to ``tool_name`` and
    ``output_bytes`` rather than next to ``output_preview`` (ADR-063). The gate
    is about content, and a name is not content: the same principal can already
    read the whole workspace listing, so putting the name here discloses
    nothing that withholding it would protect. Under the gate the field would
    disappear in exactly the deployments that turned previews down -- which is
    to say the feature would exist only where it was least needed, and a
    console reading it would have to fall back to parsing prose anyway. The
    remaining routes to this fact are worse in kind, not merely in convenience:
    ``ToolProposed.argument_preview`` is bounded at 4096 characters and drops
    the name first for the largest writes, and ``output_preview`` is an
    untested English sentence.
    """

    kind: Literal["ToolCompleted"] = "ToolCompleted"
    tool_call_id: Identifier
    duration_ms: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    #: Which workspace names this call bound to new bytes. Not a preview and
    #: not gated; see the note above.
    workspace_writes: tuple[WorkspaceName, ...] = ()
    #: Bounded like every other preview in this module. `output_bytes` above
    #: stays the truth about size, and `truncated` about the tool's own
    #: clipping; this may be shortened again on its way here, which is why the
    #: two are separate fields rather than one.
    output_preview: BoundedText = ""
    artifact: ArtifactRef | None = None
    truncated: bool = False


class ToolFailed(DomainModel):
    kind: Literal["ToolFailed"] = "ToolFailed"
    tool_call_id: Identifier
    error: ErrorInfo
    duration_ms: int = Field(ge=0)


class ContextCompacted(DomainModel):
    """Compaction derives a shorter context; it never edits the record."""

    kind: Literal["ContextCompacted"] = "ContextCompacted"
    removed_message_count: int = Field(ge=0)
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    summary_ref: ArtifactRef | None = None


class AgentDelegated(DomainModel):
    kind: Literal["AgentDelegated"] = "AgentDelegated"
    child_agent_run_id: Identifier
    profile_name: ShortText
    graph_node_id: Identifier | None = None


class AgentCompleted(DomainModel):
    kind: Literal["AgentCompleted"] = "AgentCompleted"
    child_agent_run_id: Identifier
    status: RunStatus
    stop_reason: StopReason
    usage: BudgetUsage = BudgetUsage()


class RunPaused(DomainModel):
    kind: Literal["RunPaused"] = "RunPaused"
    reason: PauseReason
    approval_id: Identifier | None = None


class RunCompleted(DomainModel):
    kind: Literal["RunCompleted"] = "RunCompleted"
    stop_reason: StopReason
    usage: BudgetUsage = BudgetUsage()


class RunFailed(DomainModel):
    kind: Literal["RunFailed"] = "RunFailed"
    error: ErrorInfo
    stop_reason: StopReason = "error"
    usage: BudgetUsage = BudgetUsage()


class RunCancelled(DomainModel):
    kind: Literal["RunCancelled"] = "RunCancelled"
    reason_code: ShortText = "cancel_requested"
    usage: BudgetUsage = BudgetUsage()


EventPayload = Annotated[
    TaskSubmitted
    | TaskApprovalRequested
    | TaskApprovalDecided
    | TaskClaimed
    | TaskRetryScheduled
    | TaskDeadLettered
    | TaskAwaitingApproval
    | TaskSucceeded
    | TaskFailed
    | TaskCancelled
    | TaskParkedForMigration
    | RunStarted
    | ContextBuilt
    | RetrievalRejected
    | ModelStarted
    | ModelDelta
    | ModelThinkingDelta
    | ModelCompleted
    | AnswerCommitted
    | UngroundedAnswerCommitted
    | AnswerWithheld
    | ChatTurnExpired
    | ToolProposed
    | PermissionRequested
    | PermissionResolved
    | ToolApprovalDecided
    | ToolStarted
    | ToolProgress
    | ToolCompleted
    | ToolFailed
    | ContextCompacted
    | AgentDelegated
    | AgentCompleted
    | RunPaused
    | RunCompleted
    | RunFailed
    | RunCancelled,
    Field(discriminator="kind"),
]

# Durability belongs to the event type. A caller cannot promote a token delta
# into the durable log, and cannot demote a terminal state out of it.
EVENT_DURABILITY: Final[Mapping[EventType, Durability]] = {
    "TaskSubmitted": "durable",
    "TaskApprovalRequested": "durable",
    "TaskApprovalDecided": "durable",
    "TaskClaimed": "durable",
    "TaskRetryScheduled": "durable",
    "TaskDeadLettered": "durable",
    "TaskAwaitingApproval": "durable",
    "TaskSucceeded": "durable",
    "TaskFailed": "durable",
    "TaskCancelled": "durable",
    "TaskParkedForMigration": "durable",
    "RunStarted": "durable",
    "ContextBuilt": "durable",
    "ModelStarted": "durable",
    "ModelDelta": "transient",
    # Transient for the same reason ModelDelta is: reasoning streams at token
    # rate, and the durable trace of it is ModelCompleted.thinking_preview.
    "ModelThinkingDelta": "transient",
    "ModelCompleted": "durable",
    "AnswerCommitted": "durable",
    "UngroundedAnswerCommitted": "durable",
    "RetrievalRejected": "durable",
    "AnswerWithheld": "durable",
    "ChatTurnExpired": "durable",
    "ToolProposed": "durable",
    "PermissionRequested": "durable",
    "PermissionResolved": "durable",
    "ToolApprovalDecided": "durable",
    "ToolStarted": "durable",
    "ToolProgress": "transient",
    "ToolCompleted": "durable",
    "ToolFailed": "durable",
    "ContextCompacted": "durable",
    "AgentDelegated": "durable",
    "AgentCompleted": "durable",
    "RunPaused": "durable",
    "RunCompleted": "durable",
    "RunFailed": "durable",
    "RunCancelled": "durable",
}

TRANSIENT_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    event_type
    for event_type, durability in EVENT_DURABILITY.items()
    if durability == "transient"
)
DURABLE_EVENT_TYPES: Final[frozenset[str]] = (
    frozenset(EVENT_DURABILITY) - TRANSIENT_EVENT_TYPES
)


class EventEnvelope(VersionedModel):
    """One observation, addressable by ``(stream_id, sequence)`` when durable."""

    event_id: Identifier
    stream_id: Identifier
    run_id: Identifier
    event_type: EventType
    durability: Durability
    timestamp: AwareDatetime
    payload: EventPayload
    sequence: int | None = Field(default=None, ge=1)
    task_id: Identifier | None = None
    graph_node_id: Identifier | None = None
    parent_event_id: Identifier | None = None

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: AwareDatetime) -> AwareDatetime:
        # Canonical UTC keeps cursor comparison and golden payloads stable
        # regardless of the producing process's local zone.
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_envelope(self) -> EventEnvelope:
        if self.event_type != self.payload.kind:
            raise ValueError(
                f"event_type {self.event_type!r} disagrees with payload "
                f"{self.payload.kind!r}"
            )
        expected = EVENT_DURABILITY[self.event_type]
        if self.durability != expected:
            raise ValueError(f"{self.event_type} is {expected}, not {self.durability}")
        if (self.sequence is not None) is not (self.durability == "durable"):
            raise ValueError(
                "a durable event carries the sequence assigned by its stream, "
                "and a transient event carries none"
            )
        return self

    @classmethod
    def for_payload(
        cls,
        payload: EventPayload,
        *,
        stream_id: str,
        run_id: str,
        timestamp: AwareDatetime,
        sequence: int | None = None,
        event_id: str | None = None,
        task_id: str | None = None,
        graph_node_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> EventEnvelope:
        """Build an envelope whose type and durability follow the payload."""

        event_type: EventType = payload.kind
        return cls(
            event_id=event_id or new_event_id(),
            stream_id=stream_id,
            run_id=run_id,
            event_type=event_type,
            durability=EVENT_DURABILITY[event_type],
            timestamp=timestamp,
            payload=payload,
            sequence=sequence,
            task_id=task_id,
            graph_node_id=graph_node_id,
            parent_event_id=parent_event_id,
        )


__all__ = [
    "DURABLE_EVENT_TYPES",
    "EVENT_DURABILITY",
    "TRANSIENT_EVENT_TYPES",
    "AgentCompleted",
    "AgentDelegated",
    "AnswerCommitted",
    "AnswerWithheld",
    "ChatTurnExpired",
    "ContextBuilt",
    "ContextCompacted",
    "Durability",
    "EventEnvelope",
    "EventPayload",
    "EventType",
    "ModelCompleted",
    "ModelDelta",
    "ModelFinishReason",
    "ModelStarted",
    "ModelThinkingDelta",
    "PauseReason",
    "PermissionRequested",
    "PermissionResolved",
    "RunCancelled",
    "RunCompleted",
    "RunFailed",
    "RunPaused",
    "RunStarted",
    "TaskApprovalDecided",
    "TaskApprovalRequested",
    "TaskAwaitingApproval",
    "TaskCancelled",
    "TaskClaimed",
    "TaskDeadLettered",
    "TaskFailed",
    "TaskLifecycleEvent",
    "TaskParkedForMigration",
    "TaskRetryScheduled",
    "TaskSubmitted",
    "TaskSucceeded",
    "ToolApprovalDecided",
    "ToolCompleted",
    "ToolFailed",
    "ToolProgress",
    "ToolProposed",
    "ToolStarted",
    "UngroundedAnswerCommitted",
]
