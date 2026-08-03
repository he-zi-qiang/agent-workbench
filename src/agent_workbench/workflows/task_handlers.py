"""Structured, artifact-backed handlers for the fixed v1 Task graph.

This module has no model or tool loop of its own.  Every node calls the
injected AgentExecutor, and each invocation reconstructs its context from the
Task Registry so a resumed graph never inherits an old process's principal or
submitted envelope object.

One thing is deliberately *not* reconstructed: the claim. Identity is read
fresh because a resumed graph must run as whoever the Task belongs to now;
the lease is carried in from the Worker because it is the assertion that this
process is still the one entitled to run at all. Re-reading it turned a check
into a lookup -- whatever the Registry answered was, by construction, what a
fenced write was then compared against.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from agent_workbench.application.task_research import (
    EvidenceStore,
    InternalResearchService,
    TaskResearchContext,
)
from agent_workbench.domain.artifacts import ArtifactKind
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.evidence import EvidenceBundle
from agent_workbench.domain.identifiers import new_agent_run_id
from agent_workbench.domain.policies import ExecutionContext, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    RunBudget,
    TraceContext,
)
from agent_workbench.domain.tasks import (
    MAX_PLAN_STEPS,
    ReviewResult,
    TaskNodeId,
    TaskState,
    TaskStep,
)
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.ports.export import ReportExportPort
from agent_workbench.ports.research import (
    ExternalEvidenceSkipped,
    ExternalEvidenceToolPort,
)
from agent_workbench.ports.task_registry import ExecutionLease, TaskRegistry, TaskRun
from agent_workbench.workflows.agent_nodes import (
    ArtifactProducingAgentNode,
    TaskRunContext,
    build_request,
)
from agent_workbench.workflows.agent_profiles import ProjectedContext
from agent_workbench.workflows.execution_scope import TaskExecutionScope
from agent_workbench.workflows.research_graph import evolve, merge_refs

TaskNodeHandler = Callable[[TaskState], Awaitable[Mapping[str, Any]]]
EventSinkFactory = Callable[[TaskRunContext], EventSink]
CancellationFactory = Callable[[TaskRunContext], CancellationToken]
TaskPrincipalResolver = Callable[[TaskRun], PrincipalContext]


class StructuredOutputError(ValueError):
    """A model response is not the exact structured value its node requires."""


class TaskNodeRunFailedError(RuntimeError):
    """A structured node charged a run but could not produce valid state."""

    def __init__(
        self,
        *,
        node: TaskNodeId,
        outcome: AgentOutcome,
        state: TaskState,
        reason: str,
    ) -> None:
        self.node = node
        self.outcome = outcome
        self.state = state
        self.reason = reason
        super().__init__(f"task node {node} did not produce valid output: {reason}")


class TaskNodeContextUnavailableError(RuntimeError):
    """The graph state names no current Task Registry row."""


class TaskLeaseUnavailableError(RuntimeError):
    """This node is running outside any Worker's claim on the Task.

    Not a missing optional dependency. A node reached without a claim is one
    nothing authorized, and the honest answer is to refuse rather than to look
    up whichever Worker happens to hold the Task now.
    """


class TaskLeaseLostError(RuntimeError):
    """The Task moved to another claim while this Worker was executing it.

    Raised at the node rather than left to the fences downstream, because the
    node is about to spend a model budget and write artifacts on behalf of a
    Task this Worker no longer owns -- and because a fence that only refuses
    the final write lets everything before it happen twice.
    """


class TaskExportUnavailableError(RuntimeError):
    """This Worker has no way to export, so it must not settle the Task.

    The type name is the whole message a person will see: a failed Task records
    the exception's class and deliberately not its text, so these are named for
    what an operator has to fix rather than for where they were raised.
    """


class TaskExportPreconditionError(RuntimeError):
    """Export was reached without the approved draft it exports."""


class EvidenceAuthorizationChangedError(RuntimeError):
    """Internal evidence was no longer readable/current at synthesis time."""


class _PlanDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: tuple[TaskStep, ...] = Field(min_length=1, max_length=MAX_PLAN_STEPS)

    @model_validator(mode="after")
    def validate_as_task_plan(self) -> _PlanDocument:
        """Reuse the domain's contiguous-id/dependency invariant verbatim."""

        try:
            TaskState(
                task_id="plan_validation",
                objective="Validate planner output.",
                plan=self.steps,
            )
        except ValidationError as error:
            raise ValueError("planner steps do not form a valid task plan") from error
        return self


_PLAN_DOCUMENT = TypeAdapter(_PlanDocument)
_REVIEW_RESULT = TypeAdapter(ReviewResult)


@dataclass(frozen=True, slots=True)
class TaskNodeInvocation:
    """The non-checkpointed facts one node invocation receives."""

    context: TaskRunContext
    events: EventSink
    cancellation: CancellationToken
    # Which claim this node is running under. It is the lease the Worker was
    # given when it claimed the Task, carried through the graph invocation
    # unchanged -- never the Registry's current one, and never the checkpoint's.
    #
    # The Registry's current one is the trap. A Worker whose lease lapsed
    # mid-graph reads back the epoch of the Worker that replaced it, and every
    # fenced write it then performs passes: the ledger compares the epoch it is
    # handed against the live claim, so a Worker writing under somebody else's
    # epoch is indistinguishable from the somebody else. The fence that exists
    # to stop a displaced Worker was the thing being satisfied by re-reading.
    lease: ExecutionLease


@dataclass(frozen=True, slots=True)
class TaskNodeInvocationProvider:
    """Rebuild non-durable execution context for every graph-node call."""

    registry: TaskRegistry
    budget: RunBudget
    sink_for: EventSinkFactory
    cancellation_for: CancellationFactory
    principal_for: TaskPrincipalResolver
    # Where the claim comes from. The Worker enters it around the invocation;
    # this reads it back. A provider whose scope is empty resolves nothing,
    # because a node with no claim behind it has no authority to spend a budget
    # or write anything.
    scope: TaskExecutionScope = field(default_factory=TaskExecutionScope)

    async def resolve(self, state: TaskState, node: TaskNodeId) -> TaskNodeInvocation:
        lease = self.scope.current()
        if lease is None or lease.task_id != state.task_id:
            raise TaskLeaseUnavailableError(
                f"graph node {node} is running under no claim on {state.task_id}"
            )
        task = await self.registry.get(state.task_id)
        if task is None:
            raise TaskNodeContextUnavailableError(
                f"task context is unavailable for graph node {node}"
            )
        # Everything else here is read fresh, and this is checked fresh: the
        # principal and envelope must come from the current row, and the claim
        # must still be the one this Worker holds. Re-reading the epoch was the
        # defect; re-reading everything else is what stops a resumed graph from
        # running on a dead process's identity.
        if (
            task.status != "running"
            or task.lease_owner != lease.worker_id
            or task.lease_epoch != lease.epoch
        ):
            raise TaskLeaseLostError(
                f"graph node {node} holds epoch {lease.epoch} of {state.task_id}, "
                f"which is {task.status} at epoch {task.lease_epoch}"
            )
        context = _context_for(
            task,
            node=node,
            budget=self.budget,
            principal=self.principal_for(task),
        )
        return TaskNodeInvocation(
            context=context,
            events=self.sink_for(context),
            cancellation=self.cancellation_for(context),
            lease=lease,
        )


@dataclass(frozen=True, slots=True)
class TaskResearchHandlers:
    """Framework-neutral evidence capabilities injected into real handlers.

    Internal retrieval is a normal application service. External retrieval is
    intentionally a port whose adapter must enter the runtime ToolGateway, so
    no graph node can bypass schema validation, policy or audit events.
    """

    internal: InternalResearchService
    evidence: EvidenceStore
    external: ExternalEvidenceToolPort
    policy_identity: str


@dataclass(frozen=True, slots=True)
class TaskExportHandlers:
    """The write half of the graph, injected on the same terms as the read half.

    Separate from :class:`TaskResearchHandlers` because it is optional in a way
    research is not: a deployment with no ledger may run every read node, and
    must not be able to run this one.  Absent means the export node fails the
    Task rather than exporting unrecorded.
    """

    export: ReportExportPort
    policy_identity: str


class BoundedParallelExecutor:
    """Run at most this many agent invocations at once.

    ``multi_agent.max_parallel_agent_invocations`` described a ceiling nothing
    applied: the fixed graph fans out to two researchers, LangGraph runs them
    concurrently, and the number in the configuration was a description of what
    the graph happened to do rather than a bound on it. A third branch would
    have raised the real parallelism and left the setting reading the same.

    It wraps the executor rather than the node, because what costs money is an
    invocation. A semaphore here bounds every agent the graph runs, including
    ones a later fan-out adds without anybody revisiting this file.
    """

    def __init__(self, executor: AgentExecutor, *, max_parallel: int) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self._executor = executor
        self._slots = asyncio.Semaphore(max_parallel)

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        async with self._slots:
            return await self._executor.run(request, emit, cancellation)


class ArtifactPersistingExecutor:
    """Persist a completed text-only outcome without changing Executor's port."""

    def __init__(self, executor: AgentExecutor, *, artifacts: ArtifactStore) -> None:
        self._executor = executor
        self._artifacts = artifacts

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        outcome = await self._executor.run(request, emit, cancellation)
        if (
            outcome.status != "completed"
            or outcome.output_ref is not None
            or not outcome.output_text
        ):
            return outcome
        try:
            reference = await self._artifacts.put(
                tenant_id=request.principal.tenant_id,
                owner_id=request.principal.principal_id,
                kind=_artifact_kind(request),
                media_type="text/markdown",
                content=outcome.output_text.encode("utf-8"),
            )
        except Exception as error:
            return AgentOutcome(
                agent_run_id=outcome.agent_run_id,
                status="failed",
                stop_reason="error",
                usage=outcome.usage,
                error=ErrorInfo.from_exception(error),
            )
        return AgentOutcome(
            agent_run_id=outcome.agent_run_id,
            status=outcome.status,
            stop_reason=outcome.stop_reason,
            output_text=outcome.output_text,
            output_ref=reference,
            citations=outcome.citations,
            usage=outcome.usage,
        )


def build_task_v1_handlers(
    *,
    executor: AgentExecutor,
    artifacts: ArtifactStore,
    invocations: TaskNodeInvocationProvider,
    research: TaskResearchHandlers | None = None,
    export: TaskExportHandlers | None = None,
) -> dict[TaskNodeId, TaskNodeHandler]:
    """Build every v1 model-invoking handler around one AgentExecutor.

    Approval stays a graph-control node: it has to interrupt, and interrupting
    belongs to the workflow framework, so it is assembled where that framework
    is.  Export is here because it is not graph control -- it is the one node
    that writes.
    """

    persisting_executor = ArtifactPersistingExecutor(executor, artifacts=artifacts)
    artifact_node = ArtifactProducingAgentNode(
        persisting_executor,
        request_builder=build_request,
    )

    def artifact_handler(node: TaskNodeId) -> TaskNodeHandler:
        async def run(state: TaskState) -> Mapping[str, Any]:
            invocation = await invocations.resolve(state, node)
            report = await artifact_node.run(
                node,
                state,
                invocation.context,
                invocation.events,
                invocation.cancellation,
            )
            update = _outcome_update(report.outcome)
            if node in {"research_internal", "research_external"}:
                if report.produced_ref is None:  # pragma: no cover
                    raise AssertionError("a completed research node has no artifact")
                update["evidence_refs"] = (report.produced_ref,)
            elif node == "synthesize":
                if report.produced_ref is None:  # pragma: no cover
                    raise AssertionError("a completed synthesis node has no artifact")
                update.update(draft_ref=report.produced_ref, review_result=None)
            return update

        return run

    async def research_internal(state: TaskState) -> Mapping[str, Any]:
        if research is None:
            return await artifact_handler("research_internal")(state)
        # The fixed v1 graph always fans out, but a general Task need not name
        # a knowledge base. That absence is not an ACL failure and produces no
        # synthetic "internal evidence" artifact.
        if state.knowledge_base_id is None:
            return {}
        invocation = await invocations.resolve(state, "research_internal")
        invocation.cancellation.raise_if_cancelled()
        reference = await research.internal.gather(
            context=_research_context(state, invocation), query=state.objective
        )
        invocation.cancellation.raise_if_cancelled()
        return {"evidence_refs": (reference.artifact_id,)}

    async def research_external(state: TaskState) -> Mapping[str, Any]:
        if research is None:
            return await artifact_handler("research_external")(state)
        invocation = await invocations.resolve(state, "research_external")
        execution = _tool_execution_context(
            invocation, policy_identity=research.policy_identity
        )
        reference = await research.external.gather(
            query=state.objective,
            task_id=state.task_id,
            principal=invocation.context.principal,
            execution=execution,
            sink=invocation.events,
            cancellation=invocation.cancellation,
        )
        if isinstance(reference, ExternalEvidenceSkipped):
            # Gateway already recorded the proposal and refusal. A missing
            # optional public-search provider is similarly visible there; no
            # artifact is invented merely to make the fan-in non-empty.
            return {}
        return {"evidence_refs": (reference.artifact_id,)}

    async def synthesize(state: TaskState) -> Mapping[str, Any]:
        if research is None:
            return await artifact_handler("synthesize")(state)
        invocation = await invocations.resolve(state, "synthesize")
        bundles = await _load_evidence(state, invocation, research)
        await _confirm_internal_evidence(bundles, invocation, research)
        synthesis_node = ArtifactProducingAgentNode(
            persisting_executor,
            request_builder=lambda node, task_state, context: _synthesis_request(
                node, task_state, context, bundles
            ),
        )
        report = await synthesis_node.run(
            "synthesize",
            state,
            invocation.context,
            invocation.events,
            invocation.cancellation,
        )
        try:
            await _confirm_internal_evidence(bundles, invocation, research)
        except EvidenceAuthorizationChangedError as error:
            raise TaskNodeRunFailedError(
                node="synthesize",
                outcome=report.outcome,
                state=report.state,
                reason="internal evidence authorization changed during synthesis",
            ) from error
        if report.produced_ref is None:  # pragma: no cover
            raise AssertionError("a completed synthesis node has no artifact")
        return _outcome_update(report.outcome) | {
            "draft_ref": report.produced_ref,
            "review_result": None,
        }

    async def plan(state: TaskState) -> Mapping[str, Any]:
        invocation = await invocations.resolve(state, "plan")
        outcome = await executor.run(
            _structured_request("plan", state, invocation.context),
            invocation.events,
            invocation.cancellation,
        )
        charged = _charged_state(state, outcome)
        _require_completed("plan", outcome, charged)
        try:
            steps = decode_plan_output(outcome.output_text)
        except StructuredOutputError as error:
            raise TaskNodeRunFailedError(
                node="plan",
                outcome=outcome,
                state=charged,
                reason="planner JSON did not satisfy the plan schema",
            ) from error
        return _outcome_update(outcome) | {
            "plan": tuple(step.model_dump() for step in steps),
        }

    async def critic(state: TaskState) -> Mapping[str, Any]:
        invocation = await invocations.resolve(state, "critic")
        outcome = await executor.run(
            _structured_request("critic", state, invocation.context),
            invocation.events,
            invocation.cancellation,
        )
        charged = _charged_state(state, outcome)
        _require_completed("critic", outcome, charged)
        try:
            review = decode_review_output(outcome.output_text, state=state)
        except StructuredOutputError as error:
            raise TaskNodeRunFailedError(
                node="critic",
                outcome=outcome,
                state=charged,
                reason="critic JSON did not satisfy the review schema",
            ) from error
        return _outcome_update(outcome) | {"review_result": review.model_dump()}

    async def export_report(state: TaskState) -> Mapping[str, Any]:
        if export is None:
            # Not a passthrough. A graph that reached export has a human's
            # approval behind it, and quietly settling that Task as succeeded
            # with nothing exported is the failure mode this node exists to
            # make impossible.
            raise TaskExportUnavailableError(
                "this Worker assembled no export capability"
            )
        if state.draft_ref is None or state.approval_id is None:
            raise TaskExportPreconditionError("export requires an approved draft")
        invocation = await invocations.resolve(state, "export")
        artifact_id = await export.export.export(
            draft_ref=state.draft_ref,
            approval_id=state.approval_id,
            execution=_tool_execution_context(
                invocation, policy_identity=export.policy_identity
            ),
            sink=invocation.events,
            cancellation=invocation.cancellation,
        )
        return {"export_ref": artifact_id}

    return {
        "understand": artifact_handler("understand"),
        "plan": plan,
        "research_internal": research_internal,
        "research_external": research_external,
        "synthesize": synthesize,
        "critic": critic,
        "export": export_report,
    }


def _research_context(
    state: TaskState, invocation: TaskNodeInvocation
) -> TaskResearchContext:
    return TaskResearchContext(
        task_id=state.task_id,
        principal=invocation.context.principal,
        knowledge_base_id=state.knowledge_base_id,
    )


def _tool_execution_context(
    invocation: TaskNodeInvocation, *, policy_identity: str
) -> ExecutionContext:
    context = invocation.context
    return ExecutionContext(
        principal=context.principal,
        envelope=context.envelope,
        agent_run_id=context.trace.agent_run_id,
        policy_identity=policy_identity,
        task_id=context.trace.task_id,
        workflow_thread_id=context.trace.workflow_thread_id,
        graph_node_id=context.trace.graph_node_id,
        # Without this the side-effect ledger has nothing to fence against, and
        # refuses every ledgered tool rather than recording one unfenced. It is
        # the claimed epoch, so the fence compares this Worker's ownership
        # against the Task's -- which is the comparison it was written to make.
        lease_epoch=invocation.lease.epoch,
    )


async def _load_evidence(
    state: TaskState,
    invocation: TaskNodeInvocation,
    research: TaskResearchHandlers,
) -> tuple[EvidenceBundle, ...]:
    """Load every reference under the freshly resolved Task identity."""

    context = _research_context(state, invocation)
    bundles: list[EvidenceBundle] = []
    for artifact_id in state.evidence_refs:
        invocation.cancellation.raise_if_cancelled()
        bundles.append(
            await research.evidence.load(context=context, artifact_id=artifact_id)
        )
    return tuple(bundles)


async def _confirm_internal_evidence(
    bundles: tuple[EvidenceBundle, ...],
    invocation: TaskNodeInvocation,
    research: TaskResearchHandlers,
) -> None:
    """Revalidate the read authority before and after model exposure."""

    for bundle in bundles:
        if bundle.source != "internal":
            continue
        invocation.cancellation.raise_if_cancelled()
        if not await research.internal.confirm_current(
            context=TaskResearchContext(
                task_id=bundle.task_id,
                principal=invocation.context.principal,
            ),
            bundle=bundle,
        ):
            raise EvidenceAuthorizationChangedError(
                "internal evidence is no longer authorized or current"
            )


def _synthesis_request(
    node: TaskNodeId,
    state: TaskState,
    context: TaskRunContext,
    bundles: tuple[EvidenceBundle, ...],
) -> AgentRunRequest:
    """The writer's run, with the evidence its profile is the only one to admit.

    Offered through the projection rather than appended afterwards: a node that
    could add messages to a finished request could add them to any agent's, and
    "the researchers never see each other's findings" would again be a property
    of who happened to call what.
    """

    return build_request(node, state, context, ProjectedContext(evidence=bundles))


def decode_plan_output(text: str) -> tuple[TaskStep, ...]:
    """Decode exactly one strict JSON planner document, or fail closed."""

    # Parse once ourselves first: Pydantic's JSON parser intentionally accepts
    # duplicate object keys with last-key-wins semantics, which is not a safe
    # protocol for a model response. ``validate_json`` then retains JSON-mode
    # strictness while accepting JSON arrays for the tuple fields in the domain
    # model (``validate_python(..., strict=True)`` would reject those arrays).
    _json_object(text)
    try:
        return _PLAN_DOCUMENT.validate_json(text, strict=True).steps
    except ValidationError as error:
        raise StructuredOutputError("planner output has an invalid shape") from error


def decode_review_output(text: str, *, state: TaskState) -> ReviewResult:
    """Decode a critic result bound to the current draft and revision."""

    if state.draft_ref is None:
        raise StructuredOutputError("critic ran before synthesis produced a draft")
    _json_object(text)
    try:
        review = _REVIEW_RESULT.validate_json(text, strict=True)
    except ValidationError as error:
        raise StructuredOutputError("critic output has an invalid shape") from error
    if review.reviewed_draft_ref != state.draft_ref:
        raise StructuredOutputError("critic reviewed a different draft")
    if review.revision_number != state.revision_count:
        raise StructuredOutputError("critic reviewed a different revision")
    return review


def _json_object(text: str) -> dict[str, Any]:
    """Reject fences, tails, duplicate keys and non-standard JSON constants."""

    if not text or not text.lstrip().startswith("{") or "```" in text:
        raise StructuredOutputError("structured output must be one JSON object")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise StructuredOutputError("structured output is not valid JSON") from error
    if not isinstance(value, dict):
        raise StructuredOutputError("structured output must be a JSON object")
    return cast("dict[str, Any]", value)


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredOutputError("structured output has duplicate object keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise StructuredOutputError(f"invalid JSON constant: {value}")


def _context_for(
    task: TaskRun,
    *,
    node: TaskNodeId,
    budget: RunBudget,
    principal: PrincipalContext,
) -> TaskRunContext:
    return TaskRunContext(
        trace=TraceContext(
            agent_run_id=new_agent_run_id(),
            task_id=task.task_id,
            workflow_thread_id=task.thread_id,
            graph_node_id=node,
        ),
        stream_id=task.thread_id,
        principal=principal,
        envelope=task.submitted_authorization_envelope,
        budget=budget,
    )


def _artifact_kind(request: AgentRunRequest) -> ArtifactKind:
    return "report" if request.trace.graph_node_id == "synthesize" else "agent_outcome"


def _structured_request(
    node: TaskNodeId,
    state: TaskState,
    context: TaskRunContext,
) -> AgentRunRequest:
    """The planner's and the critic's runs, through the same boundary as the rest.

    Their prompts are JSON contracts rather than instructions in prose, which is
    why they used to be assembled here instead of with the other agents. That
    made them the two runs whose admitted context nothing declared -- and the
    critic is exactly the agent whose isolation matters most, since a critic
    that could read the evidence would be reviewing the research instead of the
    writing.
    """

    if node == "critic" and state.draft_ref is None:
        raise StructuredOutputError("critic requires a current draft reference")
    return build_request(
        node,
        state,
        context,
        ProjectedContext(
            draft_ref=state.draft_ref if node == "critic" else None,
            revision_number=state.revision_count if node == "critic" else None,
        ),
    )


def _outcome_update(outcome: AgentOutcome) -> dict[str, Any]:
    """Return this node's deltas so parallel research can be merged safely."""

    return {
        "agent_outcome_refs": (outcome.agent_run_id,),
        "budget_usage": outcome.usage.model_dump(),
    }


def _charged_state(state: TaskState, outcome: AgentOutcome) -> TaskState:
    return evolve(
        state,
        agent_outcome_refs=merge_refs(
            state.agent_outcome_refs, (outcome.agent_run_id,)
        ),
        budget_usage=state.budget_usage.merged(outcome.usage).model_dump(),
    )


def _require_completed(
    node: TaskNodeId,
    outcome: AgentOutcome,
    charged: TaskState,
) -> None:
    if outcome.status != "completed":
        raise TaskNodeRunFailedError(
            node=node,
            outcome=outcome,
            state=charged,
            reason="agent executor did not complete",
        )


__all__ = [
    "ArtifactPersistingExecutor",
    "BoundedParallelExecutor",
    "CancellationFactory",
    "EventSinkFactory",
    "EvidenceAuthorizationChangedError",
    "StructuredOutputError",
    "TaskExportHandlers",
    "TaskExportPreconditionError",
    "TaskExportUnavailableError",
    "TaskLeaseLostError",
    "TaskLeaseUnavailableError",
    "TaskNodeContextUnavailableError",
    "TaskNodeHandler",
    "TaskNodeInvocation",
    "TaskNodeInvocationProvider",
    "TaskNodeRunFailedError",
    "TaskPrincipalResolver",
    "TaskResearchHandlers",
    "build_task_v1_handlers",
    "decode_plan_output",
    "decode_review_output",
]
