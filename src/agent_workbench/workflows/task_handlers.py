"""Structured, artifact-backed handlers for both Task graphs.

One factory builds every node either graph runs, and two selectors name which
subset each graph needs (ADR-031 §3: share the node implementations rather than
copy them).  Three of the nodes are literally shared -- ``understand`` and
``export`` mean the same thing in both, and both graphs' reviewing nodes decode
the same verdict -- so a cross-cutting change lands on both graphs or on
neither, which is the failure mode a second graph was expected to introduce.

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
from collections.abc import Awaitable, Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

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
from agent_workbench.application.workspace import TaskWorkspace, WorkspaceSession
from agent_workbench.domain.artifacts import ArtifactKind
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.evidence import (
    MAX_EVIDENCE_ITEMS,
    EvidenceBundle,
    EvidenceItem,
    EvidenceText,
    EvidenceUrl,
    ExternalTitle,
)
from agent_workbench.domain.identifiers import new_agent_run_id, new_id
from agent_workbench.domain.messages import Message
from agent_workbench.domain.policies import ExecutionContext, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
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
from agent_workbench.domain.tools import ToolName
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
from agent_workbench.workflows.agent_profiles import (
    WORKSPACE_TOOLS,
    DynamicToolSource,
    ProjectedContext,
    profile_for,
)
from agent_workbench.workflows.execution_scope import TaskExecutionScope
from agent_workbench.workflows.research_graph import evolve, merge_refs

# Re-exported names: the decode boundary moved to `structured_output` when
# ADR-036 gave it a second caller, and every existing import of the two error
# classes from this module must keep meaning the same classes.
from agent_workbench.workflows.structured_output import (
    StructuredOutputError,
    StructuredOutputFramingError,
    json_object,
    restatement_messages,
)
from agent_workbench.workflows.workspace_scope import WorkspaceScope

TaskNodeHandler = Callable[[TaskState], Awaitable[Mapping[str, Any]]]
EventSinkFactory = Callable[[TaskRunContext], EventSink]
CancellationFactory = Callable[[TaskRunContext], CancellationToken]
TaskPrincipalResolver = Callable[[TaskRun], PrincipalContext]


def _utc_now() -> datetime:
    return datetime.now(UTC)


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


class _ExternalEvidenceItem(BaseModel):
    """One source the external researcher says it actually read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: EvidenceUrl
    title: ExternalTitle
    text: EvidenceText


class _ExternalEvidenceDocument(BaseModel):
    """The external researcher's whole answer.

    Empty is a permitted answer, and is not the same as a malformed one: a
    branch that read nothing it could stand behind contributes no evidence,
    while a branch whose output does not parse is a node that failed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[_ExternalEvidenceItem, ...] = Field(max_length=MAX_EVIDENCE_ITEMS)


_PLAN_DOCUMENT = TypeAdapter(_PlanDocument)
_REVIEW_RESULT = TypeAdapter(ReviewResult)
_EXTERNAL_EVIDENCE_DOCUMENT = TypeAdapter(_ExternalEvidenceDocument)


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
    # Wall clock for one attempt (ADR-030), or ``None`` to leave the budget's
    # deadline alone. Applied here rather than baked into ``budget`` because a
    # deadline is an instant and ``budget`` is built once at composition: a
    # fixed one would start expiring while the process was still booting, and
    # every invocation after it would be born already overdue.
    max_seconds_per_invocation: int | None = None
    clock: Callable[[], datetime] = field(default=_utc_now)

    def _budget_for_this_attempt(self) -> RunBudget:
        if self.max_seconds_per_invocation is None:
            return self.budget
        return self.budget.model_copy(
            update={
                "deadline": self.clock()
                + timedelta(seconds=self.max_seconds_per_invocation)
            }
        )

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
            budget=self._budget_for_this_attempt(),
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


class BudgetedAgentExecutor:
    """Charge the Task for each agent invocation before running it.

    ADR-040. ``multi_agent.max_agent_invocation_attempts_per_task`` has been
    declared since the settings module existed and has never had a reader,
    because honouring it needs a counter that survives a retry and a reclaim --
    and a number handed to a process is not that.

    **This layer does not refuse anything.** It records, and the count is
    readable on the Task. That is deliberate and it is the middle of ADR-040's
    three steps: a ceiling whose first observable effect is a Task going
    terminal is, to whoever is on call at the time, indistinguishable from a
    bug. The number becomes visible before it ever becomes fatal.

    It wraps the executor rather than the node, for the reason
    ``BoundedParallelExecutor`` gives: what costs money is an invocation, and a
    later fan-out gets counted without anybody revisiting this file. It wraps
    *outside* the parallelism bound so the Registry round trip happens before a
    concurrency slot is taken rather than while one is held.

    A run with no lease in scope is not charged and not refused. That
    combination exists only where nothing claimed the Task -- narrow tests and
    the demo handlers -- and inventing an authority to bill would be worse than
    not billing.
    """

    def __init__(
        self,
        executor: AgentExecutor,
        *,
        registry: TaskRegistry,
        scope: TaskExecutionScope,
    ) -> None:
        self._executor = executor
        self._registry = registry
        self._scope = scope

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        lease = self._scope.current()
        if lease is not None:
            # Before the call, not after. A loop that dies inside every
            # invocation would never reach an after-the-fact write, and that
            # loop is the one the ceiling exists for. StaleExecutionError
            # propagates untouched: a Worker that lost its claim must stop,
            # not run an invocation it can no longer be charged for.
            await self._registry.reserve_agent_invocation(lease)
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


#: Which nodes each graph needs a handler for. ``approval`` is in neither: it
#: has to interrupt, and interrupting belongs to the workflow framework, so it
#: is assembled where that framework is. The routing nodes are absent for the
#: opposite reason -- they decide rather than run.
V1_HANDLER_NODES: Final[tuple[TaskNodeId, ...]] = (
    "understand",
    "plan",
    "research_internal",
    "research_external",
    "synthesize",
    "critic",
    "export",
)
V2_HANDLER_NODES: Final[tuple[TaskNodeId, ...]] = (
    "understand",
    "work",
    "review",
    "export",
)


def build_task_v1_handlers(
    *,
    executor: AgentExecutor,
    artifacts: ArtifactStore,
    invocations: TaskNodeInvocationProvider,
    research: TaskResearchHandlers | None = None,
    export: TaskExportHandlers | None = None,
    dynamic_tools: Mapping[DynamicToolSource, tuple[ToolName, ...]] | None = None,
    workspace_scope: WorkspaceScope | None = None,
) -> dict[TaskNodeId, TaskNodeHandler]:
    """The v1 graph's handlers, from the shared implementations.

    Both public builders select from one factory rather than each holding a
    copy, which is what ADR-031 §3 asks for: three of the two graphs' handler
    nodes are literally the same node, and the cost the ADR names -- a
    cross-cutting change landing on one graph and not the other -- is paid at
    exactly the places two copies would have diverged.
    """

    return _select(
        build_task_handlers(
            executor=executor,
            artifacts=artifacts,
            invocations=invocations,
            research=research,
            export=export,
            dynamic_tools=dynamic_tools,
            workspace_scope=workspace_scope,
        ),
        V1_HANDLER_NODES,
    )


def build_task_v2_handlers(
    *,
    executor: AgentExecutor,
    artifacts: ArtifactStore,
    invocations: TaskNodeInvocationProvider,
    research: TaskResearchHandlers | None = None,
    export: TaskExportHandlers | None = None,
    dynamic_tools: Mapping[DynamicToolSource, tuple[ToolName, ...]] | None = None,
    workspace_scope: WorkspaceScope | None = None,
) -> dict[TaskNodeId, TaskNodeHandler]:
    """The v2 general graph's handlers, from the same implementations.

    ``understand`` and ``export`` come from the same factory code v1's do --
    and a composition that calls :func:`build_task_handlers` once hands both
    graphs literally the same handler objects. That is the point of them
    sharing a node id (ADR-031 §2.1): framing an objective and performing the
    Task's one approved write do not become different operations because the
    topology around them did.

    ``research`` is accepted and unused by v2's own nodes, because v2 has no
    research branch. It stays in the signature so one composition can build
    both graphs' handlers from one set of dependencies.
    """

    return _select(
        build_task_handlers(
            executor=executor,
            artifacts=artifacts,
            invocations=invocations,
            research=research,
            export=export,
            dynamic_tools=dynamic_tools,
            workspace_scope=workspace_scope,
        ),
        V2_HANDLER_NODES,
    )


def _select(
    handlers: Mapping[TaskNodeId, TaskNodeHandler], nodes: tuple[TaskNodeId, ...]
) -> dict[TaskNodeId, TaskNodeHandler]:
    """The named subset, refusing to quietly return a graph a node short.

    A missing handler is not an absent capability: the graph adapter defaults an
    unsupplied node to a pass-through, so a typo here would produce a Task that
    ran its whole graph, did nothing at the node that was supposed to do the
    work, and reported success.
    """

    missing = tuple(node for node in nodes if node not in handlers)
    if missing:  # pragma: no cover - a constant disagreeing with the factory
        raise AssertionError(f"the handler factory built no {', '.join(missing)}")
    return {node: handlers[node] for node in nodes}


def build_task_handlers(
    *,
    executor: AgentExecutor,
    artifacts: ArtifactStore,
    invocations: TaskNodeInvocationProvider,
    research: TaskResearchHandlers | None = None,
    export: TaskExportHandlers | None = None,
    dynamic_tools: Mapping[DynamicToolSource, tuple[ToolName, ...]] | None = None,
    workspace_scope: WorkspaceScope | None = None,
) -> dict[TaskNodeId, TaskNodeHandler]:
    """Build every model-invoking handler either graph uses, around one executor.

    Approval stays a graph-control node: it has to interrupt, and interrupting
    belongs to the workflow framework, so it is assembled where that framework
    is.  Export is here because it is not graph control -- it is the one node
    that writes.
    """

    # What this Worker actually registered for the research audience, which is
    # what decides whether `research_external` has anything to run a tool loop
    # with. Read once here rather than per Task: the catalog is frozen at
    # assembly, and a node that re-derived it could disagree with the profile
    # the same request was built from.
    research_tool_catalog = tuple((dynamic_tools or {}).get("research", ()))
    persisting_executor = ArtifactPersistingExecutor(executor, artifacts=artifacts)
    artifact_node = ArtifactProducingAgentNode(
        persisting_executor,
        request_builder=lambda node, state, context: build_request(
            node,
            state,
            context,
            dynamic_tools=dynamic_tools,
        ),
    )

    async def _decoded[T](
        node: TaskNodeId,
        state: TaskState,
        invocation: TaskNodeInvocation,
        *,
        request_for: Callable[[TaskRunContext, tuple[Message, ...]], AgentRunRequest],
        decode: Callable[[str], T],
        reason: str,
    ) -> tuple[T, dict[str, Any]]:
        """Run one structured node, and ask once for the object alone.

        The decoder is not loosened by any of this (ADR-034 §3.1): what the node
        accepts is still exactly one JSON object and nothing else. A message
        that was not one buys a second turn, and a second turn that is not one
        either fails the node -- so an answer nobody could read still cannot
        become "read nothing", which is what ADR-032 §3.3 is for.

        Every structured node in either graph runs through here, which is the
        point: `plan`, `critic`, `review` and `research_external` share one
        strict decoder, so they share its exposure, and a fix at three of the
        four would be waiting for whichever model narrates at the fourth.

        `request_for` receives the run's own context because the corrective turn
        resolves an invocation of its own: it is a second spend, and it must
        appear on the event stream under its own run id and re-verify this
        Worker's claim before making it.
        """

        outcome = await executor.run(
            request_for(invocation.context, ()),
            invocation.events,
            invocation.cancellation,
        )
        charged = _charged_state(state, outcome)
        _require_completed(node, outcome, charged)
        try:
            return decode(outcome.output_text), _outcome_update(outcome)
        except StructuredOutputFramingError:
            # The only failure a restatement can address: how the message was
            # sent, not what it claimed. Fall through to the second turn.
            pass
        except StructuredOutputError as error:
            # Everything else the decoder refuses is a claim the model made and
            # got wrong, and asking again there is nudging rather than asking.
            raise TaskNodeRunFailedError(
                node=node, outcome=outcome, state=charged, reason=reason
            ) from error

        correction = await invocations.resolve(state, node)
        restated = await executor.run(
            # Stripped of tools here rather than at each call site, because it
            # is a property of the restatement and not of the node asking for
            # one: a turn that can reach nothing cannot come back with material
            # the first run did not already produce, so the worst it can do is
            # repeat an answer that was already on the record (ADR-034 §3.3).
            # The reviewers hold their working set statically, so a call site
            # that had to remember this would eventually not.
            request_for(
                correction.context, restatement_messages(outcome.output_text)
            ).model_copy(update={"tool_names": ()}),
            correction.events,
            correction.cancellation,
        )
        charged = _charged_state(charged, restated)
        _require_completed(node, restated, charged)
        try:
            value = decode(restated.output_text)
        except StructuredOutputError as error:
            raise TaskNodeRunFailedError(
                node=node, outcome=restated, state=charged, reason=reason
            ) from error
        return value, _outcome_update(outcome, restated)

    @contextmanager
    def _workspace_for(
        node: TaskNodeId, state: TaskState, invocation: TaskNodeInvocation
    ) -> Generator[WorkspaceSession | None]:
        """Enter this node's working set, pinned to the version it read.

        Whether a node pays for a session is derived from whether its agent
        holds workspace tools, rather than from a list of node names kept
        beside the profiles. The list was the bug waiting to happen: a node
        given the tools but not the session advertises them and then fails
        every call with ``WorkspaceUnavailableError`` while its run reports
        success (ADR-028 §3.2), and adding v2's two nodes is exactly the change
        that would have forgotten one.

        A node that dies inside this block returns no state update, which is
        why the attempt replacing it re-reads `state.workspace_version` rather
        than anything advanced here.
        """

        if workspace_scope is None or not _uses_workspace(node):
            yield None
            return
        principal = invocation.context.principal
        session = WorkspaceSession(
            workspace=TaskWorkspace(
                artifacts=artifacts,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
            ),
            version=state.workspace_version,
        )
        with workspace_scope.using(session):
            yield session

    def artifact_handler(node: TaskNodeId) -> TaskNodeHandler:
        async def run(state: TaskState) -> Mapping[str, Any]:
            invocation = await invocations.resolve(state, node)
            with _workspace_for(node, state, invocation) as session:
                report = await artifact_node.run(
                    node,
                    state,
                    invocation.context,
                    invocation.events,
                    invocation.cancellation,
                )
            update = _outcome_update(report.outcome)
            if session is not None and session.version != state.workspace_version:
                update["workspace_version"] = session.version
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

    async def _read_outward(
        state: TaskState,
        invocation: TaskNodeInvocation,
        research: TaskResearchHandlers,
    ) -> Mapping[str, Any]:
        """The half of `research_external` that reads pages it chose itself.

        Assembled only when this Worker registered research-audience tools,
        which is what makes it additive: a deployment with none runs exactly
        the search it ran before, and pays for no second model invocation
        (ADR-032 §3.1).

        The run goes through the plain executor rather than the persisting one
        on purpose. What this node owes the graph is an evidence bundle bound
        to the URLs it read, not the model's own prose stored as an artifact --
        `synthesize` loads `evidence_refs` as bundles and would refuse a
        Markdown artifact anyway.
        """

        bundle, update = await _decoded(
            "research_external",
            state,
            invocation,
            request_for=lambda context, extra: build_request(
                "research_external",
                state,
                context,
                ProjectedContext(extra_messages=extra),
                dynamic_tools=dynamic_tools,
            ),
            decode=lambda text: decode_external_evidence_output(
                text, task_id=state.task_id
            ),
            reason="external researcher JSON did not satisfy the evidence schema",
        )
        if bundle is None:
            # Read nothing it could stand behind. That is a real outcome of a
            # research branch, not a failure: the fan-in is allowed to be empty
            # and the run is still charged for above.
            return update
        reference = await research.evidence.save(
            context=_research_context(state, invocation), bundle=bundle
        )
        return update | {"evidence_refs": (reference.artifact_id,)}

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
        searched: Mapping[str, Any]
        if isinstance(reference, ExternalEvidenceSkipped):
            # Gateway already recorded the proposal and refusal. A missing
            # optional public-search provider is similarly visible there; no
            # artifact is invented merely to make the fan-in non-empty.
            searched = {}
        else:
            searched = {"evidence_refs": (reference.artifact_id,)}
        if not research_tool_catalog:
            return searched
        return _merge_node_updates(
            searched, await _read_outward(state, invocation, research)
        )

    async def synthesize(state: TaskState) -> Mapping[str, Any]:
        if research is None:
            return await artifact_handler("synthesize")(state)
        invocation = await invocations.resolve(state, "synthesize")
        bundles = await _load_evidence(state, invocation, research)
        await _confirm_internal_evidence(bundles, invocation, research)
        synthesis_node = ArtifactProducingAgentNode(
            persisting_executor,
            request_builder=lambda node, task_state, context: _synthesis_request(
                node,
                task_state,
                context,
                bundles,
                dynamic_tools=dynamic_tools,
            ),
        )
        # The same session the demo path enters. It is entered here too because
        # this is the branch a real Worker takes, and the writer's tools were
        # advertised on both: without it every `workspace_*` call the model
        # makes fails with `WorkspaceUnavailableError` while the run reports
        # success (ADR-028 §3.2).
        with _workspace_for("synthesize", state, invocation) as session:
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
        update = _outcome_update(report.outcome) | {
            "draft_ref": report.produced_ref,
            "review_result": None,
        }
        if session is not None and session.version != state.workspace_version:
            update["workspace_version"] = session.version
        return update

    async def plan(state: TaskState) -> Mapping[str, Any]:
        invocation = await invocations.resolve(state, "plan")
        steps, update = await _decoded(
            "plan",
            state,
            invocation,
            request_for=lambda context, extra: _structured_request(
                "plan", state, context, extra
            ),
            decode=decode_plan_output,
            reason="planner JSON did not satisfy the plan schema",
        )
        return update | {"plan": tuple(step.model_dump() for step in steps)}

    async def critic(state: TaskState) -> Mapping[str, Any]:
        invocation = await invocations.resolve(state, "critic")
        review, update = await _decoded(
            "critic",
            state,
            invocation,
            request_for=lambda context, extra: _structured_request(
                "critic", state, context, extra
            ),
            decode=lambda text: decode_review_output(text, state=state),
            reason="critic JSON did not satisfy the review schema",
        )
        return update | {"review_result": review.model_dump()}

    async def work(state: TaskState) -> Mapping[str, Any]:
        """v2's one working node: a full tool loop, bounded like every other.

        Structurally this is ``synthesize`` with the evidence machinery
        removed, and that is not a coincidence -- ADR-031 §2.2's whole claim is
        that "the model decides its own next step" is already what a node is
        here, and that v2 only gives that loop enough tools and enough budget
        (ADR-030) to finish something. So there is no second runtime, no
        second budget and no second cancellation path in this function; what
        differs from v1 is the profile it runs under and the topology around
        it.

        It writes ``draft_ref`` for the same reason ``synthesize`` does: the
        review gate binds a verdict to an exact revision of an exact artifact,
        and reusing that invariant is what lets one revision budget, one
        approval gate and one export path serve both graphs.
        """

        invocation = await invocations.resolve(state, "work")
        with _workspace_for("work", state, invocation) as session:
            report = await artifact_node.run(
                "work",
                state,
                invocation.context,
                invocation.events,
                invocation.cancellation,
            )
        if report.produced_ref is None:  # pragma: no cover
            raise AssertionError("a completed work node has no artifact")
        update = _outcome_update(report.outcome) | {
            # The previous verdict goes with the draft it judged. `TaskState`
            # requires a stored review to describe the current draft, and the
            # revision-aware wrapper writes the counter that goes with it.
            "draft_ref": report.produced_ref,
            "review_result": None,
        }
        if session is not None and session.version != state.workspace_version:
            update["workspace_version"] = session.version
        return update

    async def review(state: TaskState) -> Mapping[str, Any]:
        """v2's gate, deciding against the working set rather than only a text.

        The session is what makes that possible and is the reason this is not
        just ``critic`` under another name: the reviewer holds the three
        read-only workspace tools, and a node that advertises tools without
        entering the session fails every call while reporting success.
        """

        invocation = await invocations.resolve(state, "review")
        # The session wraps both turns. The corrective one holds no tools and so
        # cannot enter the working set, but a node that left the session between
        # them would be pinning a different version for each -- and the verdict
        # this decodes is about the version the first turn read.
        with _workspace_for("review", state, invocation):
            decoded, update = await _decoded(
                "review",
                state,
                invocation,
                request_for=lambda context, extra: _structured_request(
                    "review", state, context, extra
                ),
                decode=lambda text: decode_review_output(text, state=state),
                reason="reviewer JSON did not satisfy the review schema",
            )
        return update | {"review_result": decoded.model_dump()}

    async def export_report(state: TaskState) -> Mapping[str, Any]:
        if export is None:
            # Not a passthrough. A graph that reached export has a human's
            # approval behind it, and quietly settling that Task as succeeded
            # with nothing exported is the failure mode this node exists to
            # make impossible.
            raise TaskExportUnavailableError(
                "this Worker assembled no export capability"
            )
        if state.draft_ref is None:
            raise TaskExportPreconditionError("export requires a draft")
        if state.export_requires_approval and state.approval_id is None:
            # Only where there is a gate. Reaching export without an approval
            # on a gated Task means the graph walked past its own interrupt;
            # on an ungated one it is the ordinary path, and demanding an
            # approval id here would make export unreachable for it.
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
        "work": work,
        "review": review,
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
    *,
    dynamic_tools: Mapping[DynamicToolSource, tuple[ToolName, ...]] | None = None,
) -> AgentRunRequest:
    """The writer's run, with the evidence its profile is the only one to admit.

    Offered through the projection rather than appended afterwards: a node that
    could add messages to a finished request could add them to any agent's, and
    "the researchers never see each other's findings" would again be a property
    of who happened to call what.
    """

    return build_request(
        node,
        state,
        context,
        ProjectedContext(evidence=bundles),
        dynamic_tools=dynamic_tools,
    )


def decode_plan_output(text: str) -> tuple[TaskStep, ...]:
    """Decode exactly one strict JSON planner document, or fail closed."""

    # Parse once ourselves first: Pydantic's JSON parser intentionally accepts
    # duplicate object keys with last-key-wins semantics, which is not a safe
    # protocol for a model response. ``validate_json`` then retains JSON-mode
    # strictness while accepting JSON arrays for the tuple fields in the domain
    # model (``validate_python(..., strict=True)`` would reject those arrays).
    json_object(text)
    try:
        return _PLAN_DOCUMENT.validate_json(text, strict=True).steps
    except ValidationError as error:
        raise StructuredOutputError("planner output has an invalid shape") from error


def decode_review_output(text: str, *, state: TaskState) -> ReviewResult:
    """Decode a critic result bound to the current draft and revision."""

    if state.draft_ref is None:
        raise StructuredOutputError("critic ran before synthesis produced a draft")
    json_object(text)
    try:
        review = _REVIEW_RESULT.validate_json(text, strict=True)
    except ValidationError as error:
        raise StructuredOutputError("critic output has an invalid shape") from error
    if review.reviewed_draft_ref != state.draft_ref:
        raise StructuredOutputError("critic reviewed a different draft")
    if review.revision_number != state.revision_count:
        raise StructuredOutputError("critic reviewed a different revision")
    return review


def decode_external_evidence_output(
    text: str, *, task_id: str
) -> EvidenceBundle | None:
    """Decode what the external researcher read into one bounded bundle.

    ``None`` means it read nothing usable and said so, which is a permitted
    answer. Anything that does not parse raises instead: an unparseable answer
    from a node whose whole product is evidence cannot be rounded down to "no
    evidence" without the next node grounding a report in silence.
    """

    json_object(text)
    try:
        document = _EXTERNAL_EVIDENCE_DOCUMENT.validate_json(text, strict=True)
    except ValidationError as error:
        raise StructuredOutputError(
            "external evidence output has an invalid shape"
        ) from error
    if not document.items:
        return None
    return EvidenceBundle(
        task_id=task_id,
        source="external",
        items=tuple(
            EvidenceItem(
                # Not the URL: two passages read from the same page are two
                # items, and the bundle requires their ids to differ.
                evidence_id=new_id("evidence"),
                source="external",
                text=item.text,
                url=item.url,
                title=item.title,
            )
            for item in document.items
        ),
    )


def _merge_node_updates(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> dict[str, Any]:
    """Combine two halves of one node's update without losing either's refs.

    `research_external` can now contribute twice -- once from the deterministic
    search, once from the tool loop -- and both halves name reference tuples the
    graph's own fan-in reducer would have merged had they arrived as two
    branches. Merging them here keeps that equivalence.
    """

    merged = dict(first)
    for key, value in second.items():
        if key in {"evidence_refs", "agent_outcome_refs"}:
            merged[key] = merge_refs(
                cast("tuple[str, ...]", merged.get(key, ())),
                cast("tuple[str, ...]", value),
            )
        else:
            merged[key] = value
    return merged


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


#: The nodes whose artifact is the thing the Task exists to produce -- the one
#: a review binds to, an approval authorizes and the export renders. One per
#: graph, and they are ``report`` rather than ``agent_outcome`` so that a Task's
#: deliverable is distinguishable from the runs that led to it in a store where
#: both are just bytes.
_DELIVERABLE_NODES: Final[frozenset[TaskNodeId]] = frozenset({"synthesize", "work"})


def _artifact_kind(request: AgentRunRequest) -> ArtifactKind:
    node = request.trace.graph_node_id
    return "report" if node in _DELIVERABLE_NODES else "agent_outcome"


#: Nodes whose run is a verdict about the current draft rather than a product.
#: v1's ``critic`` and v2's ``review`` decode the same ``ReviewResult`` bound to
#: the same draft and revision, which is what lets one revision budget and one
#: approval gate serve both graphs (ADR-031 §2.1).
_REVIEWING_NODES: Final[frozenset[TaskNodeId]] = frozenset({"critic", "review"})


def _structured_request(
    node: TaskNodeId,
    state: TaskState,
    context: TaskRunContext,
    extra_messages: tuple[Message, ...] = (),
) -> AgentRunRequest:
    """The planner's and the reviewers' runs, through the same boundary as the rest.

    Their prompts are JSON contracts rather than instructions in prose, which is
    why they used to be assembled here instead of with the other agents. That
    made them the runs whose admitted context nothing declared -- and a
    reviewing agent is exactly the one whose isolation matters most, since a
    critic that could read the evidence would be reviewing the research instead
    of the writing.
    """

    reviewing = node in _REVIEWING_NODES
    if reviewing and state.draft_ref is None:
        raise StructuredOutputError(f"{node} requires a current draft reference")
    return build_request(
        node,
        state,
        context,
        ProjectedContext(
            draft_ref=state.draft_ref if reviewing else None,
            revision_number=state.revision_count if reviewing else None,
            extra_messages=extra_messages,
        ),
    )


def _uses_workspace(node: TaskNodeId) -> bool:
    """Whether this node's agent was declared any working-set tool.

    Asked of the profile rather than of a list of node names, so the answer
    cannot disagree with the tools the same node's request advertises.
    """

    try:
        profile = profile_for(node)
    except KeyError:
        # A routing node. It runs no agent, so it holds no tools.
        return False
    return any(name in WORKSPACE_TOOLS for name in profile.tool_names)


def _outcome_update(*outcomes: AgentOutcome) -> dict[str, Any]:
    """Return this node's deltas so parallel research can be merged safely.

    Several, because a structured node can spend more than one run: a
    corrective turn is a run of its own and both were charged for. The usage
    channel adds deltas, so a node that reported only its last run would hand
    the graph a bill missing everything before it.
    """

    usage = BudgetUsage()
    for outcome in outcomes:
        usage = usage.merged(outcome.usage)
    return {
        "agent_outcome_refs": merge_refs(
            *((outcome.agent_run_id,) for outcome in outcomes)
        ),
        "budget_usage": usage.model_dump(),
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
    "V1_HANDLER_NODES",
    "V2_HANDLER_NODES",
    "ArtifactPersistingExecutor",
    "BoundedParallelExecutor",
    "CancellationFactory",
    "EventSinkFactory",
    "EvidenceAuthorizationChangedError",
    "StructuredOutputError",
    "StructuredOutputFramingError",
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
    "build_task_handlers",
    "build_task_v1_handlers",
    "build_task_v2_handlers",
    "decode_plan_output",
    "decode_review_output",
]
