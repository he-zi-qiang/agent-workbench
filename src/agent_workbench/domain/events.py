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

from pydantic import AwareDatetime, Field, field_validator, model_validator

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
    BoundedText,
    DomainModel,
    ShortText,
    VersionedModel,
)
from agent_workbench.domain.tools import (
    PermissionScope,
    ProposedToolName,
    ToolName,
    ToolRisk,
)

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
    "ModelStarted",
    "ModelDelta",
    "ModelCompleted",
    "AnswerCommitted",
    "AnswerWithheld",
    "ChatTurnExpired",
    "ToolProposed",
    "PermissionRequested",
    "PermissionResolved",
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
    reason_code: Literal["lease_expired"] = "lease_expired"


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


class ModelDelta(DomainModel):
    """A coalesced slice of streamed text. Never persisted."""

    kind: Literal["ModelDelta"] = "ModelDelta"
    model_call_id: Identifier
    text: BoundedText


class ModelCompleted(DomainModel):
    kind: Literal["ModelCompleted"] = "ModelCompleted"
    model_call_id: Identifier
    finish_reason: ModelFinishReason
    usage: TokenUsage = TokenUsage()
    text: BoundedText = ""
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
    text: BoundedText
    citations: tuple[Citation, ...] = ()


class AnswerWithheld(DomainModel):
    """A safe replacement for an answer that failed its publication check."""

    kind: Literal["AnswerWithheld"] = "AnswerWithheld"
    reason_code: Literal["sources_changed"] = "sources_changed"
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
    risk: ToolRisk | None = None


class PermissionRequested(DomainModel):
    kind: Literal["PermissionRequested"] = "PermissionRequested"
    tool_call_id: Identifier
    required_scopes: tuple[PermissionScope, ...] = ()
    risk: ToolRisk | None = None
    approval_id: Identifier | None = None


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
    kind: Literal["ToolCompleted"] = "ToolCompleted"
    tool_call_id: Identifier
    duration_ms: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
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
    | ModelStarted
    | ModelDelta
    | ModelCompleted
    | AnswerCommitted
    | AnswerWithheld
    | ChatTurnExpired
    | ToolProposed
    | PermissionRequested
    | PermissionResolved
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
    "ModelCompleted": "durable",
    "AnswerCommitted": "durable",
    "AnswerWithheld": "durable",
    "ChatTurnExpired": "durable",
    "ToolProposed": "durable",
    "PermissionRequested": "durable",
    "PermissionResolved": "durable",
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
    "ToolCompleted",
    "ToolFailed",
    "ToolProgress",
    "ToolProposed",
    "ToolStarted",
]
