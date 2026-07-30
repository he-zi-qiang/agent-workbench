"""Control-flow contract for the fixed v1 research graph.

Every edge in the architecture baseline's diagram is asserted here, including
the three the quality gate chooses between.  The graph is plain Python at this
layer, so these run without compiling a graph or reaching a database.
"""

from __future__ import annotations

import itertools

import pytest
from pydantic import ValidationError

from agent_workbench.domain.tasks import (
    CANONICAL_V1_NODE_IDS,
    ReviewResult,
    TaskState,
    TaskStep,
)
from agent_workbench.workflows import research_graph
from agent_workbench.workflows.research_graph import (
    CONDITIONAL_NODES,
    ENTRY_NODE,
    RESEARCH_BRANCHES,
    STATIC_EDGES,
    TERMINAL_NODE,
    EmptyPlanError,
    MissingReviewError,
    ResearchContribution,
    begin_revision,
    declared_nodes,
    fan_in,
    merge_refs,
    next_nodes,
    quality_gate_failure_reason,
    route_quality_gate,
    route_research,
)


def _planned_state(**overrides: object) -> TaskState:
    base: dict[str, object] = {
        "task_id": "task_1",
        "objective": "Compare retrieval strategies.",
        "plan": (
            TaskStep(step_id="step_1", sequence=1, objective="Gather internal notes."),
            TaskStep(step_id="step_2", sequence=2, objective="Survey public work."),
        ),
    }
    base.update(overrides)
    return TaskState.model_validate(base)


def _reviewed_state(
    decision: str,
    *,
    revision_count: int = 0,
    max_revisions: int = 2,
) -> TaskState:
    return _planned_state(
        draft_ref="draft_1",
        revision_count=revision_count,
        max_revisions=max_revisions,
        review_result=ReviewResult(
            decision=decision,
            reviewed_draft_ref="draft_1",
            revision_number=revision_count,
            summary="Reviewed the draft.",
            issues=() if decision == "pass" else ("Evidence is thin.",),
            score=80 if decision == "pass" else 40,
        ),
    )


# --------------------------------------------------------------------------
# Graph shape
# --------------------------------------------------------------------------


def test_the_declared_graph_covers_exactly_the_canonical_node_set() -> None:
    # A node added to the graph without a TaskNodeId would be unrepresentable
    # in a checkpoint; a TaskNodeId with no edges would be unreachable.
    assert declared_nodes() == set(CANONICAL_V1_NODE_IDS)


def test_entry_and_terminal_nodes_match_the_baseline_diagram() -> None:
    assert ENTRY_NODE == "understand"
    assert TERMINAL_NODE == "export"
    assert STATIC_EDGES[TERMINAL_NODE] == ()


def test_conditional_nodes_are_absent_from_the_static_edge_table() -> None:
    # If a conditional node also had a static edge, a caller reading the table
    # directly would silently take an edge the router never chose.
    for node in CONDITIONAL_NODES:
        assert node not in STATIC_EDGES


def test_the_static_edges_match_the_baseline_diagram() -> None:
    assert STATIC_EDGES["understand"] == ("plan",)
    assert STATIC_EDGES["plan"] == ("route",)
    assert STATIC_EDGES["research_internal"] == ("synthesize",)
    assert STATIC_EDGES["research_external"] == ("synthesize",)
    assert STATIC_EDGES["synthesize"] == ("critic",)
    assert STATIC_EDGES["critic"] == ("quality_gate",)
    assert STATIC_EDGES["approval"] == ("export",)


def test_the_edge_table_cannot_be_mutated_by_a_caller() -> None:
    with pytest.raises(TypeError):
        STATIC_EDGES["approval"] = ("export", "understand")  # type: ignore[index]


# --------------------------------------------------------------------------
# route: the fan-out edge
# --------------------------------------------------------------------------


def test_route_fans_out_to_both_research_branches() -> None:
    assert route_research(_planned_state()) == RESEARCH_BRANCHES
    assert next_nodes("route", _planned_state()) == (
        "research_internal",
        "research_external",
    )


def test_route_fails_closed_without_a_plan() -> None:
    with pytest.raises(EmptyPlanError) as captured:
        route_research(
            TaskState(task_id="task_1", objective="Compare retrieval strategies.")
        )
    assert captured.value.task_id == "task_1"


# --------------------------------------------------------------------------
# quality_gate: the three conditional edges
# --------------------------------------------------------------------------


def test_a_passing_review_routes_to_approval() -> None:
    state = _reviewed_state("pass")
    assert route_quality_gate(state) == "approval"
    assert next_nodes("quality_gate", state) == ("approval",)


def test_a_revise_review_routes_back_to_synthesize_while_budget_remains() -> None:
    state = _reviewed_state("revise", revision_count=0, max_revisions=2)
    assert route_quality_gate(state) == "synthesize"
    assert next_nodes("quality_gate", state) == ("synthesize",)


def test_an_exhausted_revision_budget_routes_nowhere_rather_than_approving() -> None:
    # The control group is the test above: the only difference is the budget.
    # Returning "approval" here would approve a draft the critic rejected.
    exhausted = _reviewed_state("revise", revision_count=2, max_revisions=2)

    assert route_quality_gate(exhausted) is None
    assert next_nodes("quality_gate", exhausted) == ()
    assert quality_gate_failure_reason(exhausted) is not None


def test_only_an_exhausted_revise_verdict_is_a_terminal_failure() -> None:
    assert quality_gate_failure_reason(_reviewed_state("pass")) is None
    assert (
        quality_gate_failure_reason(
            _reviewed_state("revise", revision_count=0, max_revisions=1)
        )
        is None
    )


def test_quality_gate_fails_closed_before_the_critic_has_reviewed() -> None:
    with pytest.raises(MissingReviewError) as captured:
        route_quality_gate(_planned_state())
    assert captured.value.task_id == "task_1"


# --------------------------------------------------------------------------
# The revise transition
# --------------------------------------------------------------------------


def test_beginning_a_revision_advances_the_counter_and_drops_the_review() -> None:
    state = _reviewed_state("revise", revision_count=0, max_revisions=2)
    revised = begin_revision(state)

    assert revised.revision_count == 1
    # The stale verdict must not survive: it describes a draft that the next
    # synthesize pass is about to replace.
    assert revised.review_result is None
    assert revised.draft_ref == "draft_1"


def test_beginning_a_revision_refuses_an_exhausted_budget() -> None:
    exhausted = _reviewed_state("revise", revision_count=2, max_revisions=2)
    with pytest.raises(ValueError, match="revision budget is exhausted"):
        begin_revision(exhausted)


def test_the_revise_loop_terminates_at_the_configured_bound() -> None:
    state = _reviewed_state("revise", revision_count=0, max_revisions=2)

    taken = 0
    while route_quality_gate(state) == "synthesize":
        state = begin_revision(state)
        taken += 1
        # Re-entering the gate needs a fresh review of the new draft.
        state = _reviewed_state(
            "revise",
            revision_count=state.revision_count,
            max_revisions=2,
        )

    assert taken == 2
    assert route_quality_gate(state) is None


# --------------------------------------------------------------------------
# fan-in reducer
# --------------------------------------------------------------------------


def test_merge_refs_returns_a_sorted_deduplicated_union() -> None:
    assert merge_refs(("b", "a"), ("c", "a")) == ("a", "b", "c")


def test_merge_refs_is_commutative_over_every_ordering() -> None:
    parts = (("b",), ("a", "c"), ("d",))
    results = {merge_refs(*ordering) for ordering in itertools.permutations(parts)}
    assert results == {("a", "b", "c", "d")}


def test_merge_refs_is_idempotent() -> None:
    once = merge_refs(("a", "b"))
    assert merge_refs(once, ("a", "b")) == once


def test_fan_in_does_not_depend_on_branch_completion_order() -> None:
    state = _planned_state()
    internal = ResearchContribution(
        evidence_refs=("ev_internal",),
        agent_outcome_refs=("run_internal",),
    )
    external = ResearchContribution(
        evidence_refs=("ev_external",),
        agent_outcome_refs=("run_external",),
    )

    first = fan_in(state, (internal, external))
    second = fan_in(state, (external, internal))

    assert first == second
    assert first.evidence_refs == ("ev_external", "ev_internal")
    assert first.agent_outcome_refs == ("run_external", "run_internal")


def test_replaying_a_branch_after_a_crash_converges_instead_of_duplicating() -> None:
    state = _planned_state()
    internal = ResearchContribution(
        evidence_refs=("ev_internal",),
        agent_outcome_refs=("run_internal",),
    )

    once = fan_in(state, (internal,))
    twice = fan_in(once, (internal,))

    assert twice == once


def test_fan_in_preserves_references_already_in_the_state() -> None:
    state = _planned_state(evidence_refs=("ev_earlier",))
    merged = fan_in(state, (ResearchContribution(evidence_refs=("ev_new",)),))

    assert merged.evidence_refs == ("ev_earlier", "ev_new")


def test_fan_in_with_no_contributions_leaves_the_state_unchanged() -> None:
    state = _planned_state(evidence_refs=("ev_earlier",))
    assert fan_in(state, ()) == state


# --------------------------------------------------------------------------
# The reducers produce states a checkpoint can be re-read from
# --------------------------------------------------------------------------


def test_fan_in_rejects_a_broken_reducer_instead_of_checkpointing_its_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The guard only matters if a reducer regresses, so the regression is
    # injected. With model_copy in place of the re-validating evolve, this
    # unsorted tuple would be stored and only fail later, when a checkpoint
    # written by this run could no longer be loaded.
    monkeypatch.setattr(
        research_graph,
        "merge_refs",
        lambda *contributions: ("b", "a"),
    )

    with pytest.raises(ValidationError):
        fan_in(_planned_state(), (ResearchContribution(evidence_refs=("ev_a",)),))


def test_fan_in_output_survives_a_checkpoint_round_trip() -> None:
    merged = fan_in(
        _planned_state(),
        (
            ResearchContribution(evidence_refs=("ev_b", "ev_a")),
            ResearchContribution(evidence_refs=("ev_a",)),
        ),
    )

    assert TaskState.model_validate(merged.model_dump()) == merged
    assert merged.evidence_refs == ("ev_a", "ev_b")
