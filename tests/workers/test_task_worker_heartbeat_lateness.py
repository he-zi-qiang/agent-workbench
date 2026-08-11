"""A heartbeat that overslept may not renew the lease (ADR-041).

Both tests drive the *real* mechanism rather than a simulated clock: the
stalling case blocks the event loop with a synchronous ``time.sleep``, which is
what an unoffloaded embedding batch or PDF parse does to this process. Nothing
here patches ``loop.time`` -- a test that moved the clock by hand could pass
against an implementation that measured the wrong thing.

The two tests are a matched pair and are only meaningful together. They share
one Worker configuration and one code path; the single difference is whether
the loop was blocked. One renews, one refuses. A check that merely refused
would pass the first test alone, which is exactly the shape of assertion this
repository has been burned by before -- "looks like it is guarding, guards the
wrong thing".
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.tasks import TaskState
from agent_workbench.ports.task_registry import ExecutionLease, TaskClaim, TaskRun
from agent_workbench.workers.task import TaskWorker

#: One second of heartbeat and one of tolerated lateness, so the refusal
#: threshold is two seconds. Both margins below are a full second wide, which
#: is what keeps these tests from measuring how loaded the machine is.
HEARTBEAT_SECONDS = 1
ABORT_LAG_SECONDS = 1
#: Longer than HEARTBEAT + ABORT_LAG. A real freeze, not a slow await.
STALL_SECONDS = 3.0
#: Shorter than HEARTBEAT + ABORT_LAG, and long enough that at least one
#: heartbeat fires before the graph finishes.
HEALTHY_RUN_SECONDS = 1.5


def _task() -> TaskRun:
    now = datetime.now(UTC)
    return TaskRun.model_validate(
        {
            "task_id": "task_late_1",
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
            "lease_epoch": 1,
            "lease_until": now,
            "heartbeat_at": now,
            "attempt_count": 1,
            "available_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )


class _Registry:
    """Counts renewals and refuses to be written to in any other way."""

    def __init__(self) -> None:
        self.task = _task()
        self.lease = ExecutionLease(
            task_id=self.task.task_id, worker_id="worker_1", epoch=1
        )
        self.heartbeats = 0
        self.lifecycle_writes = 0
        self.release_calls = 0

    async def reclaim_expired(self, **_: Any) -> tuple[TaskRun, ...]:
        return ()

    async def claim_next(self, *_: Any, **__: Any) -> TaskClaim:
        return TaskClaim(task=self.task, lease=self.lease)

    async def get(self, _: str) -> TaskRun:
        return self.task

    async def heartbeat(self, *_: Any, **__: Any) -> TaskRun:
        self.heartbeats += 1
        return self.task

    async def release_for_retry(self, *_: Any, **__: Any) -> TaskRun:
        # A Worker that just admitted it cannot be trusted about time must not
        # hand the lease back either: letting it expire is what lets a healthy
        # Worker reclaim under a new epoch.
        self.release_calls += 1
        return self.task

    async def mark_succeeded(self, *_: Any, **__: Any) -> TaskRun:
        self.lifecycle_writes += 1
        return self.task

    mark_failed = mark_succeeded
    park_for_migration = mark_succeeded
    await_approval = mark_succeeded


class _StallingWorkflow:
    """Freezes the event loop, then never finishes on its own."""

    def __init__(self, stall_seconds: float) -> None:
        self.stall_seconds = stall_seconds
        self.cancelled = False

    async def inspect(self, _: str) -> None:
        return None

    async def run(self, _: TaskState, **__: Any) -> None:
        # Synchronous on purpose. ``asyncio.sleep`` here would prove nothing:
        # the heartbeat would wake on time and the lease would be renewed by a
        # process that really was alive.
        time.sleep(self.stall_seconds)
        try:
            await asyncio.Future[None]()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def resume(self, **__: Any) -> None:
        raise AssertionError("a new task starts rather than resumes")


class _HealthyWorkflow:
    """Takes just as long, without ever holding the loop."""

    def __init__(self, run_seconds: float) -> None:
        self.run_seconds = run_seconds
        self.finished = False

    async def inspect(self, _: str) -> None:
        return None

    async def run(self, _: TaskState, **__: Any) -> None:
        await asyncio.sleep(self.run_seconds)
        self.finished = True

    async def resume(self, **__: Any) -> None:
        raise AssertionError("a new task starts rather than resumes")


async def _load_state(_: TaskRun) -> TaskState:
    return TaskState(task_id="task_late_1", objective="Heartbeat lateness test")


def _worker(registry: _Registry, workflow: object) -> TaskWorker:
    return TaskWorker(
        registry=registry,  # type: ignore[arg-type]
        workflow=workflow,  # type: ignore[arg-type]
        load_state=_load_state,
        buildable_versions=("v1",),
        worker_id="worker_1",
        heartbeat_seconds=HEARTBEAT_SECONDS,
        abort_lag_seconds=ABORT_LAG_SECONDS,
    )


def test_a_heartbeat_that_overslept_refuses_to_renew_the_lease() -> None:
    """The stalling half of the pair: a frozen loop must not claim liveness."""

    async def scenario() -> None:
        registry = _Registry()
        workflow = _StallingWorkflow(STALL_SECONDS)

        outcome = await asyncio.wait_for(
            _worker(registry, workflow).run_once(), timeout=STALL_SECONDS + 10
        )

        assert outcome is not None
        # The point of the change. Before it, the heartbeat woke late and
        # pushed ``lease_until`` out by another full period.
        assert registry.heartbeats == 0
        # Refusing is a stop, not a settlement: a Worker that cannot prove it
        # owns the claim writes nothing under it.
        assert registry.lifecycle_writes == 0
        assert registry.release_calls == 0
        assert workflow.cancelled is True

    asyncio.run(scenario())


def test_a_heartbeat_that_woke_on_time_still_renews_the_lease() -> None:
    """The control. Without it the refusal above proves only that it refuses.

    Same Worker, same thresholds, same code path -- the graph simply never
    holds the loop. If this went red alongside a green refusal test, the check
    would be rejecting every heartbeat rather than the late ones.
    """

    async def scenario() -> None:
        registry = _Registry()
        workflow = _HealthyWorkflow(HEALTHY_RUN_SECONDS)

        outcome = await asyncio.wait_for(
            _worker(registry, workflow).run_once(), timeout=HEALTHY_RUN_SECONDS + 10
        )

        assert outcome is not None
        assert workflow.finished is True
        assert registry.heartbeats >= 1

    asyncio.run(scenario())
