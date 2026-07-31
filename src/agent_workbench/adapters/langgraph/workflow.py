"""Compile the fixed research graph onto LangGraph.

The adapter owns compilation, checkpoints and scheduling.  It owns no routing
decision: every edge comes from ``agent_workbench.workflows.research_graph``,
so the graph that replays a checkpoint and the graph the control-flow tests
assert on are the same declaration.  A routing rule restated here would be a
second definition that drifts silently, and only under recovery.

``TaskState`` crosses into the graph as a plain mapping.  Its fields are the
graph's channels, and the two reference channels carry the sorted-union
reducer, so LangGraph's own fan-in and this project's ``fan_in`` produce the
same merge.  A field added to ``TaskState`` without a channel here would be
dropped on the first checkpoint round trip, so a test asserts the two field
sets are equal rather than trusting them to stay in step.

Which graph version wrote a thread is recorded **in the checkpoint**, by
putting it on the config LangGraph is invoked with: the contract's own
``get_checkpoint_metadata`` copies scalars off ``configurable`` into every
checkpoint's metadata.  It was previously a dictionary in this object, which
made "does this thread exist" and "which graph wrote it" questions only the
process that started the run could answer -- which are exactly the two
questions a process that did *not* start it has to ask.  This is not Task product state
duplicated here: ``task_runs.graph_version`` records what the Task was asked
to run, while this records what actually wrote the execution position.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Annotated, Any, Final, TypedDict, cast

# langgraph ships no type stubs, so strict pyright cannot see through its
# generics. Narrowed here the same way the other stub-less dependencies are,
# rather than by relaxing the type checker for the whole package.
from langgraph.checkpoint.memory import (  # pyright: ignore[reportMissingTypeStubs]
    InMemorySaver,
)
from langgraph.graph import (  # pyright: ignore[reportMissingTypeStubs]
    END,
    START,
    StateGraph,
)
from langgraph.types import Command  # pyright: ignore[reportMissingTypeStubs]

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.runs import BudgetUsage
from agent_workbench.domain.tasks import CANONICAL_V1_NODE_IDS, TaskNodeId, TaskState
from agent_workbench.ports.fault_injector import FaultInjector
from agent_workbench.ports.task_workflow import (
    CHECKPOINT_FENCE_EPOCH_KEY,
    CHECKPOINT_FENCE_GUARD_KEY_KEY,
    CHECKPOINT_FENCE_GUARD_PID_KEY,
    CHECKPOINT_FENCE_TASK_ID_KEY,
    CHECKPOINT_FENCE_WORKER_ID_KEY,
    ApprovalResume,
    CheckpointFence,
    CheckpointPosition,
    GraphVersion,
    TaskWorkflowResult,
    WorkflowGraphVersionMismatchError,
    WorkflowThreadAlreadyExistsError,
    WorkflowThreadNotFoundError,
)
from agent_workbench.workflows.research_graph import (
    ENTRY_NODE,
    GRAPH_VERSION_V1,
    RESEARCH_BRANCHES,
    STATIC_EDGES,
    TERMINAL_NODE,
    begin_revision,
    merge_refs,
    route_approval,
    route_quality_gate,
    route_research,
    terminal_failure_reason,
)

# The compiled graph and the builder are the only shapes this module needs
# from langgraph, and both are opaque to the type checker.
CompiledGraph = Any
GraphSpec = Any

# The sentinel a quality gate uses when its revision budget is spent. It is a
# node name LangGraph knows and the domain does not, which is the point: the
# graph must stop, and TaskNodeId must not grow a member that only exists to
# describe stopping.
_EXHAUSTED: Final[str] = END

# The configurable key that carries the graph version into checkpoint metadata.
GRAPH_VERSION_KEY: Final[str] = "graph_version"

# The key an interrupt's value carries. It is the only thing the interrupt says:
# which approval the pause is about. The decision is read from the ledger by the
# node itself, so it deliberately never travels in the checkpoint.
INTERRUPT_APPROVAL_KEY: Final[str] = "approval_id"

# What a checkpoint that never recorded a version is reported as. It cannot
# collide with a real one: GraphVersion must start alphanumeric, so no valid
# version can equal this, and a caller comparing it to any version gets
# "different" -- which is the answer, because a checkpoint this adapter did not
# write is not one it can claim to understand.
UNRECORDED_GRAPH_VERSION: Final[str] = "<unrecorded>"


def _merge(
    existing: tuple[Identifier, ...] | None,
    incoming: tuple[Identifier, ...] | None,
) -> tuple[Identifier, ...]:
    """Channel reducer: the same sorted union the control flow specifies."""

    return merge_refs(existing or (), incoming or ())


def _merge_budget(existing: Any | None, incoming: Any | None) -> dict[str, Any]:
    """Add node-local usage deltas without losing a parallel research branch."""

    before = BudgetUsage.model_validate(existing or {})
    delta = BudgetUsage.model_validate(incoming or {})
    return before.merged(delta).model_dump()


class GraphState(TypedDict, total=False):
    """``TaskState`` as LangGraph channels.

    The reference and usage channels declare reducers because both research
    branches write them concurrently. Everything else has one writer and
    takes LangGraph's default last-write-wins behaviour.
    """

    schema_version: int
    task_id: str
    objective: str
    knowledge_base_id: str | None
    plan: tuple[Any, ...]
    evidence_refs: Annotated[tuple[Identifier, ...], _merge]
    agent_outcome_refs: Annotated[tuple[Identifier, ...], _merge]
    draft_ref: str | None
    review_result: Any
    approval_id: str | None
    approval_decision: str | None
    budget_usage: Annotated[Any, _merge_budget]
    revision_count: int
    max_revisions: int


NodeHandler = Callable[[TaskState], Awaitable[Mapping[str, Any]]]


def _passthrough(_: TaskState) -> dict[str, Any]:
    return {}


async def _default_handler(state: TaskState) -> Mapping[str, Any]:
    return _passthrough(state)


def _revision_aware_handler(handler: NodeHandler) -> NodeHandler:
    """Apply the declared revise transition immediately before synthesis.

    The quality-gate router must still see the critic's review in order to
    choose ``synthesize``.  Therefore the counter cannot be changed at the
    gate itself.  The next synthesis invocation is the first point at which
    the old verdict is no longer current, so it advances the revision and
    clears the review before the handler sees the state.
    """

    async def run(state: TaskState) -> Mapping[str, Any]:
        if state.review_result is None or state.review_result.decision != "revise":
            return await handler(state)

        revised = begin_revision(state)
        result = dict(await handler(revised))
        # A handler may write a new draft, but it must not accidentally retain
        # the verdict about the previous one or undo the bounded transition.
        result.update(
            revision_count=revised.revision_count,
            review_result=None,
        )
        return result

    return run


def _fault_injected_handlers(
    handlers: Mapping[TaskNodeId, NodeHandler] | None,
    fault_injector: FaultInjector | None,
) -> Mapping[TaskNodeId, NodeHandler] | None:
    """Pause after a node returns and before LangGraph can checkpoint it."""

    if fault_injector is None:
        return handlers
    supplied = dict(handlers or {})
    wrapped: dict[TaskNodeId, NodeHandler] = {}
    for node in CANONICAL_V1_NODE_IDS:
        handler = supplied.get(node, _default_handler)

        async def run(
            state: TaskState,
            *,
            wrapped_handler: NodeHandler = handler,
        ) -> Mapping[str, Any]:
            result = await wrapped_handler(state)
            await fault_injector.hit("after_node_before_checkpoint")
            return result

        wrapped[node] = run
    return wrapped


def _to_state(payload: Mapping[str, Any]) -> TaskState:
    # Reserved LangGraph channels are dropped here rather than tolerated by the
    # domain model: an interrupted invocation returns its pending interrupts in
    # `__interrupt__`, and a TaskState that accepted that key would be a
    # checkpoint contract with a framework detail in it.
    return TaskState.model_validate(
        {key: value for key, value in payload.items() if not key.startswith("__")}
    )


def _route_research(payload: Mapping[str, Any]) -> Sequence[str]:
    return list(route_research(_to_state(payload)))


def _route_quality_gate(payload: Mapping[str, Any]) -> str:
    target = route_quality_gate(_to_state(payload))
    # None is "the critic still wants changes and there is no budget left".
    # It ends the graph rather than reaching approval, so an exhausted budget
    # cannot be mistaken for a pass by a caller that ignores a return value.
    return _EXHAUSTED if target is None else target


def _route_approval(payload: Mapping[str, Any]) -> str:
    target = route_approval(_to_state(payload))
    # None is "a human said no". It ends the graph rather than reaching export,
    # for the same reason the exhausted gate does: a rejection that still
    # exported would make the approval a formality.
    return _EXHAUSTED if target is None else target


def build_v1_graph(
    handlers: Mapping[TaskNodeId, NodeHandler] | None = None,
) -> GraphSpec:
    """Assemble the v1 graph from the declared edges and the given handlers.

    Handlers are injected so a Task can run against fakes or against the real
    agent nodes without a second graph definition existing for tests.
    """

    supplied = dict(handlers or {})
    graph = cast("Any", StateGraph(GraphState))

    for node in STATIC_EDGES:
        handler = supplied.get(node, _default_handler)
        if node == "synthesize":
            handler = _revision_aware_handler(handler)
        graph.add_node(node, _wrap(handler))
    for node in ("route", "quality_gate"):
        graph.add_node(node, _wrap(supplied.get(node, _default_handler)))

    graph.add_edge(START, ENTRY_NODE)
    for source, targets in STATIC_EDGES.items():
        for target in targets:
            graph.add_edge(source, target)
    graph.add_edge(TERMINAL_NODE, END)

    graph.add_conditional_edges("route", _route_research, list(RESEARCH_BRANCHES))
    graph.add_conditional_edges(
        "quality_gate",
        _route_quality_gate,
        ["approval", "synthesize", _EXHAUSTED],
    )
    graph.add_conditional_edges("approval", _route_approval, ["export", _EXHAUSTED])
    return graph


def _wrap(handler: NodeHandler) -> Callable[[Mapping[str, Any]], Awaitable[Any]]:
    async def run(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await handler(_to_state(payload))

    return run


GraphBuilder = Callable[[Mapping[TaskNodeId, NodeHandler] | None], GraphSpec]

# The version registry. A checkpoint records the version it was written by, so
# an unknown version has to fail rather than fall back to the newest graph.
GRAPH_BUILDERS: Final[dict[GraphVersion, GraphBuilder]] = {
    GRAPH_VERSION_V1: build_v1_graph,
}


class LangGraphTaskWorkflow:
    """``TaskWorkflowPort`` backed by a compiled LangGraph."""

    def __init__(
        self,
        *,
        handlers: Mapping[TaskNodeId, NodeHandler] | None = None,
        checkpointer: Any | None = None,
        builders: Mapping[GraphVersion, GraphBuilder] = GRAPH_BUILDERS,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._handlers = _fault_injected_handlers(handlers, fault_injector)
        # The default is in-memory, which means "this process only". A caller
        # that wants a thread to outlive its process passes a durable saver.
        self._checkpointer = checkpointer or cast("Any", InMemorySaver())
        self._builders = dict(builders)
        self._compiled: dict[GraphVersion, CompiledGraph] = {}

    def _graph(
        self, graph_version: GraphVersion, thread_id: Identifier
    ) -> CompiledGraph:
        if graph_version not in self._builders:
            raise WorkflowGraphVersionMismatchError(
                thread_id=thread_id,
                checkpoint_graph_version=UNRECORDED_GRAPH_VERSION,
                requested_graph_version=graph_version,
            )
        if graph_version not in self._compiled:
            builder = self._builders[graph_version]
            self._compiled[graph_version] = builder(self._handlers).compile(
                checkpointer=self._checkpointer
            )
        return self._compiled[graph_version]

    async def run(
        self,
        state: TaskState,
        *,
        thread_id: Identifier,
        graph_version: GraphVersion,
        checkpoint_fence: CheckpointFence | None = None,
    ) -> TaskWorkflowResult:
        # Asked of the checkpoint, so a thread another process started is still
        # an existing thread. This is a check and not a lock: two first runs
        # racing on one thread_id are excluded by the Task lease, not here.
        if await self._checkpoint(thread_id) is not None:
            raise WorkflowThreadAlreadyExistsError(thread_id)
        compiled = self._graph(graph_version, thread_id)
        payload = await compiled.ainvoke(
            state.model_dump(),
            _config(thread_id, graph_version, checkpoint_fence),
        )
        return await self._result(compiled, thread_id, graph_version, payload)

    async def resume(
        self,
        *,
        thread_id: Identifier,
        graph_version: GraphVersion,
        checkpoint_fence: CheckpointFence | None = None,
        approval: ApprovalResume | None = None,
    ) -> TaskWorkflowResult:
        checkpoint = await self._checkpoint(thread_id)
        if checkpoint is None:
            raise WorkflowThreadNotFoundError(thread_id)
        written_by = checkpoint.metadata.get(
            GRAPH_VERSION_KEY, UNRECORDED_GRAPH_VERSION
        )
        if written_by != graph_version:
            # The checkpoint is left exactly as it was: a mismatched resume is
            # a migration decision, and continuing under another graph would
            # silently re-enter a node that means something else now. Checked
            # before the graph is built, so a checkpoint written by a version
            # this process no longer registers still reports what wrote it
            # instead of "unknown version".
            raise WorkflowGraphVersionMismatchError(
                thread_id=thread_id,
                checkpoint_graph_version=written_by,
                requested_graph_version=graph_version,
            )
        compiled = self._graph(graph_version, thread_id)
        # No initial state: it already belongs to the checkpoint, and passing
        # it again is how the original input gets appended twice. A Command is
        # not initial state either -- it carries the wake-up for a pending
        # interrupt and nothing else.
        payload = await compiled.ainvoke(
            None if approval is None else Command(resume=approval.model_dump()),
            _config(thread_id, graph_version, checkpoint_fence),
        )
        return await self._result(compiled, thread_id, graph_version, payload)

    async def inspect(self, thread_id: Identifier) -> CheckpointPosition | None:
        checkpoint = await self._checkpoint(thread_id)
        if checkpoint is None:
            return None
        written_by = checkpoint.metadata.get(GRAPH_VERSION_KEY)
        if written_by is None or written_by not in self._builders:
            # Pending work is LangGraph's own computation over the graph that
            # wrote the checkpoint, so asking for it means compiling that
            # graph. This process cannot, and does not need to: a position
            # whose version is unrecorded or unknown is parked before anything
            # reads its pending nodes.
            return CheckpointPosition(graph_version=written_by)
        snapshot = await self._graph(written_by, thread_id).aget_state(
            _config(thread_id, written_by)
        )
        pending = tuple(snapshot.next)
        return CheckpointPosition(
            graph_version=written_by,
            pending_nodes=pending,
            awaiting_approval_id=_awaiting_approval_id(snapshot),
            # A checkpoint *before* quality_gate may already contain the
            # critic's exhausting revise verdict. It is still executable,
            # though: the pending gate has not made the terminal decision.
            failure_reason=(
                None if pending else terminal_failure_reason(_to_state(snapshot.values))
            ),
        )

    async def _checkpoint(self, thread_id: Identifier) -> Any | None:
        """The thread's latest checkpoint, or None if it has never run.

        Deliberately asked without a graph version: whether a thread exists
        does not depend on which graph is asking.
        """

        return await self._checkpointer.aget_tuple(
            {"configurable": {"thread_id": thread_id}}
        )

    async def _result(
        self,
        compiled: CompiledGraph,
        thread_id: Identifier,
        graph_version: GraphVersion,
        payload: Mapping[str, Any],
    ) -> TaskWorkflowResult:
        snapshot = await compiled.aget_state(_config(thread_id, graph_version))
        pending = tuple(snapshot.next)
        state = _to_state(payload)
        failure_reason = terminal_failure_reason(state)
        return TaskWorkflowResult(
            thread_id=thread_id,
            graph_version=graph_version,
            disposition=(
                "interrupted"
                if pending
                else "failed"
                if failure_reason is not None
                else "completed"
            ),
            state=state,
            next_nodes=pending,
            failure_reason=None if pending else failure_reason,
        )


def _awaiting_approval_id(snapshot: Any) -> str | None:
    """The approval a paused thread is waiting on, if it is paused on one.

    Read from the snapshot's interrupts rather than from ``TaskState``, because
    a graph stopped *at* the approval node has not written any state yet -- the
    node raised before returning. The interrupt is the only place the id exists
    until a decision resumes it.

    Anything unrecognised is reported as ``None``. A pause this process cannot
    describe is not one it should claim is an approval: the position still has
    pending nodes, so the Worker treats it as ordinary unfinished work rather
    than parking a Task on an approval nobody can decide.
    """

    for pending in tuple(cast("Any", getattr(snapshot, "interrupts", ()) or ())):
        value = cast("Any", getattr(pending, "value", None))
        if not isinstance(value, Mapping):
            continue
        approval_id = cast("Mapping[str, object]", value).get(INTERRUPT_APPROVAL_KEY)
        if isinstance(approval_id, str) and approval_id:
            return approval_id
    return None


def _config(
    thread_id: str,
    graph_version: GraphVersion,
    checkpoint_fence: CheckpointFence | None = None,
) -> dict[str, Any]:
    # `graph_version` is not read by LangGraph. It is here because
    # `get_checkpoint_metadata` copies configurable scalars into every
    # checkpoint's metadata, which is what makes the version durable and
    # queryable without a table of this adapter's own.
    configurable: dict[str, Any] = {
        "thread_id": thread_id,
        GRAPH_VERSION_KEY: graph_version,
    }
    if checkpoint_fence is not None:
        # Scalar fields are intentional: LangGraph copies configurable scalars
        # to checkpoint metadata, allowing the saver to rebuild returned
        # config and its parent config without leaking this coordination fact
        # into the persisted TaskState.
        configurable.update(
            **{
                CHECKPOINT_FENCE_TASK_ID_KEY: checkpoint_fence.task_id,
                CHECKPOINT_FENCE_WORKER_ID_KEY: checkpoint_fence.worker_id,
                CHECKPOINT_FENCE_EPOCH_KEY: checkpoint_fence.epoch,
                CHECKPOINT_FENCE_GUARD_PID_KEY: checkpoint_fence.guard_backend_pid,
                CHECKPOINT_FENCE_GUARD_KEY_KEY: checkpoint_fence.guard_lock_key,
            },
        )
    return {"configurable": configurable}


__all__ = [
    "GRAPH_BUILDERS",
    "GRAPH_VERSION_KEY",
    "INTERRUPT_APPROVAL_KEY",
    "UNRECORDED_GRAPH_VERSION",
    "GraphBuilder",
    "GraphState",
    "LangGraphTaskWorkflow",
    "NodeHandler",
    "build_v1_graph",
]
