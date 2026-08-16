"""Framework-neutral Task graph state and its checkpoint invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_workbench.domain.runs import BudgetUsage, TokenUsage
from agent_workbench.domain.tasks import (
    CANONICAL_V1_NODE_IDS,
    ReviewResult,
    TaskState,
    TaskStep,
)


def _steps() -> tuple[TaskStep, ...]:
    return (
        TaskStep(
            step_id="step_internal",
            sequence=1,
            objective="Research internal evidence",
        ),
        TaskStep(
            step_id="step_external",
            sequence=2,
            objective="Cross-check public evidence",
            depends_on=("step_internal",),
        ),
    )


def _review(**overrides: object) -> ReviewResult:
    defaults: dict[str, object] = {
        "decision": "pass",
        "reviewed_draft_ref": "art_draft_1",
        "revision_number": 0,
        "summary": "Grounded and complete.",
        "score": 92,
    }
    return ReviewResult.model_validate(defaults | overrides)


def _state(**overrides: object) -> TaskState:
    defaults: dict[str, object] = {
        "task_id": "task_1",
        "objective": "Produce a verified market brief",
        "plan": _steps(),
        "evidence_refs": ("art_evidence_a", "art_evidence_b"),
        "draft_ref": "art_draft_1",
        "review_result": _review(),
        "agent_outcome_refs": ("art_outcome_a", "art_outcome_b"),
    }
    return TaskState.model_validate(defaults | overrides)


def test_v1_node_ids_are_the_durable_canonical_contract() -> None:
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
    assert len(set(CANONICAL_V1_NODE_IDS)) == len(CANONICAL_V1_NODE_IDS)


@pytest.mark.parametrize(
    "forbidden",
    [
        {"current_step": "critic"},
        {"status": "running"},
        {"messages": [{"role": "user", "content": "large transcript"}]},
        {"provider_model": object()},
        {"graph_version": "v1"},
        {"workflow_thread_id": "thread_1"},
    ],
)
def test_graph_state_rejects_other_state_owners_and_framework_objects(
    forbidden: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _state(**forbidden)


def test_plan_order_is_contiguous_and_step_ids_are_unique() -> None:
    reversed_plan = tuple(reversed(_steps()))
    with pytest.raises(ValidationError, match="sorted with contiguous"):
        _state(plan=reversed_plan)

    duplicate = (
        TaskStep(step_id="step_same", sequence=1, objective="First"),
        TaskStep(step_id="step_same", sequence=2, objective="Second"),
    )
    with pytest.raises(ValidationError, match="step ids must be unique"):
        _state(plan=duplicate)


def test_dependencies_are_sorted_unique_and_only_point_backwards() -> None:
    with pytest.raises(ValidationError, match="depends_on must be sorted"):
        TaskStep(
            step_id="step_3",
            sequence=3,
            objective="Combine",
            depends_on=("step_2", "step_1"),
        )

    with pytest.raises(ValidationError, match="must not contain duplicate"):
        TaskStep(
            step_id="step_3",
            sequence=3,
            objective="Combine",
            depends_on=("step_1", "step_1"),
        )

    forward_reference = (
        TaskStep(
            step_id="step_1",
            sequence=1,
            objective="First",
            depends_on=("step_2",),
        ),
        TaskStep(step_id="step_2", sequence=2, objective="Second"),
    )
    with pytest.raises(ValidationError, match="preceding plan steps"):
        _state(plan=forward_reference)


@pytest.mark.parametrize("field", ["evidence_refs", "agent_outcome_refs"])
def test_state_references_must_be_sorted_and_unique(field: str) -> None:
    with pytest.raises(ValidationError, match=rf"{field} must be sorted"):
        _state(**{field: ("ref_b", "ref_a")})

    with pytest.raises(ValidationError, match=rf"{field} must not contain duplicate"):
        _state(**{field: ("ref_a", "ref_a")})


def test_review_is_bound_to_the_current_draft_and_revision() -> None:
    with pytest.raises(ValidationError, match="current draft_ref"):
        _state(review_result=_review(reviewed_draft_ref="art_old_draft"))

    with pytest.raises(ValidationError, match="must equal revision_count"):
        _state(review_result=_review(revision_number=1))

    with pytest.raises(ValidationError, match="requires a draft_ref"):
        _state(draft_ref=None)


def test_revise_requires_an_issue_and_exposes_the_cap_to_the_router() -> None:
    with pytest.raises(ValidationError, match="at least one issue"):
        _review(decision="revise")

    revisable = _state(
        revision_count=1,
        max_revisions=2,
        review_result=_review(
            decision="revise",
            revision_number=1,
            issues=("Add the missing source.",),
        ),
    )
    assert revisable.can_revise is True

    exhausted = _state(
        revision_count=2,
        max_revisions=2,
        review_result=_review(
            decision="revise",
            revision_number=2,
            issues=("Still incomplete.",),
        ),
    )
    assert exhausted.can_revise is False


def test_an_issue_has_room_for_an_actionable_instruction() -> None:
    """The regression that killed Tasks on correct verdicts.

    ``issues`` used to be ``ShortText`` (256), the type every *identifier* in
    this domain wears -- ``model_id``, ``reason_code``, ``profile_name``. It
    was the one place carrying prose another agent then works from, and the
    reviewer is told to name something specific enough to act on. Measured on
    this machine, real issues ran 65 / 181 / 376 characters, so an ordinary
    sentence of context failed the whole node on a verdict that was right.

    This is the shortest real one that failed, at 376.
    """

    real = (
        "The draft file referenced by art_50a9bb59170b424b9483ab5ab416c786 does "
        "not exist in the workspace. The working set is empty, so there is no "
        "document to evaluate against the objective (a short quarterly summary "
        "with one subtitle, two body paragraphs, and a two-row data table "
        "exported as a Word document). The next attempt must actually produce "
        "the draft file in the workspace."
    )
    assert len(real) > 256

    review = _review(decision="revise", issues=(real,))

    assert review.issues == (real,)


def test_an_issue_is_still_bounded() -> None:
    """The control. Raising a ceiling that turned out to be arbitrary is not
    the same as removing it: this text reaches a prompt, an event and a
    database row, and 32 unbounded issues would be all three at once."""

    with pytest.raises(ValidationError, match="at most 512"):
        _review(decision="revise", issues=("x" * 513,))


def test_the_other_review_constraints_did_not_move() -> None:
    """The rest of the shape is load-bearing and stayed exactly where it was.

    Without this, "fix the reviewer" could quietly become "stop checking the
    reviewer" -- the failure mode that looks like success on every run.
    """

    with pytest.raises(ValidationError, match="at least one issue"):
        _review(decision="revise", issues=())
    with pytest.raises(ValidationError, match="must not repeat"):
        _review(decision="revise", issues=("same", "same"))
    with pytest.raises(ValidationError):
        _review(decision="revise", issues=tuple(f"n{index}" for index in range(33)))

    # And the one empty list that is correct.
    assert _review(decision="pass", issues=()).issues == ()


def test_an_approval_requires_a_review_but_not_a_passing_one() -> None:
    """ADR-060: the gate may be asked about a draft the reviewer still disputes.

    An exhausted reviewer's verdict stands recorded and the draft goes to the
    gate with it, so the human decides about the work *as reviewed*. What
    stays unrepresentable is a gate with no review at all -- an approval
    about a draft nobody examined.
    """

    disputed = _state(
        approval_id="apr_1",
        approval_decision="approved",
        review_result=_review(
            decision="revise",
            issues=("Citations are incomplete.",),
        ),
    )
    assert disputed.approval_id == "apr_1"

    with pytest.raises(ValidationError, match="requires a review"):
        _state(
            approval_id="apr_1",
            approval_decision="approved",
            review_result=None,
        )

    approved = _state(approval_id="apr_1", approval_decision="approved")
    assert approved.approval_id == "apr_1"


@pytest.mark.parametrize(
    "half",
    [
        {"approval_id": "apr_1"},
        {"approval_decision": "approved"},
        {"approval_decision": "rejected"},
    ],
)
def test_an_approval_cannot_be_half_recorded(half: dict[str, str]) -> None:
    """Either the gate was answered, or it was not.

    An ``approval_id`` alone is a gate the graph walked past without an answer;
    a decision alone names no approval an auditor could look up. The control
    group is the pair below, which the same helper accepts.
    """

    with pytest.raises(ValidationError, match="travel together"):
        _state(**half)

    assert _state(approval_id="apr_1", approval_decision="rejected") is not None


def test_budget_usage_is_the_shared_runtime_value() -> None:
    usage = BudgetUsage(
        steps=4,
        tool_calls=3,
        tokens=TokenUsage(input_tokens=800, output_tokens=120),
        cost_micro_usd=4200,
    )

    state = _state(budget_usage=usage)

    assert state.budget_usage is usage
    assert state.budget_usage.tokens.total == 920


def test_task_state_round_trips_through_json_without_framework_state() -> None:
    original = _state(
        approval_id="apr_1",
        approval_decision="approved",
        budget_usage=BudgetUsage(steps=3, tool_calls=2, cost_micro_usd=17),
    )

    encoded = original.model_dump_json()
    restored = TaskState.model_validate_json(encoded)

    assert restored == original
    assert set(restored.model_dump()) == {
        "schema_version",
        "task_id",
        "objective",
        "knowledge_base_id",
        "wants_report",
        "export_requires_approval",
        "plan",
        "evidence_refs",
        "draft_ref",
        "workspace_version",
        "review_result",
        "approval_id",
        "approval_decision",
        "export_ref",
        "agent_outcome_refs",
        "budget_usage",
        "revision_count",
        "max_revisions",
    }


def test_an_export_without_an_approval_cannot_be_represented() -> None:
    """The gate is the point of the gate.

    A state carrying an export nobody approved is reachable only by a graph
    that walked past its own interrupt, and this is the cheapest place to say
    so: the checkpoint that recorded it would not load.
    """

    with pytest.raises(ValidationError, match="requires an approved"):
        _state(export_ref="art_report_1")


def test_an_export_on_a_rejected_approval_cannot_be_represented() -> None:
    with pytest.raises(ValidationError, match="requires an approved"):
        _state(
            approval_id="apr_1",
            approval_decision="rejected",
            export_ref="art_report_1",
        )


def test_an_approved_export_is_accepted() -> None:
    """The control group: the same shape with the decision the gate gave."""

    state = _state(
        approval_id="apr_1",
        approval_decision="approved",
        export_ref="art_report_1",
    )

    assert state.export_ref == "art_report_1"


def test_a_task_with_no_gate_may_export_without_an_approval() -> None:
    """A deployment that runs without the gate has no approval to point at.

    Demanding one would make its exports unrepresentable -- the graph routes
    review straight to export, and the state it produces would then fail to
    validate. The invariant is conditioned on the Task's own frozen setting,
    not dropped.
    """

    state = _state(export_requires_approval=False, export_ref="art_report_1")

    assert state.export_ref == "art_report_1"
    assert state.approval_id is None


def test_the_gate_still_binds_the_tasks_that_have_one() -> None:
    """The control that keeps the test above from being a hole.

    `export_requires_approval` defaults True, so an ordinary Task is unchanged:
    an export it cannot name an approval for is still unrepresentable.
    """

    assert _state().export_requires_approval is True
    with pytest.raises(ValidationError, match="requires an approved"):
        _state(export_requires_approval=True, export_ref="art_report_1")
