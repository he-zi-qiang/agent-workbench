"""A retryable failure is released, not settled (ADR-059).

The measured case behind these tests is known-gaps B-06: the same Task failed
three times on ``RemoteProtocolError``/``ConnectError``, every failure carrying
``retryable: true``, and every one settled terminal -- while ``coordination``'s
retry knobs sat unread by anything except lease expiry. What these pin is the
boundary of the fix as much as the fix: only an *exception* whose run
classified itself retryable is released, a graph that *decided* to fail is
settled exactly as before, and the release stops at ``max_attempts``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.domain.tasks import TaskState
from agent_workbench.ports.task_registry import ExecutionLease, TaskClaim, TaskRun
from agent_workbench.ports.task_workflow import CheckpointPosition
from agent_workbench.workers.task import TaskWorker
from agent_workbench.workflows.agent_nodes import AgentNodeFailedError

STATE = TaskState(task_id="task_retry_1", objective="Survive a provider blip.")


def _task(attempt_count: int) -> TaskRun:
    now = datetime.now(UTC)
    return TaskRun.model_validate(
        {
            "task_id": "task_retry_1",
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
            "attempt_count": attempt_count,
            "available_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )


class _Registry:
    def __init__(self, attempt_count: int = 1) -> None:
        self.task = _task(attempt_count)
        self.lease = ExecutionLease(
            task_id=self.task.task_id, worker_id="worker_1", epoch=4
        )
        self.failed_reasons: list[str] = []
        self.released_delays: list[int] = []

    async def reclaim_expired(self, **_: Any) -> tuple[TaskRun, ...]:
        return ()

    async def claim_next(self, *_: Any, **__: Any) -> TaskClaim:
        return TaskClaim(task=self.task, lease=self.lease)

    async def get(self, _: str) -> TaskRun:
        return self.task

    async def heartbeat(self, *_: Any, **__: Any) -> TaskRun:
        return self.task

    async def release_for_retry(
        self, _: ExecutionLease, *, delay_seconds: int
    ) -> TaskRun:
        self.released_delays.append(delay_seconds)
        self.task = self.task.model_copy(
            update={"status": "queued", "lease_owner": None}
        )
        return self.task

    async def mark_failed(self, _: ExecutionLease, *, reason: str) -> TaskRun:
        self.failed_reasons.append(reason)
        self.task = self.task.model_copy(update={"status": "failed"})
        return self.task

    async def mark_succeeded(self, *_: Any, **__: Any) -> TaskRun:
        raise AssertionError("nothing here succeeds")


class _FailingWorkflow:
    """A graph whose invocation raises, the way a provider blip arrives."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def inspect(self, _: str) -> CheckpointPosition | None:
        return None

    async def run(self, *_: Any, **__: Any) -> None:
        raise self.error

    async def resume(self, **__: Any) -> None:
        raise AssertionError("a new task starts rather than resumes")


class _DecidedFailureWorkflow:
    """A graph that *decided* to fail: the checkpoint says so, no exception."""

    async def inspect(self, _: str) -> CheckpointPosition | None:
        return CheckpointPosition(
            graph_version="v1",
            failure_reason="a human rejected the approval required before export",
        )

    async def run(self, *_: Any, **__: Any) -> None:
        raise AssertionError("a decided failure settles without running")

    async def resume(self, **__: Any) -> None:
        raise AssertionError("a decided failure settles without resuming")


async def _load_state(_: TaskRun) -> TaskState:
    return STATE


def _node_failure(*, retryable: bool) -> AgentNodeFailedError:
    return AgentNodeFailedError(
        node="understand",
        outcome=AgentOutcome(
            agent_run_id="run_1",
            status="failed",
            stop_reason="error",
            error=ErrorInfo(
                code="provider_error",
                message="the request to the provider failed",
                retryable=retryable,
            ),
        ),
        state=STATE,
    )


def _worker(registry: _Registry, workflow: Any) -> TaskWorker:
    return TaskWorker(
        registry=registry,  # type: ignore[arg-type]
        workflow=workflow,
        load_state=_load_state,
        buildable_versions=("v1",),
        worker_id="worker_1",
        heartbeat_seconds=3_600,
        max_attempts=3,
        retry_base_seconds=2,
        retry_max_seconds=60,
    )


def test_a_retryable_failure_is_released_with_backoff_not_settled() -> None:
    registry = _Registry(attempt_count=2)
    outcome = asyncio.run(
        _worker(registry, _FailingWorkflow(_node_failure(retryable=True))).run_once()
    )

    assert outcome is not None
    # reclaim's own formula: base * 2**(attempts-1), attempt 2 -> 4 seconds.
    assert registry.released_delays == [4]
    assert registry.failed_reasons == []
    assert outcome.task.status == "queued"


def test_a_non_retryable_failure_settles_exactly_as_before() -> None:
    registry = _Registry(attempt_count=1)
    outcome = asyncio.run(
        _worker(registry, _FailingWorkflow(_node_failure(retryable=False))).run_once()
    )

    assert outcome is not None
    assert registry.released_delays == []
    assert len(registry.failed_reasons) == 1
    assert "provider_error" in registry.failed_reasons[0]
    # A failure nobody would retry does not advertise an attempt count.
    assert "gave up" not in registry.failed_reasons[0]


def test_an_exhausted_retry_budget_settles_and_says_how_many_attempts() -> None:
    registry = _Registry(attempt_count=3)
    outcome = asyncio.run(
        _worker(registry, _FailingWorkflow(_node_failure(retryable=True))).run_once()
    )

    assert outcome is not None
    assert registry.released_delays == []
    assert len(registry.failed_reasons) == 1
    assert "gave up after attempt 3 of 3" in registry.failed_reasons[0]


def test_an_exception_the_run_never_classified_is_not_retried() -> None:
    # Conservative on purpose: a deterministic failure retried max_attempts
    # times is the same answer at three times the price.
    registry = _Registry(attempt_count=1)
    outcome = asyncio.run(
        _worker(registry, _FailingWorkflow(RuntimeError("the graph broke"))).run_once()
    )

    assert outcome is not None
    assert registry.released_delays == []
    assert len(registry.failed_reasons) == 1


def test_a_failure_the_graph_decided_is_settled_never_retried() -> None:
    """The boundary of ADR-059: retry intercepts exceptions, not decisions.

    A checkpoint carrying ``position.failed`` -- a human's rejection, before
    ADR-060 an exhausted reviewer -- reaches ``settle_failed`` through the
    reconciliation, and must not be offered another paid attempt.
    """

    registry = _Registry(attempt_count=1)
    outcome = asyncio.run(_worker(registry, _DecidedFailureWorkflow()).run_once())

    assert outcome is not None
    assert registry.released_delays == []
    assert registry.failed_reasons == [
        "a human rejected the approval required before export"
    ]
