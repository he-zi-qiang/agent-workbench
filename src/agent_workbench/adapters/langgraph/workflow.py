"""Compile this project's Task graphs onto LangGraph.

The adapter owns compilation, checkpoints and scheduling.  It owns no routing
decision: every edge comes from ``agent_workbench.workflows.research_graph`` or
``agent_workbench.workflows.general_graph``, so the graph that replays a
checkpoint and the graph the control-flow tests assert on are the same
declaration.  A routing rule restated here would be a second definition that
drifts silently, and only under recovery.

Two graphs, and every per-version fact reached through one registry keyed by
version rather than through a module-level import.  The difference matters
under recovery and nowhere else, which is why it is stated here: both graphs
stop on the same two facts and describe them differently, so a fixed reader
would report a plausible wrong sentence rather than raise (ADR-031 §3).

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
from dataclasses import dataclass
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
from agent_workbench.domain.tasks import (
    CANONICAL_V1_NODE_IDS,
    CANONICAL_V2_NODE_IDS,
    TaskNodeId,
    TaskState,
)
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
from agent_workbench.workflows import general_graph, research_graph
from agent_workbench.workflows.research_graph import begin_revision, merge_refs

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
    # Written once at load and read by the quality gate's router. A submission
    # value that never became a channel would default back on every hop and
    # export a file nobody asked for.
    wants_report: bool
    # Written once at load, beside `wants_report`, and read by the same router.
    # Without a channel it would default back to True on every hop, so a
    # deployment that turned the gate off would still pause -- and the pause
    # would be at a node whose approval nothing opened.
    export_requires_approval: bool
    plan: tuple[Any, ...]
    evidence_refs: Annotated[tuple[Identifier, ...], _merge]
    agent_outcome_refs: Annotated[tuple[Identifier, ...], _merge]
    draft_ref: str | None
    # ADR-028. Last write wins, deliberately not merged: two nodes cannot both
    # advance the working set, because each derives its new version from the one
    # it read at entry. A merge reducer here would silently pick a winner and
    # drop the other node's writes without anything reporting it.
    workspace_version: str | None
    review_result: Any
    approval_id: str | None
    approval_decision: str | None
    export_ref: str | None
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
    """Pause after a node returns and before LangGraph can checkpoint it.

    Every node of every graph, not one graph's. This used to iterate v1's tuple
    alone, which was correct while there was one graph and silently destructive
    once there were two: the result *replaces* the supplied mapping, so a v2
    handler at a node v1 does not have would have been dropped -- and a dropped
    handler is not an error but a pass-through, which is a Task that ran its
    whole graph, did no work, and succeeded.
    """

    if fault_injector is None:
        return handlers
    supplied = dict(handlers or {})
    wrapped: dict[TaskNodeId, NodeHandler] = {}
    for node in dict.fromkeys((*CANONICAL_V1_NODE_IDS, *CANONICAL_V2_NODE_IDS)):
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


def _review_aware_handler(handler: NodeHandler) -> NodeHandler:
    """Close v2's review *after* the work node has read it.

    The mirror image of ``_revision_aware_handler``, and the difference is the
    point. v1 hands the writer a state with the critic's verdict already
    removed; v2's worker runs against the state as checkpointed, verdict and
    all, because the review is *why* it is running again and a loop that
    carries nothing back is not a method (ADR-031 §2.1). The bounded transition
    is then applied to what the handler returned, which is also the only
    ordering ``TaskState`` allows -- a stored review must describe the current
    revision.
    """

    async def run(state: TaskState) -> Mapping[str, Any]:
        if state.review_result is None or state.review_result.decision != "revise":
            return await handler(state)
        result = dict(await handler(state))
        # After the handler, and unconditionally: a work node may write a new
        # draft, but it must not keep the verdict about the previous one or
        # undo the counter that bounds this loop.
        result.update(general_graph.revision_update(state))
        return result

    return run


def _route_research(payload: Mapping[str, Any]) -> Sequence[str]:
    return list(research_graph.route_research(_to_state(payload)))


def _route_quality_gate(payload: Mapping[str, Any]) -> str:
    target = research_graph.route_quality_gate(_to_state(payload))
    # None is either "no budget left to revise" or "passed, and no file was
    # asked for". Both end the graph here; `terminal_failure_reason` is what
    # tells the Worker which of the two it settles as. Ending on the first
    # keeps an exhausted budget from being mistaken for a pass by a caller
    # that ignores a return value.
    return _EXHAUSTED if target is None else target


def _route_approval(payload: Mapping[str, Any]) -> str:
    target = research_graph.route_approval(_to_state(payload))
    # None is "a human said no". It ends the graph rather than reaching export,
    # for the same reason the exhausted gate does: a rejection that still
    # exported would make the approval a formality.
    return _EXHAUSTED if target is None else target


def _route_review(payload: Mapping[str, Any]) -> str:
    target = general_graph.route_review(_to_state(payload))
    return _EXHAUSTED if target is None else target


def _route_v2_approval(payload: Mapping[str, Any]) -> str:
    # Its own function rather than v1's, even though the two decide the same
    # way today. Each graph declares its own gate, and one adapter function
    # reading from one of them is how a change to one graph's rejection
    # semantics would silently become both graphs'.
    target = general_graph.route_approval(_to_state(payload))
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

    for node in research_graph.STATIC_EDGES:
        handler = supplied.get(node, _default_handler)
        if node == "synthesize":
            handler = _revision_aware_handler(handler)
        graph.add_node(node, _wrap(handler))
    for node in ("route", "quality_gate"):
        graph.add_node(node, _wrap(supplied.get(node, _default_handler)))

    graph.add_edge(START, research_graph.ENTRY_NODE)
    for source, targets in research_graph.STATIC_EDGES.items():
        for target in targets:
            graph.add_edge(source, target)
    graph.add_edge(research_graph.TERMINAL_NODE, END)

    graph.add_conditional_edges(
        "route", _route_research, list(research_graph.RESEARCH_BRANCHES)
    )
    # "export" for the reason spelled out over v2's `review` edges below, and
    # this list is the half that was missed when v1 learned to read
    # `export_requires_approval`. The bug the v2 comment records is not
    # hypothetical here either: routing unit tests call the router directly and
    # see a correct answer, while LangGraph resolves that same answer against
    # this list and raises `KeyError: 'export'` on the first real Task.
    graph.add_conditional_edges(
        "quality_gate",
        _route_quality_gate,
        ["approval", "export", "synthesize", _EXHAUSTED],
    )
    graph.add_conditional_edges("approval", _route_approval, ["export", _EXHAUSTED])
    return graph


def build_v2_graph(
    handlers: Mapping[TaskNodeId, NodeHandler] | None = None,
) -> GraphSpec:
    """Assemble the v2 general graph the same way, from its own declaration.

    Written out rather than shared with ``build_v1_graph`` behind a parameter.
    The two functions look alike because both compile an edge table, and that
    is all they have in common: one fans out to two researchers and loops a
    writer, the other loops one working node. A single builder taking "which
    edges, which conditional nodes, which node gets the revision wrapper" would
    be a third description of both graphs, and the place a change to one leaked
    into the other (ADR-031 §3).
    """

    supplied = dict(handlers or {})
    graph = cast("Any", StateGraph(GraphState))

    for node in general_graph.STATIC_EDGES:
        handler = supplied.get(node, _default_handler)
        if node == "work":
            handler = _review_aware_handler(handler)
        graph.add_node(node, _wrap(handler))

    graph.add_edge(START, general_graph.ENTRY_NODE)
    for source, targets in general_graph.STATIC_EDGES.items():
        for target in targets:
            graph.add_edge(source, target)
    graph.add_edge(general_graph.TERMINAL_NODE, END)

    # "export" is here because a deployment may run without the approval gate
    # (`workflow.export_requires_approval`), and then `route_review` sends a
    # passing review straight there. This list is what LangGraph resolves a
    # router's answer against, so a target the router can return and this list
    # omits is a `KeyError` at run time -- measured, on the first real Task
    # submitted after the gate was made optional.
    graph.add_conditional_edges(
        "review", _route_review, ["approval", "export", "work", _EXHAUSTED]
    )
    graph.add_conditional_edges("approval", _route_v2_approval, ["export", _EXHAUSTED])
    return graph


def _wrap(handler: NodeHandler) -> Callable[[Mapping[str, Any]], Awaitable[Any]]:
    async def run(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await handler(_to_state(payload))

    return run


GraphBuilder = Callable[[Mapping[TaskNodeId, NodeHandler] | None], GraphSpec]
TerminalFailureReason = Callable[[TaskState], str | None]


@dataclass(frozen=True, slots=True)
class GraphDefinition:
    """Everything this adapter needs to know about one graph version.

    The two fields travel together because the second one is only meaningful
    for threads the first one wrote. Reading a v2 thread's final state through
    v1's ``terminal_failure_reason`` would not raise -- both graphs stop on the
    same two facts -- it would report the wrong sentence, which is the kind of
    cross-graph mistake that survives every test that only checks whether a
    Task failed (ADR-031 §3).
    """

    build: GraphBuilder
    terminal_failure_reason: TerminalFailureReason


# The version registry. A checkpoint records the version it was written by, so
# an unknown version has to fail rather than fall back to the newest graph.
GRAPH_DEFINITIONS: Final[dict[GraphVersion, GraphDefinition]] = {
    research_graph.GRAPH_VERSION_V1: GraphDefinition(
        build=build_v1_graph,
        terminal_failure_reason=research_graph.terminal_failure_reason,
    ),
    general_graph.GRAPH_VERSION_V2: GraphDefinition(
        build=build_v2_graph,
        terminal_failure_reason=general_graph.terminal_failure_reason,
    ),
}


class LangGraphTaskWorkflow:
    """``TaskWorkflowPort`` backed by a compiled LangGraph."""

    def __init__(
        self,
        *,
        handlers: Mapping[TaskNodeId, NodeHandler] | None = None,
        checkpointer: Any | None = None,
        graphs: Mapping[GraphVersion, GraphDefinition] = GRAPH_DEFINITIONS,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._handlers = _fault_injected_handlers(handlers, fault_injector)
        # The default is in-memory, which means "this process only". A caller
        # that wants a thread to outlive its process passes a durable saver.
        self._checkpointer = checkpointer or cast("Any", InMemorySaver())
        self._graphs = dict(graphs)
        self._compiled: dict[GraphVersion, CompiledGraph] = {}

    def _definition(
        self, graph_version: GraphVersion, thread_id: Identifier
    ) -> GraphDefinition:
        definition = self._graphs.get(graph_version)
        if definition is None:
            raise WorkflowGraphVersionMismatchError(
                thread_id=thread_id,
                checkpoint_graph_version=UNRECORDED_GRAPH_VERSION,
                requested_graph_version=graph_version,
            )
        return definition

    def _graph(
        self, graph_version: GraphVersion, thread_id: Identifier
    ) -> CompiledGraph:
        definition = self._definition(graph_version, thread_id)
        if graph_version not in self._compiled:
            self._compiled[graph_version] = definition.build(self._handlers).compile(
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
        if written_by is None or written_by not in self._graphs:
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
        state = _to_state(snapshot.values)
        # Asked of the graph that *wrote* this checkpoint, not of whichever
        # one this call happens to be about. Both graphs stop on the same
        # two facts and word them differently, so a fixed reader here would
        # not fail -- it would put v1's sentence on a v2 Task, and no test
        # that only checks "did it fail" would notice.
        #
        # A checkpoint *before* the gate may already contain the exhausting
        # revise verdict. It is still executable, though: the pending gate
        # has not made the terminal decision.
        failure = (
            None if pending else self._graphs[written_by].terminal_failure_reason(state)
        )
        return CheckpointPosition(
            graph_version=written_by,
            pending_nodes=pending,
            awaiting_approval_id=_awaiting_approval_id(snapshot),
            failure_reason=failure,
            # One wording for both graphs, because unlike the failure above it
            # reads a shared domain fact (`unresolved_review`, ADR-060) rather
            # than a graph's own gate. Suppressed on a failed position: a
            # rejected approval may coexist with the exhausted verdict, and the
            # position type refuses to carry both stories at once.
            caveat=(None if pending or failure is not None else _finish_caveat(state)),
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
        failure_reason = self._definition(
            graph_version, thread_id
        ).terminal_failure_reason(state)
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


def _finish_caveat(state: TaskState) -> str | None:
    """The sentence a successful finish carries about its unanswered review.

    Bounded to the position field's 256 rather than trusting 32 issues to be
    short: the full verdict lives in the checkpoint's ``review_result``, so a
    cut here loses nothing an auditor cannot recover -- what this sentence owes
    the reader is that the dispute *exists*, not its every clause (ADR-060).
    """

    review = state.unresolved_review
    if review is None:
        return None
    sentence = (
        f"the reviewer still saw {len(review.issues)} unresolved issue(s) "
        f"after {state.max_revisions} revision(s): " + "; ".join(review.issues)
    )
    return sentence if len(sentence) <= 256 else sentence[:253] + "..."


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
    "GRAPH_DEFINITIONS",
    "GRAPH_VERSION_KEY",
    "INTERRUPT_APPROVAL_KEY",
    "UNRECORDED_GRAPH_VERSION",
    "GraphBuilder",
    "GraphDefinition",
    "GraphState",
    "LangGraphTaskWorkflow",
    "NodeHandler",
    "TerminalFailureReason",
    "build_v1_graph",
    "build_v2_graph",
]
