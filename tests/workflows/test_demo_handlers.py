"""The deterministic offline handlers used by Task Worker smoke tests."""

from __future__ import annotations

import asyncio

from agent_workbench.adapters.langgraph import LangGraphTaskWorkflow
from agent_workbench.domain.tasks import TaskState
from agent_workbench.workflows.demo_handlers import build_demo_v1_handlers


def _state(*, task_id: str = "task_demo") -> TaskState:
    return TaskState(task_id=task_id, objective="Compare two retrieval strategies.")


def test_demo_handlers_complete_the_real_v1_graph_offline() -> None:
    result = asyncio.run(
        LangGraphTaskWorkflow(handlers=build_demo_v1_handlers()).run(
            _state(),
            thread_id="thr_demo",
            graph_version="v1",
        )
    )

    assert result.disposition == "completed"
    assert result.state.plan[0].sequence == 1
    assert result.state.evidence_refs == tuple(sorted(result.state.evidence_refs))
    assert len(result.state.evidence_refs) == 2
    assert result.state.draft_ref is not None
    assert result.state.review_result is not None
    assert result.state.review_result.decision == "pass"
    assert result.state.review_result.reviewed_draft_ref == result.state.draft_ref
    assert result.state.review_result.revision_number == result.state.revision_count
    assert result.state.approval_id is not None


def test_demo_handlers_are_stable_for_the_same_task() -> None:
    async def run(thread_id: str) -> TaskState:
        workflow = LangGraphTaskWorkflow(handlers=build_demo_v1_handlers())
        return (
            await workflow.run(
                _state(),
                thread_id=thread_id,
                graph_version="v1",
            )
        ).state

    async def scenario() -> tuple[TaskState, TaskState]:
        return await asyncio.gather(run("thr_demo_1"), run("thr_demo_2"))

    first, second = asyncio.run(scenario())

    assert first.plan == second.plan
    assert first.evidence_refs == second.evidence_refs
    assert first.draft_ref == second.draft_ref
    assert first.agent_outcome_refs == second.agent_outcome_refs


def test_demo_handler_references_are_only_synthetic_identifiers() -> None:
    state = asyncio.run(
        LangGraphTaskWorkflow(handlers=build_demo_v1_handlers()).run(
            _state(),
            thread_id="thr_demo",
            graph_version="v1",
        )
    ).state

    assert all(reference.startswith("art_demo_") for reference in state.evidence_refs)
    assert state.draft_ref is not None and state.draft_ref.startswith("art_demo_")
    assert all(
        reference.startswith("run_demo_") for reference in state.agent_outcome_refs
    )
