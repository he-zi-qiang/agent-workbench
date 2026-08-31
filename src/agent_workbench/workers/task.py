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

That same claim is published into the graph invocation rather than left for
the nodes to look up.  A lease is an assertion about *this* process, and a
node that asked the Registry for one would receive whichever Worker holds the
Task at that moment -- so a Worker that lost it mid-graph would go on writing,
under its successor's epoch, past every fence built to stop exactly that.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from typing import Final

from agent_workbench.application.task_recovery import Reconciliation, reconcile
from agent_workbench.application.task_research import EvidenceUnavailableError
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
    AgentInvocationBudgetExhaustedError,
    AgentInvocationCeilingMissingError,
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
from agent_workbench.workflows.agent_nodes import AgentNodeFailedError
from agent_workbench.workflows.execution_scope import TaskExecutionScope
from agent_workbench.workflows.structured_output import StructuredOutputError
from agent_workbench.workflows.task_handlers import TaskNodeRunFailedError

logger = logging.getLogger(__name__)

#: How many times one claimed Task may be decided about. Three covers the
#: longest legitimate chain -- decide, run, settle -- and turns anything longer
#: into a recorded failure rather than a Worker that never returns.
DEFAULT_MAX_DECISIONS: Final[int] = 3

LoadState = Callable[[TaskRun], Awaitable[TaskState]]


class _GuardLostError(RuntimeError):
    """Stop this Worker without writing after its execution guard was lost."""


def _failure_detail(error: BaseException, action: str) -> str:
    """Say why a Task failed, without quoting the provider.

    This string reaches ``status_detail``, the event log and the API, so it may
    not carry a provider's exception text: those hold request bodies and prompt
    fragments. The exception *type* is safe, and used to be all this recorded --
    which left the reader with ``AgentNodeFailedError`` and nowhere to go, while
    the run had already classified the cause one layer down.

    So a node failure reports its run's ``ErrorCode`` instead. The code is a
    closed vocabulary rather than free text, and ``retryable`` says whether
    trying again could plausibly work -- which is the one thing a reader who saw
    a transient provider blip actually needs to know. ``ErrorInfo.message`` is
    still not included: it is operator-facing text with no such guarantee.

    Both node failures are read the same way. ``TaskNodeRunFailedError`` carries
    the same ``AgentOutcome`` and was falling through to the type name anyway,
    so a research node that looped until it exhausted its token budget reached
    the console as ``TaskNodeRunFailedError`` while ``budget_exceeded`` sat one
    layer down, unread.

    ``EvidenceUnavailableError`` is the one exception, and for the same reason
    the rule exists rather than despite it. Its messages are constants written
    in this repository -- "internal research requires a knowledge base", "the
    search returned no evidence" -- and the one that interpolates carries a page
    *count* and short failure codes that ``SourcesUnreadableError`` documents as
    safe for a model's context. None of it comes from a provider. Withholding
    them bought no privacy and cost the reader the whole message: a Task that
    died because a tool ran out of time, because nobody attached a knowledge
    base, and because every page 404'd all read identically.

    **A structured node's failure now says which one it was, and that is the
    whole subject of known-gaps C-05.** For ``plan``/``critic``/``review`` a
    decode failure means the model ran *fine* -- so ``outcome.error`` is
    ``None`` by construction, and every one of them fell through to "did not
    produce usable output". C-05 recorded two competing hypotheses about a real
    2026-08-13 failure and could not choose between them; the reason it could
    not is that both, and every other decode failure in either graph, produce
    that exact sentence. The discriminating text already existed on the
    exception and had no reader.

    Two safety arguments, because the rule above still applies:

    * ``TaskNodeRunFailedError.reason`` is safe, and is what a node without a
      decoder cause reports. All six values are string literals in
      ``task_handlers`` ("critic JSON did not satisfy the review schema" and
      five like it) with no interpolation at all -- a stricter guarantee than
      the ``EvidenceUnavailableError`` precedent needed.
    * The ``__cause__`` is read **one link and no further**. A
      ``StructuredOutputError``'s own message is repo-authored, and the only
      two that interpolate stay inside closed vocabularies: CPython calls
      ``parse_constant`` with exactly ``NaN``/``Infinity``/``-Infinity``, and
      the other interpolates a ``TaskNodeId``. But *its* ``__cause__`` is
      typically a pydantic ``ValidationError``, which quotes the offending
      input -- that is model output, and printing the chain would leak it.
      ``str(exc)`` renders only that exception's own args, which is why this
      formats the cause rather than the traceback.

    ``AgentNodeFailedError`` keeps the old sentence: it carries no ``reason``,
    because its empty-output branch means an artifact was never written rather
    than a value that would not decode.
    """

    if isinstance(error, EvidenceUnavailableError):
        return f"evidence was unavailable during {action}: {error}"
    if isinstance(error, AgentNodeFailedError | TaskNodeRunFailedError):
        info = error.outcome.error
        if info is not None:
            retryable = "retryable" if info.retryable else "not retryable"
            return (
                f"the {error.node} step failed with {info.code} "
                f"({retryable}) during {action}"
            )
        blind = f"the {error.node} step did not produce usable output during {action}"
        if not isinstance(error, TaskNodeRunFailedError):
            return blind
        # The most specific safe text, and only that one. Appending both the
        # reason and its cause produced three clauses where the first two said
        # the same thing ("did not produce usable output: critic JSON did not
        # satisfy the review schema: critic reviewed a different revision"),
        # and the node is already named in the prefix -- which is the only
        # thing `reason` adds once a cause is present.
        cause = error.__cause__
        if isinstance(cause, StructuredOutputError):
            return f"{blind}: {cause}"
        return f"{blind}: {error.reason}"
    return f"the graph raised {type(error).__name__} during {action}"


def _is_retryable(error: BaseException) -> bool:
    """Whether another attempt could plausibly end differently (ADR-059).

    Deliberately narrower than "the exception looks transient": only a node
    failure whose own run classified its error as retryable qualifies. That
    classification already exists one layer down (``ErrorInfo.retryable``) and
    is where transport blips land -- the measured case in known-gaps B-06 was
    ``RemoteProtocolError``/``ConnectError`` arriving here as ``provider_error
    (retryable)`` and settling terminal anyway. Everything else -- evidence
    errors, missing ``ErrorInfo``, exceptions the graph itself raised -- stays
    non-retryable, because a deterministic failure retried ``max_attempts``
    times is the same answer at five times the price.
    """

    if isinstance(error, AgentNodeFailedError | TaskNodeRunFailedError):
        info = error.outcome.error
        return info is not None and info.retryable
    return False


@dataclass(frozen=True, slots=True)
class _ExecutionFailure:
    """Why the graph invocation failed, and whether an attempt could differ.

    The pair travels together because the caller needs both to pick a
    transition: the reason is what ``mark_failed`` records, and ``retryable``
    is what decides whether ``mark_failed`` runs at all.
    """

    reason: str
    retryable: bool


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
    #: How far past its own interval a heartbeat may wake and still be believed
    #: (ADR-041). Not a configuration field on purpose: a deployment that set it
    #: above ``lease_seconds`` would disable the self-check silently, and no
    #: config validator can catch that -- the two numbers live in different
    #: sections and the failure is a missing refusal, not a bad value.
    #:
    #: ``None`` derives it from ``heartbeat_seconds``, which is the whole point:
    #: the threshold is a property of the heartbeat rather than a knob, and the
    #: previous attempt at this (``event_loop_lag.py``'s hardcoded 10.0) drifted
    #: precisely because it was written down a second time. The production
    #: assembly still passes it explicitly so the derivation is visible there.
    abort_lag_seconds: int | None = None
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
    # Where this Worker publishes the claim it is executing under, for the nodes
    # inside the graph to read. It has a default because a Worker driving
    # handlers that never ask -- the demo graph, most workflow tests -- needs no
    # wiring; a composition whose nodes *do* ask passes the same scope to both
    # sides, and one that forgets fails closed at the first node rather than
    # running it under whatever claim the Registry currently reports.
    scope: TaskExecutionScope = field(default_factory=TaskExecutionScope)

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
                except AgentInvocationBudgetExhaustedError as exhausted:
                    # Dead-letter, not failure: the next claim reads the same
                    # full counter and refuses again, so another attempt is
                    # not a chance, it is the same answer later. The wording
                    # has to stay distinguishable from the reaper's
                    # "lease expired after N attempts" -- an operator who
                    # cannot tell the two writers apart is looking at a gate
                    # that might as well be destroying Tasks quietly.
                    task = await self.registry.mark_dead_lettered(
                        lease,
                        reason=(
                            f"agent invocation budget exhausted: spent "
                            f"{exhausted.spent} of {exhausted.ceiling} allowed"
                        ),
                    )
                    return TaskOutcome(task=task, decisions=tuple(decisions))
                except AgentInvocationCeilingMissingError:
                    # A deployment that cannot say what it allows. Its defect,
                    # not this Task's, so the Task stays retryable -- fixing
                    # the submission path must not require reviving a batch of
                    # dead-lettered Tasks.
                    task = await self._fail(
                        task,
                        lease,
                        "this task's run semantics carry no agent invocation "
                        "ceiling, so what it may spend is unknown",
                    )
                    return TaskOutcome(task=task, decisions=tuple(decisions))
                if failure is not None:
                    # A retryable failure is released, not settled (ADR-059).
                    # Only the exception path reaches here: a graph that wrote
                    # a `position.failed` checkpoint -- a reviewer out of
                    # revisions, a node that declined -- settles through
                    # `reconcile` above and is never offered a retry, which is
                    # the boundary between "the provider hiccuped" and "the
                    # graph decided".
                    if failure.retryable and task.attempt_count < self.max_attempts:
                        task = await self._release_for_retry(task, lease)
                    else:
                        reason = (
                            f"{failure.reason}; gave up after attempt "
                            f"{task.attempt_count} of {self.max_attempts}"
                            if failure.retryable
                            else failure.reason
                        )
                        task = await self._fail(task, lease, reason)
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
    ) -> _ExecutionFailure | None:
        """Run or resume the graph. Returns the failure when it failed."""

        execution = asyncio.create_task(
            self._invoke_graph(task, lease, decision, guard, approval),
            name=f"task-graph:{task.task_id}",
        )
        heartbeat = asyncio.create_task(
            # ``since`` is read here, at scheduling time, and not on the
            # coroutine's first line -- see ``_heartbeat_loop``. A stall that
            # begins inside the graph before this task ever runs has to count.
            self._heartbeat_loop(lease, since=asyncio.get_running_loop().time()),
            name=f"task-heartbeat:{task.task_id}",
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
        except (
            StaleExecutionError,
            AgentInvocationBudgetExhaustedError,
            AgentInvocationCeilingMissingError,
        ):
            # ADR-040. The budget refusals travel the same way a lost fence
            # does -- cancel the graph and let the caller pick the terminal
            # state -- rather than becoming a failure reason here. Which status
            # a refusal deserves is the Worker's decision, and the node that
            # raised only knows the condition.
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
        except Exception as error:
            logger.warning(
                "task %s failed during %s",
                task.task_id,
                decision.action,
                exc_info=True,
            )
            return _ExecutionFailure(
                reason=_failure_detail(error, decision.action),
                retryable=_is_retryable(error),
            )
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

    async def _release_for_retry(self, task: TaskRun, lease: ExecutionLease) -> TaskRun:
        """Give a retryably-failed Task back to the queue, with backoff.

        The delay formula is ``reclaim_expired``'s, on purpose: lease expiry
        and execution failure are the two producers of "try this again later",
        and two formulas would drift. ``attempt_count`` was incremented at
        claim, so the first release waits ``retry_base`` and the rest double
        toward ``retry_max``. A stale or rejected release means cancellation or
        reclaim won while this Worker was deciding -- same answer as
        ``_guard_unavailable_outcome``: the current row is the fact.
        """

        delay = min(
            self.retry_max_seconds,
            self.retry_base_seconds * 2 ** max(0, task.attempt_count - 1),
        )
        try:
            return await self.registry.release_for_retry(lease, delay_seconds=delay)
        except (StaleExecutionError, TaskTransitionRejectedError):
            current = await self.registry.get(task.task_id)
            return current if current is not None else task

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
        # The claim, published for the duration of this invocation and no
        # longer. A node inside the graph needs to know which lease it is
        # executing on behalf of, and the Registry cannot tell it: asked during
        # a node, the Registry answers with whoever holds the Task *now*, which
        # for a Worker whose lease lapsed mid-graph is the Worker that replaced
        # it. Writing under that epoch passes every fence that exists to refuse
        # this Worker specifically.
        with self.scope.executing(lease):
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
                    # resumed. An ordinary resume that carried one would be
                    # handing a wake-up to a graph that is not waiting for one,
                    # and the id is gated on the action rather than merely on
                    # the record so a future action that also has an approval in
                    # hand has to say so.
                    approval=(
                        approval.resume
                        if approval is not None
                        and decision.action == "resume_with_approval"
                        else None
                    ),
                )

    async def _heartbeat_loop(self, lease: ExecutionLease, *, since: float) -> None:
        """Renew the lease, but only while this process is actually running.

        ADR-041. The renewal is fenced on ``lease_until > now()``, and nothing
        in that predicate asks how long this coroutine was gone. So a process
        whose event loop froze for a minute wakes up, finds its lease still
        valid -- because the freeze was shorter than ``lease_seconds`` -- and
        pushes the expiry out another full period. It holds the claim by
        asserting a liveness it did not have, and no other Worker can reclaim
        the Task because ``reclaim_expired`` looks for an expiry that keeps
        moving away from it.

        The check belongs here rather than in a watchdog for a reason that is
        about capability, not taste: this coroutine is the only thing that was
        *waiting* through the stall and is therefore the first to know how long
        it lasted. A thread can detect a frozen loop sooner, but it cannot stop
        this renewal without holding a database handle of its own -- and a
        thread holding a database handle is one edit away from renewing the
        lease itself, which is the thing WP08-12 forbids.

        Refusing is expressed as ``StaleExecutionError`` because that is what it
        is: this Worker can no longer prove it owns the claim. It lands in the
        handler ``_execute`` already has for a lost fence, so the run is
        cancelled and nothing further is written -- no checkpoint, no
        heartbeat, no lifecycle transition. The lease is deliberately *not*
        released: letting it expire on its own is what lets a healthy Worker
        reclaim the Task under a new epoch, and a release from a process that
        just admitted it cannot be trusted about time is a write nobody should
        accept.
        """

        loop = asyncio.get_running_loop()
        # Derived once, outside the loop: it cannot change while a lease is held,
        # and reading it per iteration would suggest it could.
        abort_lag = (
            self.heartbeat_seconds
            if self.abort_lag_seconds is None
            else self.abort_lag_seconds
        )
        tolerated_seconds = self.heartbeat_seconds + abort_lag
        # The anchor is when this coroutine was *scheduled*, not when it first
        # got to run, and the difference is a real hole rather than a detail.
        # ``_execute`` creates this task and the graph task together; the loop
        # can freeze inside the graph before this one has executed a single
        # line. Anchoring on the first line to run would start the measurement
        # after the stall it exists to catch, and the first window -- the one
        # right after ``claim_next`` set ``lease_until`` -- is exactly where a
        # cold model load or a large parse lands.
        last_alive = since
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            # ``loop.time()`` is monotonic, so a clock adjustment during the
            # sleep can neither manufacture a stall nor hide one.
            gone_for = loop.time() - last_alive
            if gone_for > tolerated_seconds:
                # The numbers go to the log rather than into the exception:
                # ``StaleExecutionError`` carries the lease and is caught by
                # type, and an operator asking "why did this Task get handed to
                # someone else" needs the measurement, not a longer ``str()``.
                logger.warning(
                    "task %s heartbeat was gone %.1fs across a %ss sleep, past the "
                    "%ss this worker may be absent and still claim its lease; "
                    "refusing to renew -- something blocked the event loop",
                    lease.task_id,
                    gone_for,
                    self.heartbeat_seconds,
                    tolerated_seconds,
                )
                raise StaleExecutionError(lease)
            await self.registry.heartbeat(lease, lease_seconds=self.lease_seconds)
            # Re-anchor after the renewal rather than before it. The Registry
            # round trip is an await like any other, and time spent waiting on
            # a database is not this process failing to run.
            last_alive = loop.time()

    async def _settle(
        self, task: TaskRun, lease: ExecutionLease, decision: Reconciliation
    ) -> TaskRun:
        if decision.action == "propagate_terminal":
            # Already terminal. Writing anything here would be re-recording a
            # fact somebody else established, conditionally, and failing.
            return task
        if decision.action == "settle_succeeded":
            # The caveat rides along (ADR-060): a success that shipped with an
            # unanswered review says so on the row, where the console reads it.
            return await self._settle_or_current(
                task,
                lambda: self.registry.mark_succeeded(lease, detail=decision.caveat),
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
