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

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.tasks import TaskNodeId, TaskState
from agent_workbench.ports.task_workflow import (
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
    merge_refs,
    route_quality_gate,
    route_research,
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


def _merge(
    existing: tuple[Identifier, ...] | None,
    incoming: tuple[Identifier, ...] | None,
) -> tuple[Identifier, ...]:
    """Channel reducer: the same sorted union the control flow specifies."""

    return merge_refs(existing or (), incoming or ())


class GraphState(TypedDict, total=False):
    """``TaskState`` as LangGraph channels.

    Only the two reference channels declare a reducer, because they are the
    only ones two concurrent nodes write.  Everything else has a single writer
    and takes last-write-wins, which is LangGraph's default.
    """

    schema_version: int
    task_id: str
    objective: str
    plan: tuple[Any, ...]
    evidence_refs: Annotated[tuple[Identifier, ...], _merge]
    agent_outcome_refs: Annotated[tuple[Identifier, ...], _merge]
    draft_ref: str | None
    review_result: Any
    approval_id: str | None
    budget_usage: Any
    revision_count: int
    max_revisions: int


NodeHandler = Callable[[TaskState], Awaitable[Mapping[str, Any]]]


def _passthrough(_: TaskState) -> dict[str, Any]:
    return {}


async def _default_handler(state: TaskState) -> Mapping[str, Any]:
    return _passthrough(state)


def _to_state(payload: Mapping[str, Any]) -> TaskState:
    return TaskState.model_validate(dict(payload))


def _route_research(payload: Mapping[str, Any]) -> Sequence[str]:
    return list(route_research(_to_state(payload)))


def _route_quality_gate(payload: Mapping[str, Any]) -> str:
    target = route_quality_gate(_to_state(payload))
    # None is "the critic still wants changes and there is no budget left".
    # It ends the graph rather than reaching approval, so an exhausted budget
    # cannot be mistaken for a pass by a caller that ignores a return value.
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
        graph.add_node(node, _wrap(supplied.get(node, _default_handler)))
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
    ) -> None:
        self._handlers = handlers
        self._checkpointer = checkpointer or cast("Any", InMemorySaver())
        self._builders = dict(builders)
        self._compiled: dict[GraphVersion, CompiledGraph] = {}
        # Which graph version wrote a thread. LangGraph's checkpoint does not
        # carry it, and inferring it from the state's shape would be a guess
        # made exactly when a wrong answer is most expensive.
        self._thread_versions: dict[str, GraphVersion] = {}

    def _graph(self, graph_version: GraphVersion) -> CompiledGraph:
        if graph_version not in self._builders:
            raise WorkflowGraphVersionMismatchError(
                thread_id="",
                checkpoint_graph_version="",
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
    ) -> TaskWorkflowResult:
        if thread_id in self._thread_versions:
            raise WorkflowThreadAlreadyExistsError(thread_id)
        compiled = self._graph(graph_version)
        self._thread_versions[thread_id] = graph_version
        payload = await compiled.ainvoke(
            state.model_dump(),
            _config(thread_id),
        )
        return await self._result(compiled, thread_id, graph_version, payload)

    async def resume(
        self,
        *,
        thread_id: Identifier,
        graph_version: GraphVersion,
    ) -> TaskWorkflowResult:
        written_by = self._thread_versions.get(thread_id)
        if written_by is None:
            raise WorkflowThreadNotFoundError(thread_id)
        if written_by != graph_version:
            # The checkpoint is left exactly as it was: a mismatched resume is
            # a migration decision, and continuing under another graph would
            # silently re-enter a node that means something else now.
            raise WorkflowGraphVersionMismatchError(
                thread_id=thread_id,
                checkpoint_graph_version=written_by,
                requested_graph_version=graph_version,
            )
        compiled = self._graph(graph_version)
        # No initial state: it already belongs to the checkpoint, and passing
        # it again is how the original input gets appended twice.
        payload = await compiled.ainvoke(None, _config(thread_id))
        return await self._result(compiled, thread_id, graph_version, payload)

    async def _result(
        self,
        compiled: CompiledGraph,
        thread_id: Identifier,
        graph_version: GraphVersion,
        payload: Mapping[str, Any],
    ) -> TaskWorkflowResult:
        snapshot = await compiled.aget_state(_config(thread_id))
        pending = tuple(snapshot.next)
        return TaskWorkflowResult(
            thread_id=thread_id,
            graph_version=graph_version,
            disposition="interrupted" if pending else "completed",
            state=_to_state(payload),
            next_nodes=pending,
        )


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


__all__ = [
    "GRAPH_BUILDERS",
    "GraphBuilder",
    "GraphState",
    "LangGraphTaskWorkflow",
    "NodeHandler",
    "build_v1_graph",
]
