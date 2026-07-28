"""Contract for the LangGraph-backed workflow adapter.

The adapter is checked against the same ``TaskWorkflowPort`` contract the
in-memory fake satisfies, plus the two things only a real graph can show: that
it compiles the declared edges rather than a restatement of them, and that a
checkpoint round trip does not lose state.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_workbench.adapters.langgraph.workflow import (
    GRAPH_BUILDERS,
    GraphState,
    LangGraphTaskWorkflow,
)
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep
from agent_workbench.ports.task_workflow import (
    TaskWorkflowPort,
    WorkflowGraphVersionMismatchError,
    WorkflowThreadAlreadyExistsError,
    WorkflowThreadNotFoundError,
)


def _state(**overrides: object) -> TaskState:
    base: dict[str, object] = {
        "task_id": "task_1",
        "objective": "Compare retrieval strategies.",
        "plan": (
            TaskStep(step_id="step_1", sequence=1, objective="Gather internal notes."),
        ),
    }
    base.update(overrides)
    return TaskState.model_validate(base)


def _passing_review(revision: int = 0) -> ReviewResult:
    return ReviewResult(
        decision="pass",
        reviewed_draft_ref="draft_1",
        revision_number=revision,
        summary="Grounded in the evidence.",
        score=90,
    )


def _handlers() -> dict[str, Any]:
    """Deterministic stand-ins: each node writes only its own channel."""

    async def understand(state: TaskState) -> dict[str, Any]:
        return {"agent_outcome_refs": ("run_understand",)}

    async def internal(state: TaskState) -> dict[str, Any]:
        return {
            "evidence_refs": ("ev_internal",),
            "agent_outcome_refs": ("run_internal",),
        }

    async def external(state: TaskState) -> dict[str, Any]:
        return {
            "evidence_refs": ("ev_external",),
            "agent_outcome_refs": ("run_external",),
        }

    async def synthesize(state: TaskState) -> dict[str, Any]:
        return {"draft_ref": "draft_1", "review_result": None}

    async def critic(state: TaskState) -> dict[str, Any]:
        return {"review_result": _passing_review(state.revision_count).model_dump()}

    return {
        "understand": understand,
        "research_internal": internal,
        "research_external": external,
        "synthesize": synthesize,
        "critic": critic,
    }


def _workflow(**kwargs: Any) -> LangGraphTaskWorkflow:
    return LangGraphTaskWorkflow(handlers=_handlers(), **kwargs)


# --------------------------------------------------------------------------
# Port conformance and workflow identity
# --------------------------------------------------------------------------


def test_the_adapter_satisfies_the_framework_neutral_port() -> None:
    assert isinstance(_workflow(), TaskWorkflowPort)


def test_a_run_reaches_the_terminal_node_through_the_declared_edges() -> None:
    result = asyncio.run(
        _workflow().run(_state(), thread_id="thread_1", graph_version="v1")
    )

    assert result.disposition == "completed"
    assert result.thread_id == "thread_1"
    assert result.graph_version == "v1"
    # Both branches ran and merged: the sorted union is the adapter's channel
    # reducer, so LangGraph's fan-in agrees with the control flow's fan_in.
    assert result.state.evidence_refs == ("ev_external", "ev_internal")
    assert result.state.draft_ref == "draft_1"


def test_a_first_run_refuses_a_thread_that_already_exists() -> None:
    async def scenario() -> None:
        workflow = _workflow()
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        with pytest.raises(WorkflowThreadAlreadyExistsError):
            await workflow.run(_state(), thread_id="thread_1", graph_version="v1")

    asyncio.run(scenario())


def test_resume_rejects_an_unknown_thread() -> None:
    with pytest.raises(WorkflowThreadNotFoundError):
        asyncio.run(_workflow().resume(thread_id="thread_absent", graph_version="v1"))


def test_resume_fails_closed_on_a_graph_version_mismatch() -> None:
    async def scenario() -> None:
        workflow = _workflow()
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")

        with pytest.raises(WorkflowGraphVersionMismatchError) as captured:
            await workflow.resume(thread_id="thread_1", graph_version="v2")
        assert captured.value.checkpoint_graph_version == "v1"
        assert captured.value.requested_graph_version == "v2"

        # The rejected resume must not have migrated or damaged the checkpoint.
        recovered = await workflow.resume(thread_id="thread_1", graph_version="v1")
        assert recovered.disposition == "completed"

    asyncio.run(scenario())


def test_an_unregistered_graph_version_never_falls_back_to_the_newest() -> None:
    with pytest.raises(WorkflowGraphVersionMismatchError):
        asyncio.run(
            _workflow().run(_state(), thread_id="thread_1", graph_version="v99")
        )


def test_resume_does_not_resubmit_the_original_input() -> None:
    calls: list[str] = []

    async def counting_understand(state: TaskState) -> dict[str, Any]:
        calls.append(state.task_id)
        return {"agent_outcome_refs": ("run_understand",)}

    handlers = _handlers() | {"understand": counting_understand}

    async def scenario() -> None:
        workflow = LangGraphTaskWorkflow(handlers=handlers)
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        await workflow.resume(thread_id="thread_1", graph_version="v1")
        await workflow.resume(thread_id="thread_1", graph_version="v1")

    asyncio.run(scenario())

    # A resume that passed the initial state again would re-enter the graph
    # from START and run understand a second time.
    assert calls == ["task_1"]


# --------------------------------------------------------------------------
# The adapter compiles the declaration rather than restating it
# --------------------------------------------------------------------------


def test_every_task_state_field_is_a_graph_channel() -> None:
    # A field added to TaskState without a channel here would be silently
    # dropped on the first checkpoint round trip.
    assert set(GraphState.__annotations__) == set(TaskState.model_fields)


def test_a_checkpoint_round_trip_preserves_the_state() -> None:
    started = _state(max_revisions=1)
    result = asyncio.run(
        _workflow().run(started, thread_id="thread_1", graph_version="v1")
    )

    assert result.state.task_id == started.task_id
    assert result.state.objective == started.objective
    assert result.state.plan == started.plan
    assert result.state.max_revisions == 1


def test_only_the_registered_versions_are_buildable() -> None:
    assert set(GRAPH_BUILDERS) == {"v1"}


# --------------------------------------------------------------------------
# The conditional edges behave as the control flow specifies
# --------------------------------------------------------------------------


def test_an_exhausted_revision_budget_ends_the_graph_without_approving() -> None:
    async def revising_critic(state: TaskState) -> dict[str, Any]:
        return {
            "review_result": ReviewResult(
                decision="revise",
                reviewed_draft_ref="draft_1",
                revision_number=state.revision_count,
                summary="Still thin.",
                issues=("Evidence is thin.",),
                score=30,
            ).model_dump()
        }

    async def approval(state: TaskState) -> dict[str, Any]:
        return {"approval_id": "approval_1"}

    handlers = _handlers() | {"critic": revising_critic, "approval": approval}

    result = asyncio.run(
        LangGraphTaskWorkflow(handlers=handlers).run(
            _state(max_revisions=0),
            thread_id="thread_1",
            graph_version="v1",
        )
    )

    # The critic rejected the draft and there was no budget to revise. The
    # graph must stop, not walk into approval.
    assert result.state.approval_id is None
    assert result.state.review_result is not None
    assert result.state.review_result.decision == "revise"


def test_a_passing_review_reaches_approval_and_export() -> None:
    visited: list[str] = []

    async def approval(state: TaskState) -> dict[str, Any]:
        visited.append("approval")
        return {"approval_id": "approval_1"}

    async def export(state: TaskState) -> dict[str, Any]:
        visited.append("export")
        return {}

    handlers = _handlers() | {"approval": approval, "export": export}

    result = asyncio.run(
        LangGraphTaskWorkflow(handlers=handlers).run(
            _state(), thread_id="thread_1", graph_version="v1"
        )
    )

    assert visited == ["approval", "export"]
    assert result.state.approval_id == "approval_1"
    assert result.disposition == "completed"
