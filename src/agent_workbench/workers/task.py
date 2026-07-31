"""The Task runner.

It owns no decision. Picking a Task up is the Registry's conditional update,
deciding what its two facts mean is ``application.task_recovery.reconcile``, and
running the graph is the workflow adapter's. What is left here -- and it is the
only thing here -- is the loop that connects them and the rule for when to stop.

Running is expressed as re-deciding rather than as a second state machine. A
Worker that started a graph asks the same question again afterwards: the
position moved, so the answer moves with it, from ``start`` to
``settle_succeeded`` or to ``wait_for_approval``. Writing "and then mark it
succeeded" after the invocation would be a second, differently-worded copy of
the reconciliation, and the copy is the one that would still be running after
somebody changes the original.

The loop is bounded. A graph that comes back neither finished nor waiting is
one this Worker cannot settle, and looping on it forever is worse than
recording that plainly, so the budget runs out into ``failed``.

The Registry claim is a time-bounded execution lease.  A separate heartbeat
coroutine keeps it alive while the graph runs, and every lifecycle write uses
its owner and epoch.  This protects Task rows across Workers; E2 adds the
corresponding fence at the LangGraph checkpointer boundary.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Final

from agent_workbench.application.task_recovery import Reconciliation, reconcile
from agent_workbench.domain.task_registry import ApprovalDecision
from agent_workbench.domain.tasks import TaskState
from agent_workbench.ports.approvals import ApprovalStore
from agent_workbench.ports.execution_guard import (
    ExecutionGuard,
    GuardFactory,
    GuardUnavailableError,
)
from agent_workbench.ports.fault_injector import FailpointName, FaultInjector
from agent_workbench.ports.task_registry import (
    ExecutionLease,
    StaleExecutionError,
    TaskRegistry,
    TaskRun,
    TaskTransitionRejectedError,
)
from agent_workbench.ports.task_workflow import (
    ApprovalResume,
    CheckpointFence,
    CheckpointPosition,
    GraphVersion,
    TaskWorkflowPort,
)

logger = logging.getLogger(__name__)

#: How many times one claimed Task may be decided about. Three covers the
#: longest legitimate chain -- decide, run, settle -- and turns anything longer
#: into a recorded failure rather than a Worker that never returns.
DEFAULT_MAX_DECISIONS: Final[int] = 3

LoadState = Callable[[TaskRun], Awaitable[TaskState]]


class _GuardLostError(RuntimeError):
    """Stop this Worker without writing after its execution guard was lost."""


@dataclass(frozen=True, slots=True)
class _DecidedApproval:
    """An approval a human has answered, and the two things that answer decides.

    A pending approval cannot be one of these, and that is the point: the type
    is what stops "the ledger has a row" from being confused with "somebody
    said yes or no".
    """

    decision: ApprovalDecision
    #: What the graph is woken with. It names the approval and the version seen
    #: and carries no verdict; the node re-reads the decision itself.
    resume: ApprovalResume


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """What one pass over one Task did, for the caller and for the log."""

    task: TaskRun
    decisions: tuple[Reconciliation, ...]

    @property
    def final_status(self) -> str:
        return self.task.status


@dataclass(frozen=True, slots=True)
class TaskWorker:
    """Take the next queued Task as far as it can go, once."""

    registry: TaskRegistry
    workflow: TaskWorkflowPort
    # How a Task's submitted input becomes the graph's initial state. A
    # callable rather than a port: what an ``input_ref`` points at is settled
    # by the submission transaction (WP07-02), and inventing a store here would
    # be deciding that ahead of the change that has to.
    load_state: LoadState
    buildable_versions: Collection[GraphVersion]
    worker_id: str = "worker_local"
    lease_seconds: int = 90
    heartbeat_seconds: int = 20
    max_attempts: int = 5
    retry_base_seconds: int = 2
    retry_max_seconds: int = 60
    max_decisions: int = DEFAULT_MAX_DECISIONS
    # ``None`` keeps the worker usable for narrow unit tests and migrations;
    # the production composition always supplies a session-pinned guard.
    guards: GuardFactory | None = None
    # The approvals ledger, for the one question this Worker cannot answer from
    # the checkpoint: has a human decided yet. Optional for the same reason the
    # guard is, and with a louder consequence -- a Worker without one parks every
    # interrupted Task instead of resuming it, which is the safe direction but a
    # standstill, so it says so in the log rather than only here.
    approvals: ApprovalStore | None = None
    # Test-only. ``None`` is the production no-op binding, so no controller
    # or test package crosses the normal composition boundary.
    fault_injector: FaultInjector | None = None

    async def run_once(self) -> TaskOutcome | None:
        """Claim one Task and drive it, or return ``None`` if none is queued."""

        await self.registry.reclaim_expired(
            limit=10,
            max_attempts=self.max_attempts,
            retry_base_seconds=self.retry_base_seconds,
            retry_max_seconds=self.retry_max_seconds,
        )
        claimed = await self.registry.claim_next(
            self.worker_id, lease_seconds=self.lease_seconds
        )
        if claimed is None:
            return None

        task = claimed.task
        lease = claimed.lease
        await self._hit_failpoint("after_claim_commit_before_advisory_lock")
        guard: ExecutionGuard | None = None
        if self.guards is not None:
            try:
                guard = await self.guards.acquire(
                    task_id=lease.task_id,
                    worker_id=lease.worker_id,
                    epoch=lease.epoch,
                )
            except GuardUnavailableError:
                return await self._guard_unavailable_outcome(task, lease)

        decisions: list[Reconciliation] = []
        try:
            for _ in range(self.max_decisions):
                await self._require_healthy_guard(guard)
                # Re-read rather than trust the claim: a cancellation that landed
                # while the graph was running is exactly the fact this has to see,
                # and it is the reconciliation's first branch.
                current = await self.registry.get(task.task_id)
                if current is None:  # pragma: no cover - deletion is not a thing here
                    raise RuntimeError(f"task {task.task_id} vanished mid-run")
                task = current

                position = await self.workflow.inspect(task.thread_id)
                # Asked before deciding, not after: whether a graph stopped at an
                # approval is the checkpoint's fact, and whether anybody answered
                # is the ledger's. The reconciliation is a function of both, so
                # both have to be in hand before it runs.
                approval = await self._decided_approval(position)
                decision = reconcile(
                    status=task.status,
                    graph_version=task.graph_version,
                    position=position,
                    buildable_versions=self.buildable_versions,
                    approval_decision=(None if approval is None else approval.decision),
                )
                decisions.append(decision)

                if not decision.keeps_executing:
                    await self._require_healthy_guard(guard)
                    task = await self._settle(task, lease, decision)
                    return TaskOutcome(task=task, decisions=tuple(decisions))

                try:
                    failure = await self._execute(
                        task, lease, decision, guard, approval
                    )
                except StaleExecutionError:
                    # Reclaim or cancellation won while this graph was running.
                    # It is not this Worker's failure to write.
                    current = await self.registry.get(task.task_id)
                    return TaskOutcome(task=current or task, decisions=tuple(decisions))
                if failure is not None:
                    task = await self._fail(task, lease, failure)
                    return TaskOutcome(task=task, decisions=tuple(decisions))
                await self._hit_failpoint("after_graph_complete_before_registry_commit")

            task = await self._fail(
                task,
                lease,
                f"the graph did not settle within {self.max_decisions} decisions",
            )
            return TaskOutcome(task=task, decisions=tuple(decisions))
        except _GuardLostError:
            # The advisory session is no longer ours. Do not write another
            # checkpoint, heartbeat, or lifecycle transition under this lease.
            current = await self.registry.get(task.task_id)
            return TaskOutcome(task=current or task, decisions=tuple(decisions))
        finally:
            if guard is not None:
                await guard.release()

    async def _decided_approval(
        self, position: CheckpointPosition | None
    ) -> _DecidedApproval | None:
        """The answer to the approval this thread is stopped on, if there is one.

        ``None`` covers four different situations on purpose: the graph is not
        at an approval, the approval has vanished, nobody has decided yet, and
        this Worker has no ledger to ask. All four mean the same thing to the
        reconciliation -- do not resume -- and distinguishing them there would
        be inventing branches for differences no execution decision depends on.
        """

        if position is None or position.awaiting_approval_id is None:
            return None
        if self.approvals is None:
            logger.warning(
                "no approvals ledger is wired, so the task waiting on approval "
                "%s cannot be resumed by this worker",
                position.awaiting_approval_id,
            )
            return None
        record = await self.approvals.get(position.awaiting_approval_id)
        if record is None:
            return None
        status = record.status
        if status == "pending":
            return None
        return _DecidedApproval(
            decision=status,
            resume=ApprovalResume(
                approval_id=record.approval_id,
                decision_version=record.decision_version,
            ),
        )

    async def _execute(
        self,
        task: TaskRun,
        lease: ExecutionLease,
        decision: Reconciliation,
        guard: ExecutionGuard | None,
        approval: _DecidedApproval | None = None,
    ) -> str | None:
        """Run or resume the graph. Returns a reason when it failed."""

        execution = asyncio.create_task(
            self._invoke_graph(task, lease, decision, guard, approval),
            name=f"task-graph:{task.task_id}",
        )
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(lease), name=f"task-heartbeat:{task.task_id}"
        )
        guard_lost = (
            asyncio.create_task(guard.lost.wait(), name=f"task-guard:{task.task_id}")
            if guard is not None
            else None
        )
        try:
            wait_for: set[asyncio.Task[object]] = {execution, heartbeat}
            if guard_lost is not None:
                wait_for.add(guard_lost)
            done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)
            if guard_lost is not None and guard_lost in done:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                raise _GuardLostError(task.task_id)
            if heartbeat in done:
                # A heartbeat only finishes when it lost ownership or failed.
                heartbeat.result()
                raise AssertionError("heartbeat stopped without an error")
            await execution
            await self._require_healthy_guard(guard)
        except _GuardLostError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
        except StaleExecutionError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
        except Exception as error:
            # The message, not the exception: a provider's exception text
            # carries request bodies and prompt fragments, and this string
            # reaches events and API responses. The type is what is recorded.
            logger.warning(
                "task %s failed during %s",
                task.task_id,
                decision.action,
                exc_info=True,
            )
            return f"the graph raised {type(error).__name__} during {decision.action}"
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if guard_lost is not None:
                guard_lost.cancel()
                await asyncio.gather(guard_lost, return_exceptions=True)
        return None

    async def _require_healthy_guard(self, guard: ExecutionGuard | None) -> None:
        """Fence every post-claim decision behind the pinned guard session."""

        if guard is not None and (guard.lost.is_set() or not await guard.healthcheck()):
            raise _GuardLostError(guard.task_id)

    async def _hit_failpoint(self, name: FailpointName) -> None:
        """Visit a test-only reliability window without importing test adapters."""

        if self.fault_injector is not None:
            await self.fault_injector.hit(name)

    async def _guard_unavailable_outcome(
        self, task: TaskRun, lease: ExecutionLease
    ) -> TaskOutcome:
        """Yield a claimed Task when exclusivity could not be obtained.

        This is deliberately a conditional Registry transition: a concurrent
        cancellation or lease reclaim must win over an old Worker trying to
        make the Task runnable again. The retry delay also avoids hot-spinning
        while another session still owns the advisory lock.
        """

        try:
            requeued = await self.registry.release_for_retry(
                lease, delay_seconds=self.retry_base_seconds
            )
        except (StaleExecutionError, TaskTransitionRejectedError):
            current = await self.registry.get(task.task_id)
            requeued = current or task
        return TaskOutcome(task=requeued, decisions=())

    async def _invoke_graph(
        self,
        task: TaskRun,
        lease: ExecutionLease,
        decision: Reconciliation,
        guard: ExecutionGuard | None,
        approval: _DecidedApproval | None,
    ) -> None:
        # A production Worker reaches this point only after an acquire. The
        # optional branch keeps narrow non-PostgreSQL workflow tests usable,
        # while ensuring a strict saver never receives a lease-only fence.
        checkpoint_fence = (
            CheckpointFence(
                task_id=lease.task_id,
                worker_id=lease.worker_id,
                epoch=lease.epoch,
                guard_backend_pid=guard.backend_pid,
                guard_lock_key=guard.lock_key,
            )
            if guard is not None
            else None
        )
        if decision.action == "start":
            await self.workflow.run(
                await self.load_state(task),
                thread_id=task.thread_id,
                graph_version=task.graph_version,
                checkpoint_fence=checkpoint_fence,
            )
        else:
            await self.workflow.resume(
                thread_id=task.thread_id,
                graph_version=task.graph_version,
                checkpoint_fence=checkpoint_fence,
                # Only where the decision says an approval is what is being
                # resumed. An ordinary resume that carried one would be handing
                # a wake-up to a graph that is not waiting for one, and the id
                # is gated on the action rather than merely on the record so a
                # future action that also has an approval in hand has to say so.
                approval=(
                    approval.resume
                    if approval is not None
                    and decision.action == "resume_with_approval"
                    else None
                ),
            )

    async def _heartbeat_loop(self, lease: ExecutionLease) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await self.registry.heartbeat(lease, lease_seconds=self.lease_seconds)

    async def _settle(
        self, task: TaskRun, lease: ExecutionLease, decision: Reconciliation
    ) -> TaskRun:
        if decision.action == "propagate_terminal":
            # Already terminal. Writing anything here would be re-recording a
            # fact somebody else established, conditionally, and failing.
            return task
        if decision.action == "settle_succeeded":
            return await self._settle_or_current(
                task, lambda: self.registry.mark_succeeded(lease)
            )
        if decision.action == "settle_failed":
            return await self._settle_or_current(
                task, lambda: self.registry.mark_failed(lease, reason=decision.detail)
            )
        if decision.action == "wait_for_migration":
            return await self._settle_or_current(
                task,
                lambda: self.registry.park_for_migration(lease, reason=decision.detail),
            )
        if decision.action == "wait_for_approval":
            return await self._settle_or_current(
                task, lambda: self.registry.await_approval(lease)
            )
        raise AssertionError(f"no settlement for {decision.action}")

    async def _settle_or_current(
        self, task: TaskRun, operation: Callable[[], Awaitable[TaskRun]]
    ) -> TaskRun:
        try:
            return await operation()
        except (TaskTransitionRejectedError, StaleExecutionError):
            settled = await self.registry.get(task.task_id)
            return settled if settled is not None else task

    async def _fail(self, task: TaskRun, lease: ExecutionLease, reason: str) -> TaskRun:
        try:
            return await self.registry.mark_failed(lease, reason=reason)
        except (TaskTransitionRejectedError, StaleExecutionError):
            # The Task moved underneath this Worker -- cancelled, most likely.
            # Its status is the current fact and this failure is not.
            logger.info("task %s was already settled elsewhere", task.task_id)
            settled = await self.registry.get(task.task_id)
            return settled if settled is not None else task


__all__ = ["DEFAULT_MAX_DECISIONS", "TaskOutcome", "TaskWorker"]
