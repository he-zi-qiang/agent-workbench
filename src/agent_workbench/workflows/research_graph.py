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
* a quality gate that has run out of revisions returns **no next node** and a
  stable failure reason.  Routing an exhausted budget to approval would
  approve a draft the critic rejected, which turns the gate into a formality
  precisely when it matters.
* the approval gate routes the same way, for the same reason.  A rejection is a
  path through the graph rather than the absence of one, and the path it takes
  is **not** the export: a gate whose rejection still exported would be a
  formality too.  So ``approval`` is a conditional node, and a rejected decision
  returns no next node together with its own terminal failure reason.
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
CONDITIONAL_NODES: Final[frozenset[TaskNodeId]] = frozenset(
    {"route", "quality_gate", "approval"}
)

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
    # Conditional; see route_approval.  Listed with no static successor so the
    # edge table stays the single source of "which nodes exist".
    "approval": (),
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


class MissingApprovalError(RuntimeError):
    """Raised when the approval gate routes before a decision was recorded.

    Failing is the only safe answer.  The two alternatives are exporting on an
    approval nobody gave, and treating "no answer yet" as a rejection -- and a
    graph that reached this router has already left its interrupt behind, so
    "wait" is not a value this function can return.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"approval gate requires a recorded decision: {task_id}")


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
    """Return the next node after the quality gate, or ``None`` to stop here.

    ``None`` has two meanings, and they are told apart by
    ``terminal_failure_reason`` rather than by this return value:

    * the critic still wants changes and the revision budget is spent -- a
      failure, and deliberately not ``approval``, because the caller has to
      decide the Task failed rather than reach the gate by ignoring a value;
    * the work passed and this Task was never asked for a file -- a success.
      Routing to ``approval`` here would interrupt a human to authorize an
      export nobody wanted, and then write one.

    The draft is written either way.  ``wants_report`` decides whether it also
    becomes a downloadable artifact, not whether the Task produced anything.

    ``export_requires_approval`` decides which of the two remaining paths a
    wanted report takes.  This function used to route to ``approval``
    unconditionally, which meant v1 asked for an approval that
    ``config.default.toml`` and every local profile had already answered
    ``false`` -- ADR-038 taught v2's ``route_review`` to read the field and
    ADR-048 flipped the default, but neither touched this line.  Observed as a
    Task that stopped to ask whether the submitter approved handing a file to
    themselves, and settled ``failed`` when they said no.
    """

    review = state.review_result
    if review is None:
        raise MissingReviewError(state.task_id)
    if review.decision == "pass":
        if not state.wants_report:
            return None
        # Read exactly as v2 reads it, deliberately: the gate is skipped rather
        # than faked, so no approval row is opened and nothing downstream reads
        # a decision nobody made. `route_approval` below still refuses to export
        # without one, which is what keeps this the only way past it.
        return "approval" if state.export_requires_approval else "export"
    return "synthesize" if state.can_revise else None


def quality_gate_failure_reason(state: TaskState) -> str | None:
    """Return the terminal failure recorded by an exhausted quality gate.

    ``None`` is deliberately different from the graph reaching ``END``: the
    latter is only a framework control-flow detail.  This value is carried
    across the workflow port and checkpoint inspection boundary so a Worker
    can mark the product Task failed rather than inferring success from an
    empty pending-node list.

    A gate that passed returns ``None`` here whether or not it routed onward,
    which is what makes "passed, no file wanted" terminate as a success.
    """

    review = state.review_result
    if review is None or review.decision != "revise" or state.can_revise:
        return None
    return (
        "the critic requested another revision after the revision budget was exhausted"
    )


def route_approval(state: TaskState) -> TaskNodeId | None:
    """Return the next node after the approval gate, or ``None`` to fail.

    ``None`` is a rejection.  It is deliberately not ``export``: the export is
    the Task's one externally visible write, and reaching it after a human said
    no would make the approval a formality.  The caller has to decide the Task
    failed, and cannot reach the export by ignoring a value.
    """

    if state.approval_decision is None:
        raise MissingApprovalError(state.task_id)
    return "export" if state.approval_decision == "approved" else None


def approval_failure_reason(state: TaskState) -> str | None:
    """Return the terminal failure recorded by a rejected approval."""

    if state.approval_decision != "rejected":
        return None
    return "a human rejected the approval required before export"


def terminal_failure_reason(state: TaskState) -> str | None:
    """Return why this state is a *deliberate* terminal failure, if it is.

    One entry point, because a caller that had to remember both gates would
    eventually remember one.  The two are mutually exclusive by construction --
    an exhausted budget requires the critic's verdict to be ``revise``, and an
    approval requires it to be ``pass`` -- so their order here is not a
    precedence rule, and a test pins that rather than trusting the reading.
    """

    return quality_gate_failure_reason(state) or approval_failure_reason(state)


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
    if node == "approval":
        approved = route_approval(state)
        return () if approved is None else (approved,)
    return _STATIC_EDGES[node]


def evolve(state: TaskState, **changes: object) -> TaskState:
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
    return evolve(
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
    return evolve(
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
    "MissingApprovalError",
    "MissingReviewError",
    "ResearchContribution",
    "approval_failure_reason",
    "begin_revision",
    "declared_nodes",
    "evolve",
    "fan_in",
    "merge_refs",
    "next_nodes",
    "quality_gate_failure_reason",
    "route_approval",
    "route_quality_gate",
    "route_research",
    "terminal_failure_reason",
]
