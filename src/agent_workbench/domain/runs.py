"""Agent run input, budgets and structured outcome.

One run is one agent's model-tool loop inside one graph node. It is not a task
and not a workflow: the graph owns where a long task stands, and this module
owns what a single agent was asked to do, what it may spend, and what it
produced.

Budgets are values rather than ambient limits so a request can only narrow
them. The effective budget is ``min(requested, configured)``, computed by the
caller; nothing here can widen a ceiling that settings established.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.context import Citation, ContextPacket
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.messages import Message
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.schema import BoundedText, DomainModel, VersionedModel
from agent_workbench.domain.tools import ToolName

RunKind = Literal["chat", "task"]
ModelProfileName = Literal["main", "compact"]
RunStatus = Literal["completed", "failed", "cancelled"]

# The runtime state machine. Execution positions are transient process state;
# only the terminal three appear in an outcome.
RunState = Literal[
    "building_context",
    "model_streaming",
    "validating_tools",
    "authorizing",
    "executing_tools",
    "recording_results",
    "compacting",
    "completed",
    "failed",
    "cancelled",
]
TERMINAL_RUN_STATES: Final[frozenset[str]] = frozenset(
    {"completed", "failed", "cancelled"}
)

StopReason = Literal[
    "completed",
    "max_steps",
    "max_tool_calls",
    "token_budget",
    "cost_budget",
    "deadline",
    "cancelled",
    "error",
]

SystemPrompt = Annotated[str, StringConstraints(max_length=65_536)]


class TokenUsage(DomainModel):
    """Token accounting for one run, including cache traffic."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def merged(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class BudgetUsage(DomainModel):
    """What a run has consumed so far.

    Cost is integer micro-USD: money never round-trips through a float, and an
    integer stays exact across JSON, PostgreSQL and multi-agent aggregation.
    """

    steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    tokens: TokenUsage = TokenUsage()
    cost_micro_usd: int = Field(default=0, ge=0)

    def merged(self, other: BudgetUsage) -> BudgetUsage:
        """Aggregate a child agent's usage into the parent's."""

        return BudgetUsage(
            steps=self.steps + other.steps,
            tool_calls=self.tool_calls + other.tool_calls,
            tokens=self.tokens.merged(other.tokens),
            cost_micro_usd=self.cost_micro_usd + other.cost_micro_usd,
        )


class RunBudget(DomainModel):
    """Hard ceilings for one run."""

    max_steps: int = Field(ge=1, le=100)
    max_tool_calls: int = Field(ge=1, le=500)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_cost_micro_usd: int | None = Field(default=None, ge=1)
    deadline: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_budget(self) -> RunBudget:
        if self.max_tool_calls < self.max_steps:
            raise ValueError("max_tool_calls must be >= max_steps")
        return self

    def overrun_reason_for(
        self,
        usage: BudgetUsage,
        *,
        now: datetime | None = None,
    ) -> StopReason | None:
        """Return which ceiling this run has *passed*, or ``None``.

        The counterpart to ``stop_reason_for``, and a different question.
        "May I start more work?" is answered by whether the allowance is used
        up; "did this run overrun?" is answered by whether it was exceeded.
        Asking the first one after a turn makes ``max_steps=1`` reject a run
        the model finished in one step -- the allowance was spent exactly, not
        overspent, and throwing the answer away is not what the caller asked
        for.

        Tokens and cost are different in kind: a model that reported 120 when
        the ceiling was 1 really did overrun, and no earlier check could have
        prevented it, because what a call will cost is unknowable before making
        it.
        """

        if usage.steps > self.max_steps:
            return "max_steps"
        if usage.tool_calls > self.max_tool_calls:
            return "max_tool_calls"
        if self.max_total_tokens is not None and usage.tokens.total > (
            self.max_total_tokens
        ):
            return "token_budget"
        if self.max_cost_micro_usd is not None and usage.cost_micro_usd > (
            self.max_cost_micro_usd
        ):
            return "cost_budget"
        if self.deadline is not None and now is not None and now >= self.deadline:
            return "deadline"
        return None

    def stop_reason_for(
        self,
        usage: BudgetUsage,
        *,
        now: datetime | None = None,
    ) -> StopReason | None:
        """Return why the run must stop, or ``None`` to continue.

        Evaluated before starting more work, never after: a budget that only
        triggers once it has been overrun is not a ceiling. ``now`` is passed
        in rather than read from the system clock so budget tests stay
        deterministic.
        """

        if usage.steps >= self.max_steps:
            return "max_steps"
        if usage.tool_calls >= self.max_tool_calls:
            return "max_tool_calls"
        if self.max_total_tokens is not None and usage.tokens.total >= (
            self.max_total_tokens
        ):
            return "token_budget"
        if self.max_cost_micro_usd is not None and usage.cost_micro_usd >= (
            self.max_cost_micro_usd
        ):
            return "cost_budget"
        if self.deadline is not None and now is not None and now >= self.deadline:
            return "deadline"
        return None


class TraceContext(DomainModel):
    """Position of a run in the ``thread -> node -> run`` hierarchy.

    Chat runs carry neither a thread nor a node; task runs carry both. Model
    call and tool call ids sit one level below and belong to individual events.
    """

    agent_run_id: Identifier
    task_id: Identifier | None = None
    workflow_thread_id: Identifier | None = None
    graph_node_id: Identifier | None = None
    parent_agent_run_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_hierarchy(self) -> TraceContext:
        if self.graph_node_id is not None and (
            self.workflow_thread_id is None or self.task_id is None
        ):
            raise ValueError("a graph node exists only inside a task workflow thread")
        return self


class AgentRunRequest(VersionedModel):
    """Everything one agent run needs, resolved by the caller.

    The request names a model profile, never a model id: which concrete model a
    profile maps to is settings' decision, so a request cannot select an
    unreviewed model.
    """

    trace: TraceContext
    run_kind: RunKind
    stream_id: Identifier
    principal: PrincipalContext
    envelope: AuthorizationEnvelope
    budget: RunBudget
    model_profile: ModelProfileName = "main"
    system_prompt: SystemPrompt = ""
    messages: tuple[Message, ...]
    tool_names: tuple[ToolName, ...] = ()
    context: ContextPacket | None = None

    @model_validator(mode="after")
    def validate_messages(self) -> AgentRunRequest:
        if not self.messages:
            raise ValueError("an agent run needs at least one message")
        if any(message.role == "system" for message in self.messages):
            # System content has exactly one home. Two sources would make the
            # effective instructions depend on adapter ordering.
            raise ValueError(
                "system content belongs to system_prompt, not to the message list"
            )
        if self.messages[-1].role not in {"user", "tool"}:
            raise ValueError("a run starts from a user message or from tool results")
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("tool_names must not repeat a tool")
        return self


class AgentOutcome(VersionedModel):
    """Structured result a graph node can store and route on.

    Large output lives in the artifact store; graph state keeps the reference.
    A run stopped by a budget is reported as failed, never as a quiet success:
    truncated work must not look complete to the node that consumes it.
    """

    agent_run_id: Identifier
    status: RunStatus
    stop_reason: StopReason
    output_text: BoundedText = ""
    output_ref: ArtifactRef | None = None
    citations: tuple[Citation, ...] = ()
    usage: BudgetUsage = BudgetUsage()
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def validate_terminal_consistency(self) -> AgentOutcome:
        if self.status == "completed":
            if self.stop_reason != "completed" or self.error is not None:
                raise ValueError(
                    "a completed outcome stops for completion and carries no error"
                )
        elif self.status == "cancelled":
            if self.stop_reason != "cancelled" or self.error is not None:
                raise ValueError(
                    "a cancelled outcome stops for cancellation and carries no error"
                )
        elif self.error is None or self.stop_reason in {"completed", "cancelled"}:
            raise ValueError(
                "a failed outcome carries an ErrorInfo and a failure stop reason"
            )
        return self


def stale_execution_outcome(run_id: str) -> AgentOutcome:
    """Return the one terminal outcome allowed for an expired execution lease.

    Lease expiry is a persisted fact shared by the request path, the recovery
    coordinator and every storage adapter. Keeping its shape in the domain
    prevents those writers from disagreeing about status, retryability or the
    public error code.
    """

    return AgentOutcome(
        agent_run_id=run_id,
        status="failed",
        stop_reason="deadline",
        error=ErrorInfo(
            code="stale_execution",
            message="execution lease expired",
            retryable=False,
        ),
    )


__all__ = [
    "TERMINAL_RUN_STATES",
    "AgentOutcome",
    "AgentRunRequest",
    "BudgetUsage",
    "ModelProfileName",
    "RunBudget",
    "RunKind",
    "RunState",
    "RunStatus",
    "StopReason",
    "SystemPrompt",
    "TokenUsage",
    "TraceContext",
    "stale_execution_outcome",
]
