"""Contract for the framework-neutral Task workflow boundary."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from agent_workbench.domain.tasks import TaskState
from agent_workbench.ports.task_workflow import (
    CheckpointFence,
    CheckpointPosition,
    TaskWorkflowPort,
    TaskWorkflowResult,
    WorkflowGraphVersionMismatchError,
    WorkflowThreadAlreadyExistsError,
    WorkflowThreadNotFoundError,
)


@dataclass(frozen=True)
class _Checkpoint:
    graph_version: str
    state: TaskState


class _FakeTaskWorkflow:
    """The smallest adapter that demonstrates identity and resume semantics."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, _Checkpoint] = {}
        self.initial_state_submissions = 0

    async def run(
        self,
        state: TaskState,
        *,
        thread_id: str,
        graph_version: str,
        checkpoint_fence: CheckpointFence | None = None,
    ) -> TaskWorkflowResult:
        if thread_id in self._checkpoints:
            raise WorkflowThreadAlreadyExistsError(thread_id)
        self.initial_state_submissions += 1
        self._checkpoints[thread_id] = _Checkpoint(graph_version, state)
        return TaskWorkflowResult(
            thread_id=thread_id,
            graph_version=graph_version,
            disposition="interrupted",
            state=state,
            next_nodes=("understand",),
        )

    async def resume(
        self,
        *,
        thread_id: str,
        graph_version: str,
        checkpoint_fence: CheckpointFence | None = None,
    ) -> TaskWorkflowResult:
        checkpoint = self._checkpoints.get(thread_id)
        if checkpoint is None:
            raise WorkflowThreadNotFoundError(thread_id)
        if checkpoint.graph_version != graph_version:
            raise WorkflowGraphVersionMismatchError(
                thread_id=thread_id,
                checkpoint_graph_version=checkpoint.graph_version,
                requested_graph_version=graph_version,
            )
        return TaskWorkflowResult(
            thread_id=thread_id,
            graph_version=graph_version,
            disposition="completed",
            state=checkpoint.state,
        )

    async def inspect(self, thread_id: str) -> CheckpointPosition | None:
        checkpoint = self._checkpoints.get(thread_id)
        if checkpoint is None:
            return None
        return CheckpointPosition(
            graph_version=checkpoint.graph_version, pending_nodes=("understand",)
        )


def _state() -> TaskState:
    return TaskState(task_id="task_1", objective="Compare retrieval strategies.")


def test_a_structural_fake_satisfies_the_runtime_checkable_port() -> None:
    assert isinstance(_FakeTaskWorkflow(), TaskWorkflowPort)
    assert inspect.iscoroutinefunction(TaskWorkflowPort.run)
    assert inspect.iscoroutinefunction(TaskWorkflowPort.resume)
    # Deciding what to do with a thread must not require running it first.
    assert inspect.iscoroutinefunction(TaskWorkflowPort.inspect)


def test_identity_is_explicit_and_resume_cannot_accept_initial_state() -> None:
    run_parameters = inspect.signature(TaskWorkflowPort.run).parameters
    resume_parameters = inspect.signature(TaskWorkflowPort.resume).parameters

    assert set(run_parameters) == {
        "self",
        "state",
        "thread_id",
        "graph_version",
        "checkpoint_fence",
    }
    # `approval` is a wake-up naming a pending interrupt, not initial state:
    # it carries an approval id and the version seen, and the node re-reads the
    # decision itself. `state` remains absent, which is the property this
    # assertion exists for.
    assert set(resume_parameters) == {
        "self",
        "thread_id",
        "graph_version",
        "checkpoint_fence",
        "approval",
    }
    assert run_parameters["thread_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert run_parameters["graph_version"].kind is inspect.Parameter.KEYWORD_ONLY
    assert run_parameters["checkpoint_fence"].kind is inspect.Parameter.KEYWORD_ONLY
    assert resume_parameters["thread_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert resume_parameters["graph_version"].kind is inspect.Parameter.KEYWORD_ONLY
    assert resume_parameters["checkpoint_fence"].kind is inspect.Parameter.KEYWORD_ONLY
    assert resume_parameters["approval"].kind is inspect.Parameter.KEYWORD_ONLY


def test_run_and_resume_echo_the_explicit_workflow_identity() -> None:
    async def scenario() -> tuple[TaskWorkflowResult, TaskWorkflowResult]:
        workflow = _FakeTaskWorkflow()
        started = await workflow.run(
            _state(),
            thread_id="thread_1",
            graph_version="v1",
        )
        resumed = await workflow.resume(thread_id="thread_1", graph_version="v1")
        return started, resumed

    started, resumed = asyncio.run(scenario())

    assert (started.thread_id, started.graph_version) == ("thread_1", "v1")
    assert (resumed.thread_id, resumed.graph_version) == ("thread_1", "v1")
    assert started.disposition == "interrupted"
    assert resumed.disposition == "completed"


def test_resume_cannot_resubmit_or_duplicate_initial_state() -> None:
    async def scenario() -> int:
        workflow = _FakeTaskWorkflow()
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        await workflow.resume(thread_id="thread_1", graph_version="v1")
        await workflow.resume(thread_id="thread_1", graph_version="v1")
        return workflow.initial_state_submissions

    assert asyncio.run(scenario()) == 1


def test_first_run_rejects_an_existing_thread() -> None:
    async def scenario() -> None:
        workflow = _FakeTaskWorkflow()
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        with pytest.raises(WorkflowThreadAlreadyExistsError):
            await workflow.run(
                _state(),
                thread_id="thread_1",
                graph_version="v1",
            )

    asyncio.run(scenario())


def test_resume_fails_closed_on_a_graph_version_mismatch() -> None:
    async def scenario() -> None:
        workflow = _FakeTaskWorkflow()
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        with pytest.raises(WorkflowGraphVersionMismatchError) as captured:
            await workflow.resume(thread_id="thread_1", graph_version="v2")

        assert captured.value.checkpoint_graph_version == "v1"
        assert captured.value.requested_graph_version == "v2"

        # A rejected resume must not migrate or corrupt the checkpoint.
        recovered = await workflow.resume(
            thread_id="thread_1",
            graph_version="v1",
        )
        assert recovered.disposition == "completed"

    asyncio.run(scenario())


def test_resume_rejects_an_unknown_thread() -> None:
    async def scenario() -> None:
        workflow = _FakeTaskWorkflow()
        with pytest.raises(WorkflowThreadNotFoundError):
            await workflow.resume(
                thread_id="thread_absent",
                graph_version="v1",
            )

    asyncio.run(scenario())


def test_structured_results_enforce_disposition_invariants() -> None:
    with pytest.raises(ValidationError, match="cannot have next_nodes"):
        TaskWorkflowResult(
            thread_id="thread_1",
            graph_version="v1",
            disposition="completed",
            state=_state(),
            next_nodes=("understand",),
        )

    with pytest.raises(ValidationError, match="must identify a next node"):
        TaskWorkflowResult(
            thread_id="thread_1",
            graph_version="v1",
            disposition="interrupted",
            state=_state(),
        )

    with pytest.raises(ValidationError, match="must identify a failure reason"):
        TaskWorkflowResult(
            thread_id="thread_1",
            graph_version="v1",
            disposition="failed",
            state=_state(),
        )

    with pytest.raises(
        ValidationError, match="terminal workflow cannot have next_nodes"
    ):
        TaskWorkflowResult(
            thread_id="thread_1",
            graph_version="v1",
            disposition="failed",
            state=_state(),
            failure_reason="the graph failed",
            next_nodes=("critic",),
        )


def test_checkpoint_fence_requires_a_complete_guard_identity() -> None:
    with pytest.raises(ValidationError, match="pid and lock key"):
        CheckpointFence(
            task_id="task_1",
            worker_id="worker_1",
            epoch=1,
            guard_backend_pid=123,
        )

    fence = CheckpointFence(
        task_id="task_1",
        worker_id="worker_1",
        epoch=1,
        guard_backend_pid=123,
        guard_lock_key=-(2**63),
    )
    assert fence.guard_backend_pid == 123
    assert fence.guard_lock_key == -(2**63)


def test_the_port_surface_contains_no_framework_types() -> None:
    annotations = " ".join(
        repr(annotation)
        for member in (
            TaskWorkflowPort.run,
            TaskWorkflowPort.resume,
            TaskWorkflowPort.inspect,
        )
        for annotation in inspect.get_annotations(member).values()
    ).lower()

    assert "langgraph" not in annotations
    assert "langchain" not in annotations
