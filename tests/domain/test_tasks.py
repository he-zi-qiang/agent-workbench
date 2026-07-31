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


def test_approval_only_follows_a_passing_review() -> None:
    with pytest.raises(ValidationError, match="requires a passing"):
        _state(
            approval_id="apr_1",
            approval_decision="approved",
            review_result=_review(
                decision="revise",
                issues=("Citations are incomplete.",),
            ),
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
        "plan",
        "evidence_refs",
        "draft_ref",
        "review_result",
        "approval_id",
        "approval_decision",
        "agent_outcome_refs",
        "budget_usage",
        "revision_count",
        "max_revisions",
    }
