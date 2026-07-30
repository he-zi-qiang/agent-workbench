"""Worker-level guard ownership tests without a PostgreSQL dependency."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_workbench.adapters.testing import FailpointController, InjectedCrash
from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.tasks import TaskState
from agent_workbench.ports.execution_guard import GuardUnavailableError
from agent_workbench.ports.task_registry import ExecutionLease, TaskClaim, TaskRun
from agent_workbench.ports.task_workflow import CheckpointPosition
from agent_workbench.workers.task import TaskWorker


def _task(*, status: str = "running") -> TaskRun:
    now = datetime.now(UTC)
    return TaskRun.model_validate(
        {
            "task_id": "task_guard_1",
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
            "status": status,
            "lease_owner": "worker_1" if status == "running" else None,
            "lease_epoch": 1,
            "lease_until": now if status == "running" else None,
            "heartbeat_at": now if status == "running" else None,
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
            task_id=self.task.task_id, worker_id="worker_1", epoch=1
        )
        self.release_calls: list[tuple[ExecutionLease, int]] = []
        self.lifecycle_writes = 0

    async def reclaim_expired(self, **_: Any) -> tuple[TaskRun, ...]:
        return ()

    async def claim_next(self, *_: Any, **__: Any) -> TaskClaim:
        return TaskClaim(task=self.task, lease=self.lease)

    async def get(self, _: str) -> TaskRun:
        return self.task

    async def heartbeat(self, *_: Any, **__: Any) -> TaskRun:
        raise AssertionError("heartbeat must be cancelled after guard loss")

    async def release_for_retry(
        self, lease: ExecutionLease, *, delay_seconds: int
    ) -> TaskRun:
        self.release_calls.append((lease, delay_seconds))
        self.task = self.task.model_copy(
            update={
                "status": "queued",
                "lease_owner": None,
                "lease_until": None,
                "heartbeat_at": None,
            }
        )
        return self.task

    async def mark_succeeded(self, *_: Any, **__: Any) -> TaskRun:
        self.lifecycle_writes += 1
        raise AssertionError("guard-lost Worker must not settle")

    mark_failed = mark_succeeded
    park_for_migration = mark_succeeded
    await_approval = mark_succeeded


class _Guard:
    def __init__(self) -> None:
        self.task_id = "task_guard_1"
        self.worker_id = "worker_1"
        self.epoch = 1
        self.backend_pid = 101
        self.lock_key = 202
        self.lost = asyncio.Event()
        self.released = False

    async def healthcheck(self) -> bool:
        return not self.lost.is_set()

    async def release(self) -> None:
        self.released = True


class _GuardFactory:
    def __init__(self, guard: _Guard | None, *, unavailable: bool = False) -> None:
        self.guard = guard
        self.unavailable = unavailable
        self.acquisitions: list[tuple[str, str, int]] = []

    async def acquire(self, *, task_id: str, worker_id: str, epoch: int) -> _Guard:
        self.acquisitions.append((task_id, worker_id, epoch))
        if self.unavailable:
            raise GuardUnavailableError(task_id)
        assert self.guard is not None
        return self.guard


class _BlockingWorkflow:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.invocations = 0

    async def inspect(self, _: str) -> None:
        return None

    async def run(self, _: TaskState, **__: Any) -> None:
        self.invocations += 1
        self.started.set()
        try:
            await asyncio.Future[None]()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def resume(self, **__: Any) -> None:
        raise AssertionError("a new task starts rather than resumes")


class _CompletingWorkflow:
    def __init__(self) -> None:
        self.finished = False
        self.invocations = 0

    async def inspect(self, _: str) -> CheckpointPosition | None:
        return CheckpointPosition(graph_version="v1") if self.finished else None

    async def run(self, _: TaskState, **__: Any) -> None:
        self.invocations += 1
        self.finished = True

    async def resume(self, **__: Any) -> None:
        raise AssertionError("a new task starts rather than resumes")


async def _load_state(_: TaskRun) -> TaskState:
    return TaskState(task_id="task_guard_1", objective="Guard cancellation test")


def _worker(
    registry: _Registry, workflow: _BlockingWorkflow, guards: _GuardFactory
) -> TaskWorker:
    return TaskWorker(
        registry=registry,  # type: ignore[arg-type]
        workflow=workflow,  # type: ignore[arg-type]
        load_state=_load_state,
        buildable_versions=("v1",),
        worker_id="worker_1",
        heartbeat_seconds=3_600,
        guards=guards,
    )


def test_guard_loss_cancels_graph_and_does_not_settle_old_worker() -> None:
    async def scenario() -> None:
        registry = _Registry()
        workflow = _BlockingWorkflow()
        guard = _Guard()
        factory = _GuardFactory(guard)
        running = asyncio.create_task(_worker(registry, workflow, factory).run_once())
        await asyncio.wait_for(workflow.started.wait(), timeout=1)

        guard.lost.set()
        outcome = await asyncio.wait_for(running, timeout=1)

        assert outcome is not None
        assert workflow.cancelled is True
        assert guard.released is True
        assert registry.lifecycle_writes == 0
        assert factory.acquisitions == [("task_guard_1", "worker_1", 1)]

    asyncio.run(scenario())


def test_guard_unavailable_releases_the_claim_without_running_graph() -> None:
    async def scenario() -> None:
        registry = _Registry()
        workflow = _BlockingWorkflow()
        factory = _GuardFactory(None, unavailable=True)

        outcome = await _worker(registry, workflow, factory).run_once()

        assert outcome is not None
        assert outcome.final_status == "queued"
        assert workflow.invocations == 0
        assert registry.release_calls == [(registry.lease, 2)]

    asyncio.run(scenario())


def test_claim_failpoint_stops_before_advisory_guard_acquisition() -> None:
    async def scenario() -> None:
        registry = _Registry()
        workflow = _BlockingWorkflow()
        factory = _GuardFactory(_Guard())
        controller = FailpointController(
            frozenset({"after_claim_commit_before_advisory_lock"})
        )
        controller.arm("after_claim_commit_before_advisory_lock", mode="crash")
        worker = _worker(registry, workflow, factory)
        worker = replace(worker, fault_injector=controller)

        with pytest.raises(InjectedCrash):
            await worker.run_once()
        await controller.wait_until_hit("after_claim_commit_before_advisory_lock")
        assert factory.acquisitions == []
        assert workflow.invocations == 0

    asyncio.run(scenario())


def test_completed_graph_crash_prevents_registry_settlement() -> None:
    async def scenario() -> None:
        registry = _Registry()
        workflow = _CompletingWorkflow()
        guard = _Guard()
        factory = _GuardFactory(guard)
        controller = FailpointController(
            frozenset(
                {
                    "after_claim_commit_before_advisory_lock",
                    "after_graph_complete_before_registry_commit",
                }
            )
        )
        controller.arm("after_graph_complete_before_registry_commit", mode="crash")
        worker = TaskWorker(
            registry=registry,  # type: ignore[arg-type]
            workflow=workflow,  # type: ignore[arg-type]
            load_state=_load_state,
            buildable_versions=("v1",),
            worker_id="worker_1",
            heartbeat_seconds=3_600,
            guards=factory,
            fault_injector=controller,
        )

        with pytest.raises(InjectedCrash):
            await worker.run_once()
        await controller.wait_until_hit("after_graph_complete_before_registry_commit")
        assert workflow.invocations == 1
        assert registry.lifecycle_writes == 0
        assert guard.released is True

    asyncio.run(scenario())
