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
    """Token accounting for one run, including cache traffic.

    ``input_tokens`` is the whole prompt, and ``cache_read_tokens`` is the part
    of that prompt which was served from cache -- a subset, not a separate
    stream of tokens. That is the providers' convention rather than this
    project's: DeepSeek reports ``prompt_tokens`` alongside
    ``prompt_cache_hit_tokens``, and the adapter passes both through unchanged.
    ``cache_write_tokens`` is the exception, reported outside the prompt count,
    and so the one cache figure that is genuinely additive.

    Adapters must preserve that convention. An adapter that reported only the
    cache *miss* as ``input_tokens`` would silently unprice its own run:
    ``ModelPrices.cost_micro_usd`` subtracts the hit from the prompt to find
    what to charge at the full input rate, and against an already-exclusive
    count that subtraction clamps to zero and bills nothing for the uncached
    prompt.
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        """Every token this run moved, counting each of them once.

        Adding ``cache_read_tokens`` to ``input_tokens`` would count the cached
        prompt twice, and prompt caching is on by default -- so the inflated
        figure was what ``max_total_tokens`` and the ``token_budget`` stop
        reason actually saw. On the pinned DeepSeek turn, a 142-token run
        reported 206 and could be killed as over budget with a quarter of its
        ceiling still unspent. Caching must change what a prompt costs, never
        how long the run is judged to be.

        Decomposed exactly as ``ModelPrices.cost_micro_usd`` decomposes the
        same usage, including the clamp for a cache report larger than the
        prompt it belongs to: the two answer different questions about one
        turn, and must not disagree about what that turn contained.
        """

        uncached_input = max(0, self.input_tokens - self.cache_read_tokens)
        return (
            uncached_input
            + self.cache_read_tokens
            + self.output_tokens
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
    """Hard ceilings for one run.

    ``max_tool_calls`` may sit *below* ``max_steps``, and that combination is a
    budget rather than a mistake (ADR-022). It says "N tool calls, and a turn
    left over to write the answer from them": once the allowance is gone the
    loop stops advertising tools, so the extra steps are answering turns, not
    steps that mysteriously cannot call anything.

    An earlier cross-field validator forbade it, on the reading that a step
    unable to call a tool was a misconfiguration. That reading came from a loop
    which *ended* the run when tool calls ran out, and under it the two
    ceilings could never be expressed independently: one call per turn reaches
    both at the same turn, ``max_steps`` is reported first, and so a tool
    ceiling below the step ceiling was unreachable by construction. Measured on
    the chat web fallback -- the budget said "at most two searches", the run
    spent step three proposing a third, and died holding 5.5KB of results it
    never wrote a word from.
    """

    # The domain ceiling was 100, from a time when steps were how a run was
    # budgeted. ADR-030 moves that job to cost and deadline, which grow with
    # the work rather than with the number of turns it took, and leaves this as
    # a backstop against a loop that will not terminate. A backstop belongs far
    # above any real run: at 100 a node that edits a file, greps for the next
    # site and edits again is stopped mid-task by the guard rather than by its
    # budget, and the symptom -- "the tools all work, it just never finishes"
    # -- reads as the model being incapable.
    max_steps: int = Field(ge=1, le=1000)
    max_tool_calls: int = Field(ge=1, le=500)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_cost_micro_usd: int | None = Field(default=None, ge=1)
    deadline: AwareDatetime | None = None

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

    def halt_reason_for(
        self,
        usage: BudgetUsage,
        *,
        now: datetime | None = None,
    ) -> StopReason | None:
        """Return why the run must **end**, or ``None`` to take another turn.

        The third of the three questions, and the one that separates a ceiling
        on tools from a ceiling on the run. Every limit here ends the run
        because there is no work left it could usefully do: no steps, no
        tokens, no money, no time. ``max_tool_calls`` is deliberately absent --
        a run that has spent its tool allowance can still write its answer from
        what the tools already returned, and it needs a turn to do it.

        Measured, on the chat shape that made this distinction necessary: a run
        that searched twice, hit its tool ceiling and was stopped there
        discarded 5.5KB of successful search results and answered "I have no
        ability to search". Asking ``stop_reason_for`` here -- "may I start
        more work?" -- makes a spent allowance indistinguishable from a spent
        run, and throws away everything the allowance bought.

        Evaluated before a turn, on the same reasoning as ``stop_reason_for``.
        """

        if usage.steps >= self.max_steps:
            return "max_steps"
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

    def stop_reason_for(
        self,
        usage: BudgetUsage,
        *,
        now: datetime | None = None,
    ) -> StopReason | None:
        """Return why the run may not start more work, or ``None`` to continue.

        ``halt_reason_for`` plus the tool ceiling. That is the whole difference
        between the two, and which one a caller wants follows from what it is
        about to do: this one guards *dispatching a tool call*, the other
        guards *taking another turn at all*. Reaching for this one before a
        model turn is what stops a run one step short of the answer its own
        tool calls paid for.

        Evaluated before starting more work, never after: a budget that only
        triggers once it has been overrun is not a ceiling. ``now`` is passed
        in rather than read from the system clock so budget tests stay
        deterministic.
        """

        if usage.steps >= self.max_steps:
            return "max_steps"
        if usage.tool_calls >= self.max_tool_calls:
            return "max_tool_calls"
        return self.halt_reason_for(usage, now=now)

    def tool_allowance_spent(self, usage: BudgetUsage) -> bool:
        """Whether this run may still dispatch a tool call.

        Read by the runtime to decide what to *advertise*, which is a different
        act from deciding whether to refuse a proposal. A model offered a tool
        it can no longer call will propose it, and the proposal has to be
        turned away -- so the honest thing is to stop offering it, and let the
        model answer from what it has.
        """

        return usage.tool_calls >= self.max_tool_calls


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
