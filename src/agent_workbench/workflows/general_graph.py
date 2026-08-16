"""Control flow for the v2 general graph: understand, work, review, export.

The second graph exists because v1's shape is an argument, not a container
(ADR-031). "Two researchers fan out, a synthesizer writes, a critic reviews"
encodes what a *research report* is; a Task that is asked to convert a
spreadsheet, or to fix a script until it runs, is made to pretend it is
writing a report -- it plans research it does not need, fans out to two
branches with nothing to find, and produces a draft where the answer was
supposed to be a file.

v2 says less. One node does the work, one reviews it, and the loop between
them is the whole method:

    understand -> work -> review -> export
                   ^        |
                   +--------+   (review sends it back)

What it deliberately does not have is a plan node. v1 plans because it must
decide what to research before it can fan out; v2's ``work`` node holds tools
and decides its own next step every turn, so a plan written in advance would
be a second, staler opinion about an order the node is already choosing --
and one that nothing revisits when the first tool call disproves it.

Same conventions as ``research_graph``, and for the same reasons: edges are
data, routing is pure functions, and the adapter compiles this declaration
rather than holding a second copy of it. Neither module imports the other.
Three of the four node ids are shared with v1 and none of the shape is, so a
helper spanning both would have to be written in terms of what they have in
common -- which is almost nothing, and would be the place a change to one
graph leaked into the other.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from agent_workbench.domain.tasks import CANONICAL_V2_NODE_IDS, TaskNodeId, TaskState
from agent_workbench.ports.task_workflow import GraphVersion

GRAPH_VERSION_V2: Final[GraphVersion] = "v2_general"

ENTRY_NODE: Final[TaskNodeId] = "understand"
TERMINAL_NODE: Final[TaskNodeId] = "export"

#: Nodes whose successor depends on state. As in v1, a node absent from the
#: static table with no router is terminal.
CONDITIONAL_NODES: Final[frozenset[TaskNodeId]] = frozenset({"review", "approval"})

_STATIC_EDGES: Final[dict[TaskNodeId, tuple[TaskNodeId, ...]]] = {
    "understand": ("work",),
    "work": ("review",),
    # Conditional; see route_review. Listed with no static successor so the
    # edge table stays the single source of "which nodes exist".
    "review": (),
    "approval": (),
    "export": (),
}

#: Read-only for the same reason v1's is: an edge table a caller can mutate is
#: one that can disagree with the compiled graph at runtime.
STATIC_EDGES: Final[Mapping[TaskNodeId, tuple[TaskNodeId, ...]]] = MappingProxyType(
    _STATIC_EDGES
)


class MissingReviewError(RuntimeError):
    """The review router ran before ``work`` produced a review result."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"v2 review requires a review result: {task_id}")


class MissingApprovalError(RuntimeError):
    """The approval router ran before a decision was recorded.

    Verbatim the v1 reasoning, and deliberately a separate class: the two
    graphs fail here for the same reason but a caller catching one must not
    silently catch the other, because the recovery paths differ.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"v2 approval requires a recorded decision: {task_id}")


def route_review(state: TaskState) -> TaskNodeId | None:
    """The next node after review, or ``None`` to stop here.

    The back edge to ``work`` shares v1's revision budget rather than getting
    one of its own (ADR-031). It is the same question -- how many times may a
    reviewer send this back before the Task is failed rather than looped -- and
    two budgets would be two places to change it, with the second one found by
    a Task that never terminated.

    ``None`` means what it means in v1 since ADR-060: nobody asked for a
    file, so the graph is done -- whether the reviewer passed the draft or
    ran out of revisions disputing it.
    """

    review = state.review_result
    if review is None:
        raise MissingReviewError(state.task_id)
    if review.decision == "pass":
        if not state.wants_report:
            return None
        # Straight to export when the deployment does not gate it. The gate is
        # skipped, not faked: no approval row is opened, so nothing later reads
        # a decision nobody made. `route_approval` still refuses to export
        # without one, which is what keeps this the only way past it.
        return "approval" if state.export_requires_approval else "export"
    if state.can_revise:
        return "work"
    # Out of revisions. Same routing as a pass, same reason as v1's quality
    # gate (ADR-060): an exhausted reviewer annotates rather than vetoes, and
    # the verdict travels with the draft as `state.unresolved_review`.
    if not state.wants_report:
        return None
    return "approval" if state.export_requires_approval else "export"


def revision_update(state: TaskState) -> dict[str, object]:
    """The state change that closes the review the work node is answering.

    Deliberately a *result* rather than a new state, and that is the whole
    difference from v1's ``begin_revision``. v1 hands the writer a state with
    the critic's verdict already removed, so the rewrite happens without
    knowing what was wrong with the draft -- tolerable there, because the
    writer re-derives a document from evidence it can see in full.

    v2's loop is the whole method: ``review`` sends work back *because of*
    something, and a second attempt that cannot read the complaint is a coin
    flip rather than a fix. So the work node runs against the state as
    checkpointed, review and all, and this closes it afterwards -- which is
    also the only ordering ``TaskState`` permits, since a stored review must
    describe the current ``revision_count``.
    """

    if not state.can_revise:
        raise ValueError("revision budget is exhausted")
    return {"revision_count": state.revision_count + 1, "review_result": None}


def route_approval(state: TaskState) -> TaskNodeId | None:
    """Export on approval, nowhere on rejection.

    A rejection is a path through the graph rather than the absence of one,
    and the path it takes is not the export -- a gate whose rejection still
    exported would be a formality.
    """

    decision = state.approval_decision
    if decision is None:
        raise MissingApprovalError(state.task_id)
    return "export" if decision == "approved" else None


def approval_failure_reason(state: TaskState) -> str | None:
    """The terminal failure recorded by a rejected approval."""

    decision = state.approval_decision
    if decision is None:
        raise MissingApprovalError(state.task_id)
    return None if decision == "approved" else "export was rejected by a reviewer"


def terminal_failure_reason(state: TaskState) -> str | None:
    """Why this state stops without exporting, or ``None`` if it succeeded.

    Since ADR-060 the review gate no longer stops the graph -- an exhausted
    reviewer annotates rather than vetoes -- so the rejected approval is the
    only deliberate failure left. The entry point stays, so the caller keeps
    asking one question even as the answers change underneath.
    """

    if state.approval_decision is not None:
        return approval_failure_reason(state)
    return None


def next_nodes(node: TaskNodeId, state: TaskState) -> tuple[TaskNodeId, ...]:
    """Every successor of ``node`` for ``state``.

    One entry point for static and conditional edges both, so a caller cannot
    handle the table and forget the two nodes that route on state.
    """

    if node == "review":
        target = route_review(state)
        return () if target is None else (target,)
    if node == "approval":
        approved = route_approval(state)
        return () if approved is None else (approved,)
    return _STATIC_EDGES[node]


def declared_nodes() -> frozenset[TaskNodeId]:
    """Every node this module routes into or out of."""

    reachable: set[TaskNodeId] = {ENTRY_NODE, *_STATIC_EDGES}
    reachable.update(*_STATIC_EDGES.values())
    # The two conditional targets, which no static edge names.
    reachable.update({"work", "approval", "export"})
    return frozenset(reachable)


__all__ = [
    "CANONICAL_V2_NODE_IDS",
    "CONDITIONAL_NODES",
    "ENTRY_NODE",
    "GRAPH_VERSION_V2",
    "STATIC_EDGES",
    "TERMINAL_NODE",
    "MissingApprovalError",
    "MissingReviewError",
    "approval_failure_reason",
    "declared_nodes",
    "next_nodes",
    "revision_update",
    "route_approval",
    "route_review",
    "terminal_failure_reason",
]
