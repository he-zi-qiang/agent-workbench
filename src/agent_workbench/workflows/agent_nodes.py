"""Agent-backed nodes of the fixed research graph.

A node reaches the model through ``AgentExecutor`` and through nothing else.
It holds no tool registry, no model port and no runtime internals, so the
project's one model-tool loop stays in one place: a node that could assemble
its own loop would be a second runtime with none of the first one's budget,
cancellation or policy guarantees.

The nodes here are the ones whose product is a stored artifact -- a framing,
a body of evidence, a draft.  Their contribution to the state is the reference
the run produced, which ``AgentOutcome`` already carries.  ``plan`` and
``critic`` instead need structured values decoded out of model output, so they
need a decoding contract and are deliberately not in this module yet.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.messages import Message
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    RunBudget,
    TraceContext,
)
from agent_workbench.domain.tasks import TaskNodeId, TaskState
from agent_workbench.domain.tools import ToolName
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.workflows.agent_profiles import (
    ProjectedContext,
    build_agent_request,
    profile_for,
    profile_with_mcp_tools,
    render_projection,
)
from agent_workbench.workflows.research_graph import (
    ResearchContribution,
    evolve,
    merge_refs,
)

# Nodes whose entire product is one stored artifact.  Membership is checked at
# call time so a node that grows a structured result cannot keep using the
# artifact-shaped path and silently drop that result.
ARTIFACT_PRODUCING_NODES: Final[frozenset[TaskNodeId]] = frozenset(
    {"understand", "research_internal", "research_external", "synthesize"}
)


class AgentNodeFailedError(RuntimeError):
    """Raised when an agent node's run did not produce usable content.

    The failure carries the state that already absorbed the run's cost, so a
    caller that turns this into a failed Task still records what was spent.
    Dropping the usage would let a retry loop spend an unbounded amount while
    the checkpoint claims nothing was spent.
    """

    def __init__(
        self,
        *,
        node: TaskNodeId,
        outcome: AgentOutcome,
        state: TaskState,
    ) -> None:
        self.node = node
        self.outcome = outcome
        self.state = state
        super().__init__(
            f"agent node {node} did not complete: "
            f"status={outcome.status} stop_reason={outcome.stop_reason}"
        )


@dataclass(frozen=True, slots=True)
class TaskRunContext:
    """Identity, authorization and budget for one Task's agent runs.

    Deliberately not part of ``TaskState``.  A checkpoint that carried a
    principal and an authorization envelope would let a resume replay whatever
    was authorized when the Task started; the Registry supplies this at claim
    time so every resume re-derives it.
    """

    trace: TraceContext
    stream_id: Identifier
    principal: PrincipalContext
    envelope: AuthorizationEnvelope
    budget: RunBudget


def node_prompt(node: TaskNodeId, state: TaskState) -> Message:
    """Project the state into the messages this node's run starts from.

    A projection, not a transcript, and the profile decides which one: the
    node sends what its agent admits and never the accumulated output of
    earlier nodes, because those live in the artifact store and copying them
    into a prompt makes context grow with the graph instead of with the
    question.

    Returns the first message. Kept for the callers that want just the
    projection text; a run's full message list comes from ``build_request``,
    which is the only thing that may add the writer's evidence block.
    """

    return render_projection(profile_for(node), state)[0]


def build_request(
    node: TaskNodeId,
    state: TaskState,
    context: TaskRunContext,
    offered: ProjectedContext | None = None,
    *,
    mcp_tool_names: Sequence[ToolName] = (),
) -> AgentRunRequest:
    """Assemble one node's run request under its agent's declared boundary."""

    return build_agent_request(
        profile_with_mcp_tools(profile_for(node), mcp_tool_names),
        state,
        trace=context.trace,
        stream_id=context.stream_id,
        principal=context.principal,
        envelope=context.envelope,
        budget=context.budget,
        offered=offered,
    )


RequestBuilder = Callable[[TaskNodeId, TaskState, TaskRunContext], AgentRunRequest]


@dataclass(frozen=True, slots=True)
class AgentNodeReport:
    """One node's run, and the state that has absorbed its cost.

    ``produced_ref`` is present only for a run that completed with an
    artifact.  Keeping the reference and the cost in separate fields is what
    makes "the run was charged for but produced nothing usable" expressible
    rather than rounded to either success or nothing-happened.
    """

    node: TaskNodeId
    outcome: AgentOutcome
    state: TaskState
    produced_ref: Identifier | None


class ArtifactProducingAgentNode:
    """Runs one agent node and records what the run cost and produced."""

    def __init__(
        self,
        executor: AgentExecutor,
        *,
        request_builder: RequestBuilder = build_request,
    ) -> None:
        self._executor = executor
        self._build_request = request_builder

    async def run(
        self,
        node: TaskNodeId,
        state: TaskState,
        context: TaskRunContext,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentNodeReport:
        """Execute ``node`` and return its report.

        The run's identifier and usage are recorded whether or not it
        succeeded: a failed run still consumed budget, and a node that only
        recorded successes would let the same Task retry forever inside a
        budget that never appears to move.
        """

        if node not in ARTIFACT_PRODUCING_NODES:
            raise ValueError(f"{node} is not an artifact-producing agent node")

        outcome = await self._executor.run(
            self._build_request(node, state, context),
            emit,
            cancellation,
        )
        charged = _absorb_cost(state, outcome)

        if outcome.status != "completed" or outcome.output_ref is None:
            # A completed run with no artifact is a failure, not an empty
            # success: producing the artifact is the whole job of these nodes,
            # and letting the graph continue would hand the next node an
            # objective with nothing behind it.
            raise AgentNodeFailedError(node=node, outcome=outcome, state=charged)

        return AgentNodeReport(
            node=node,
            outcome=outcome,
            state=charged,
            produced_ref=outcome.output_ref.artifact_id,
        )


def _absorb_cost(state: TaskState, outcome: AgentOutcome) -> TaskState:
    """Fold a run's identity and usage into the state, ignoring its content."""

    return evolve(
        state,
        agent_outcome_refs=merge_refs(
            state.agent_outcome_refs,
            (outcome.agent_run_id,),
        ),
        budget_usage=state.budget_usage.merged(outcome.usage).model_dump(),
    )


def research_contribution(report: AgentNodeReport) -> ResearchContribution:
    """Convert a research node's report into its fan-in contribution.

    The run reference is already in ``report.state``; the contribution carries
    it too so that a branch merged into a state that never saw the branch run
    -- the ordinary fan-in case -- still records which run produced it.
    """

    if report.node not in {"research_internal", "research_external"}:
        raise ValueError(f"{report.node} is not a research branch")
    if report.produced_ref is None:  # pragma: no cover - constructor guarantees
        raise ValueError("a research report must carry a produced reference")
    return ResearchContribution(
        evidence_refs=(report.produced_ref,),
        agent_outcome_refs=(report.outcome.agent_run_id,),
    )


def absorb_draft(report: AgentNodeReport) -> TaskState:
    """Store a synthesize report's artifact as the current draft.

    Any stored review is dropped with the draft it reviewed.  ``TaskState``
    requires a review to describe the current draft, and a review that
    outlived its draft would let the quality gate pass a rewrite nobody read.
    """

    if report.node != "synthesize":
        raise ValueError(f"{report.node} does not produce a draft")
    if report.produced_ref is None:  # pragma: no cover - constructor guarantees
        raise ValueError("a synthesize report must carry a produced reference")
    return evolve(
        report.state,
        draft_ref=report.produced_ref,
        review_result=None,
    )


__all__ = [
    "ARTIFACT_PRODUCING_NODES",
    "AgentNodeFailedError",
    "AgentNodeReport",
    "ArtifactProducingAgentNode",
    "RequestBuilder",
    "TaskRunContext",
    "absorb_draft",
    "build_request",
    "node_prompt",
    "research_contribution",
]
