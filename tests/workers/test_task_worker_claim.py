"""What a graph invocation is told about the claim it is running under.

The Worker is the only thing that holds an ``ExecutionLease``: it receives one
from the Registry when it claims a Task, and every fenced write is supposed to
be checked against *that* value. A node deep inside the graph has no way to
reach it -- the graph's state is checkpointed and a lease must never be -- so
the Worker publishes it for the duration of one invocation.

Two properties, and the second matters as much as the first: the claim is
visible where the graph runs, and it is gone afterwards. A lease that outlived
its invocation would be a Worker's authority lying around for whatever ran
next, which is the failure this mechanism exists to prevent, arriving by
another route.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.tasks import TaskState
from agent_workbench.ports.task_registry import ExecutionLease, TaskClaim, TaskRun
from agent_workbench.ports.task_workflow import CheckpointPosition
from agent_workbench.workers.task import TaskWorker
from agent_workbench.workflows.execution_scope import TaskExecutionScope


def _task() -> TaskRun:
    now = datetime.now(UTC)
    return TaskRun.model_validate(
        {
            "task_id": "task_claim_1",
            "tenant_id": "tenant_1",
            "owner_id": "user_1",
            "thread_id": "thread_1",
            "graph_version": "v1",
            "input_ref": "input_1",
            "input_fingerprint": "a" * 64,
            "submission_dedup_key": "dedup_1",
            "run_semantics_snapshot": {},
            "run_semantics_revision": "semantics_1",
            "submitted_policy_revision": "policy_1",
            "submitted_policy_fingerprint": "b" * 16,
            "submitted_authorization_envelope": AuthorizationEnvelope(),
            "status": "running",
            "lease_owner": "worker_1",
            "lease_epoch": 4,
            "lease_until": now,
            "heartbeat_at": now,
            "attempt_count": 1,
            "available_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )


class _Registry:
    def __init__(self) -> None:
        self.task = _task()
        self.lease = ExecutionLease(
            task_id=self.task.task_id, worker_id="worker_1", epoch=4
        )
        self.settled: list[str] = []

    async def reclaim_expired(self, **_: Any) -> tuple[TaskRun, ...]:
        return ()

    async def claim_next(self, *_: Any, **__: Any) -> TaskClaim:
        return TaskClaim(task=self.task, lease=self.lease)

    async def get(self, _: str) -> TaskRun:
        return self.task

    async def heartbeat(self, *_: Any, **__: Any) -> TaskRun:
        return self.task

    async def mark_succeeded(self, *_: Any, **__: Any) -> TaskRun:
        self.settled.append("succeeded")
        self.task = self.task.model_copy(update={"status": "succeeded"})
        return self.task


class _ObservingWorkflow:
    """Answers what the graph could see while it was running."""

    def __init__(self, scope: TaskExecutionScope) -> None:
        self._scope = scope
        self.finished = False
        self.observed: ExecutionLease | None = None

    async def inspect(self, _: str) -> CheckpointPosition | None:
        # Asked before the invocation and again after it, and outside it both
        # times: whatever this sees is what a *non*-node caller sees.
        self.observed_during_inspect = self._scope.current()
        return CheckpointPosition(graph_version="v1") if self.finished else None

    async def run(self, _: TaskState, **__: Any) -> None:
        # Deliberately after an await: a node runs several suspensions deep
        # inside the framework, not synchronously under the Worker's frame.
        await asyncio.sleep(0)
        self.observed = self._scope.current()
        self.finished = True

    async def resume(self, **__: Any) -> None:
        raise AssertionError("a new task starts rather than resumes")


async def _load_state(_: TaskRun) -> TaskState:
    return TaskState(task_id="task_claim_1", objective="Publish the claim.")


def test_the_worker_publishes_the_claim_it_was_given_and_takes_it_back() -> None:
    """The lease the Registry handed this Worker, and nothing else.

    Epoch 4 rather than 1 on purpose: a bug that published a constant, an index
    or a fresh read would still look right against epoch 1.
    """

    scope = TaskExecutionScope()
    registry = _Registry()
    workflow = _ObservingWorkflow(scope)
    worker = TaskWorker(
        registry=registry,  # type: ignore[arg-type]
        workflow=workflow,  # type: ignore[arg-type]
        load_state=_load_state,
        buildable_versions=("v1",),
        worker_id="worker_1",
        heartbeat_seconds=3_600,
        scope=scope,
    )

    assert scope.current() is None
    outcome = asyncio.run(worker.run_once())

    assert outcome is not None
    assert registry.settled == ["succeeded"]
    assert workflow.observed == registry.lease
    # Outside the invocation there is no claim -- before it, during the Worker's
    # own Registry reads, and after it returns.
    assert workflow.observed_during_inspect is None
    assert scope.current() is None
