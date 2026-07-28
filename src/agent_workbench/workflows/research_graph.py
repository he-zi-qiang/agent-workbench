"""Control flow for the fixed v1 research graph.

Edges live here as data and routing lives here as pure functions, because a
routing decision embedded in a compiled graph can only be tested by running
that graph.  The LangGraph adapter compiles this declaration; it must not hold
a second, differently-worded copy of the same decision.

Two properties are load bearing for recovery and are therefore established in
this module rather than in the adapter:

* fan-in is a **sorted union**, so the merged state does not depend on which
  research branch finished first, and re-merging an already merged
  contribution changes nothing.  A checkpoint written after a crash mid-fan-in
  therefore converges instead of accumulating duplicates.
* a quality gate that has run out of revisions returns **no next node**.
  Routing an exhausted budget to approval would approve a draft the critic
  rejected, which turns the gate into a formality precisely when it matters.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.tasks import CANONICAL_V1_NODE_IDS, TaskNodeId, TaskState
from agent_workbench.ports.task_workflow import GraphVersion

GRAPH_VERSION_V1: Final[GraphVersion] = "v1"

ENTRY_NODE: Final[TaskNodeId] = "understand"
TERMINAL_NODE: Final[TaskNodeId] = "export"

# Nodes whose successor depends on state.  Everything else has one static
# successor, and a node with no entry here is terminal.
CONDITIONAL_NODES: Final[frozenset[TaskNodeId]] = frozenset({"route", "quality_gate"})

# The fan-in point: both research branches converge on synthesize.
RESEARCH_BRANCHES: Final[tuple[TaskNodeId, ...]] = (
    "research_internal",
    "research_external",
)

_STATIC_EDGES: Final[dict[TaskNodeId, tuple[TaskNodeId, ...]]] = {
    "understand": ("plan",),
    "plan": ("route",),
    "research_internal": ("synthesize",),
    "research_external": ("synthesize",),
    "synthesize": ("critic",),
    "critic": ("quality_gate",),
    "approval": ("export",),
    "export": (),
}

# Exposed read-only: an edge table a caller can mutate is an edge table that
# can disagree with the compiled graph at runtime.
STATIC_EDGES: Final[Mapping[TaskNodeId, tuple[TaskNodeId, ...]]] = MappingProxyType(
    _STATIC_EDGES
)


class EmptyPlanError(RuntimeError):
    """Raised when routing is asked to fan out research with no plan.

    v1's route is a fixed fan-out rather than a choice, so the only thing it
    can refuse is a state that never planned.  Fanning out anyway would spend
    two agent invocations deciding what to research.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"cannot route research without a plan: {task_id}")


class MissingReviewError(RuntimeError):
    """Raised when the quality gate runs before the critic produced a review."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"quality gate requires a review result: {task_id}")


def route_research(state: TaskState) -> tuple[TaskNodeId, ...]:
    """Return the research branches to run in parallel.

    v1 always fans out to both branches.  Narrowing this to a subset is a
    different graph, not a tuning knob: the critic and the synthesizer are
    written against both evidence sources, so dropping one silently changes
    what an answer is grounded in.  Such a change must bump ``graph_version``.
    """

    if not state.plan:
        raise EmptyPlanError(state.task_id)
    return RESEARCH_BRANCHES


def route_quality_gate(state: TaskState) -> TaskNodeId | None:
    """Return the next node after the quality gate, or ``None`` to fail.

    ``None`` means the critic still wants changes and the revision budget is
    spent.  It is deliberately not ``approval``: the caller has to decide that
    the Task failed, and cannot reach the approval node by ignoring a value.
    """

    review = state.review_result
    if review is None:
        raise MissingReviewError(state.task_id)
    if review.decision == "pass":
        return "approval"
    return "synthesize" if state.can_revise else None


def next_nodes(node: TaskNodeId, state: TaskState) -> tuple[TaskNodeId, ...]:
    """Return every successor of ``node`` for ``state``.

    One entry point for both static and conditional edges, so a caller cannot
    handle the static table and forget that two nodes route on state.
    """

    if node == "route":
        return route_research(state)
    if node == "quality_gate":
        target = route_quality_gate(state)
        return () if target is None else (target,)
    return _STATIC_EDGES[node]


def _evolve(state: TaskState, **changes: object) -> TaskState:
    """Return a re-validated copy of ``state``.

    ``model_copy`` is deliberately not used.  It skips validation, so a reducer
    that produced an unsorted or duplicated reference tuple would be stored
    rather than rejected -- exactly the class of bug ``TaskState``'s validators
    exist to catch, and one that would only surface later as a checkpoint that
    cannot be re-read.
    """

    return TaskState.model_validate({**state.model_dump(), **changes})


def begin_revision(state: TaskState) -> TaskState:
    """Advance the revision counter and drop the review being answered.

    The review has to go.  ``TaskState`` requires a stored review to describe
    the current ``revision_count``, so keeping it would either fail validation
    or, worse, let the next quality gate read a stale verdict about a draft
    that has since been rewritten.
    """

    if not state.can_revise:
        raise ValueError("revision budget is exhausted")
    return _evolve(
        state,
        revision_count=state.revision_count + 1,
        review_result=None,
    )


@dataclass(frozen=True, slots=True)
class ResearchContribution:
    """What one research branch adds to the state.

    A branch returns only its own contribution rather than a whole state, so
    two branches running concurrently cannot each claim to know the value of a
    field the other one is also writing.
    """

    evidence_refs: tuple[Identifier, ...] = ()
    agent_outcome_refs: tuple[Identifier, ...] = ()


def merge_refs(*contributions: Iterable[Identifier]) -> tuple[Identifier, ...]:
    """Merge reference tuples into the canonical sorted union.

    Sorted because ``TaskState`` stores references in canonical order; a union
    because fan-in must be idempotent.  Together these make the merge
    commutative, associative and idempotent, which is what lets a retried or
    replayed branch converge instead of duplicating its own output.
    """

    merged: set[Identifier] = set()
    for contribution in contributions:
        merged.update(contribution)
    return tuple(sorted(merged))


def fan_in(
    state: TaskState,
    contributions: Iterable[ResearchContribution],
) -> TaskState:
    """Merge parallel research contributions into ``state``.

    The result is independent of iteration order, so the graph runner may hand
    the branches over in completion order without changing the checkpoint.
    """

    collected = tuple(contributions)
    return _evolve(
        state,
        evidence_refs=merge_refs(
            state.evidence_refs,
            *(item.evidence_refs for item in collected),
        ),
        agent_outcome_refs=merge_refs(
            state.agent_outcome_refs,
            *(item.agent_outcome_refs for item in collected),
        ),
    )


def declared_nodes() -> frozenset[TaskNodeId]:
    """Return every node this module routes into or out of."""

    reachable: set[TaskNodeId] = {ENTRY_NODE, *_STATIC_EDGES}
    reachable.update(*_STATIC_EDGES.values())
    reachable.update(RESEARCH_BRANCHES)
    reachable.update({"synthesize", "approval"})
    return frozenset(reachable)


__all__ = [
    "CANONICAL_V1_NODE_IDS",
    "CONDITIONAL_NODES",
    "ENTRY_NODE",
    "GRAPH_VERSION_V1",
    "RESEARCH_BRANCHES",
    "STATIC_EDGES",
    "TERMINAL_NODE",
    "EmptyPlanError",
    "MissingReviewError",
    "ResearchContribution",
    "begin_revision",
    "declared_nodes",
    "fan_in",
    "merge_refs",
    "next_nodes",
    "route_quality_gate",
    "route_research",
]
