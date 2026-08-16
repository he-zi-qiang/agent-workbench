"""The v2 general graph's shape, and the ways it must not resemble v1.

Two kinds of assertion live here. The first kind pins v2's own routing: the
back edge, the two ways it can stop, and the gates that refuse to guess. The
second kind is the one this file exists for -- **v1 is unchanged**. A second
graph is only worth having if adding it could not have moved the first, and
that is a property nothing else in the suite is positioned to notice.
"""

from __future__ import annotations

import pytest

from agent_workbench.domain.tasks import (
    CANONICAL_V1_NODE_IDS,
    CANONICAL_V2_NODE_IDS,
    ReviewResult,
    TaskState,
)
from agent_workbench.workflows import general_graph, research_graph
from agent_workbench.workflows.general_graph import (
    CONDITIONAL_NODES,
    ENTRY_NODE,
    GRAPH_VERSION_V2,
    STATIC_EDGES,
    TERMINAL_NODE,
    MissingApprovalError,
    MissingReviewError,
    approval_failure_reason,
    declared_nodes,
    next_nodes,
    route_approval,
    route_review,
    terminal_failure_reason,
)


def _state(**overrides: object) -> TaskState:
    base: dict[str, object] = {
        "task_id": "task_1",
        "objective": "Turn the attached spreadsheet into a cleaned CSV.",
    }
    base.update(overrides)
    return TaskState.model_validate(base)


def _reviewed(
    decision: str,
    *,
    revision_count: int = 0,
    max_revisions: int = 2,
    wants_report: bool = True,
) -> TaskState:
    return _state(
        draft_ref="draft_1",
        revision_count=revision_count,
        max_revisions=max_revisions,
        wants_report=wants_report,
        review_result=ReviewResult(
            decision=decision,
            reviewed_draft_ref="draft_1",
            revision_number=revision_count,
            summary="Reviewed the work.",
            issues=() if decision == "pass" else ("The script still fails.",),
            score=80 if decision == "pass" else 40,
        ),
    )


# --- the shape ---------------------------------------------------------------


def test_the_graph_is_four_nodes_plus_the_shared_approval_gate() -> None:
    """ADR-031 §2.1 counts four, and this tuple has five.

    Four is the shape -- understand, work, review, export. The fifth is the
    human gate on the export path, and it belongs in this tuple because the
    tuple is not the shape: it is every node id a checkpoint of this graph may
    sit on, and a v2 Task paused for a human sits on exactly that one.
    """

    assert CANONICAL_V2_NODE_IDS == (
        "understand",
        "work",
        "review",
        "approval",
        "export",
    )
    assert ENTRY_NODE == "understand"
    assert TERMINAL_NODE == "export"


def test_there_is_no_plan_node() -> None:
    """Stated as an assertion because it is a decision, not an omission.

    v1 plans because it has to decide what to research before it can fan out.
    v2's work node chooses its own next step every turn, so a plan written in
    advance would be a second and staler opinion about that order.
    """

    assert "plan" not in declared_nodes()
    assert "plan" not in CANONICAL_V2_NODE_IDS


def test_understand_leads_to_work_and_work_to_review() -> None:
    assert next_nodes("understand", _state()) == ("work",)
    assert next_nodes("work", _state()) == ("review",)


def test_only_review_and_approval_route_on_state() -> None:
    assert frozenset({"review", "approval"}) == CONDITIONAL_NODES
    # And the control: every conditional node is one this module can route.
    for node in CONDITIONAL_NODES:
        assert node in declared_nodes()


# --- the back edge, and the budget it shares with v1 -------------------------


def test_a_review_that_wants_changes_sends_the_work_back() -> None:
    assert route_review(_reviewed("revise")) == ("work")
    assert next_nodes("review", _reviewed("revise")) == ("work",)


def test_the_back_edge_stops_when_the_shared_revision_budget_is_spent() -> None:
    """Not a budget of its own (ADR-031). Two budgets would be two places to
    change the same question, and the second one is found by a Task that never
    terminated.

    Stopping is no longer failing (ADR-060): the spent budget routes onward
    exactly as a pass would, and the reviewer's standing verdict travels as
    ``unresolved_review`` instead of vanishing into a terminal failure.
    """

    spent = _reviewed("revise", revision_count=2, max_revisions=2)

    assert route_review(spent) == "approval"
    assert next_nodes("review", spent) == ("approval",)
    assert terminal_failure_reason(spent) is None
    assert spent.unresolved_review is not None


def test_a_revision_still_in_budget_carries_no_unresolved_review() -> None:
    """The control for the caveat above: a live loop is not an annotated one."""

    assert _reviewed("revise", revision_count=1).unresolved_review is None


# --- the two ways it stops ---------------------------------------------------


def test_passing_work_that_was_asked_for_a_file_goes_to_approval() -> None:
    assert route_review(_reviewed("pass")) == "approval"


def test_a_deployment_that_does_not_gate_exports_goes_straight_to_export() -> None:
    """The gate is skipped, not faked.

    What export writes is a versioned artifact in this tenant's own store,
    behind the same per-principal authorization as everything else; nothing
    reaches anyone until a human clicks download. A deployment whose Tasks
    produce files for the person who asked for them can say so, and this is
    what that says.
    """

    ungated = _reviewed("pass").model_copy(update={"export_requires_approval": False})

    assert route_review(ungated) == "export"
    # No approval row was opened, so nothing downstream reads a decision that
    # nobody made -- `route_approval` still refuses without one, which is what
    # keeps routing past it impossible by any other path.
    assert ungated.approval_id is None
    assert ungated.approval_decision is None


def test_the_gate_is_on_unless_a_deployment_turns_it_off() -> None:
    """The control. `TaskState` defaults it True for checkpoints written before
    the field existed, and those Tasks paused at a gate they must still find on
    resume -- a default that flipped would export work a human was mid-review
    of."""

    assert _state().export_requires_approval is True
    assert route_review(_reviewed("pass")) == "approval"


def test_passing_work_nobody_asked_a_file_for_stops_without_approving() -> None:
    """``None`` here is success, not failure -- and the pair of assertions is
    what says so. Interrupting a human to authorise an export nobody wanted,
    and then writing one, is the alternative."""

    passed = _reviewed("pass", wants_report=False)

    assert route_review(passed) is None
    assert terminal_failure_reason(passed) is None


def test_a_rejected_approval_does_not_export() -> None:
    rejected = _reviewed("pass").model_copy(update={"approval_decision": "rejected"})

    assert route_approval(rejected) is None
    assert approval_failure_reason(rejected) == "export was rejected by a reviewer"


def test_an_approved_task_exports() -> None:
    """The control: the gate must be able to say yes."""

    approved = _reviewed("pass").model_copy(update={"approval_decision": "approved"})

    assert route_approval(approved) == "export"
    assert terminal_failure_reason(approved) is None


# --- the gates refuse to guess ----------------------------------------------


def test_reviewing_before_there_is_a_review_refuses() -> None:
    with pytest.raises(MissingReviewError):
        route_review(_state())


def test_approving_before_a_decision_was_recorded_refuses() -> None:
    """Failing is the only safe answer: the alternatives are exporting on an
    approval nobody gave, and reading "no answer yet" as a rejection."""

    with pytest.raises(MissingApprovalError):
        route_approval(_reviewed("pass"))


# --- v1 is unchanged ---------------------------------------------------------


def test_the_v1_node_tuple_is_untouched() -> None:
    """The regression guard this whole file is really for."""

    assert CANONICAL_V1_NODE_IDS == (
        "understand",
        "plan",
        "route",
        "research_internal",
        "research_external",
        "synthesize",
        "critic",
        "quality_gate",
        "approval",
        "export",
    )


def test_v1_still_declares_exactly_its_own_nodes() -> None:
    """v2's ids were added to the shared ``TaskNodeId`` union, so the risk is
    that they leaked into what v1 says it can reach."""

    assert "work" not in research_graph.declared_nodes()
    assert "review" not in research_graph.declared_nodes()
    assert research_graph.declared_nodes() == frozenset(CANONICAL_V1_NODE_IDS)


def test_the_two_graphs_have_different_versions() -> None:
    assert GRAPH_VERSION_V2 == "v2_general"
    assert research_graph.GRAPH_VERSION_V1 == "v1"
    assert GRAPH_VERSION_V2 != research_graph.GRAPH_VERSION_V1


def test_neither_graph_module_imports_the_other() -> None:
    """Three of four node ids are shared and none of the shape is.

    A helper spanning both would have to be written in terms of what they have
    in common, which is almost nothing -- and would be exactly where a change
    to one graph leaked into the other.
    """

    assert "general_graph" not in research_graph.__dict__
    assert "research_graph" not in general_graph.__dict__


def test_the_two_edge_tables_agree_with_neither_being_the_others_subset() -> None:
    """Guards against v2 having been written by copying v1's table."""

    assert STATIC_EDGES["understand"] == ("work",)
    assert research_graph.STATIC_EDGES["understand"] == ("plan",)
