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
    RunStarted
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
    "ToolCompleted",
    "ToolFailed",
    "ToolProgress",
    "ToolProposed",
    "ToolStarted",
]
