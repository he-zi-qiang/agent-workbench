"""The single-Worker Task runner.

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

Multiple Workers are WP08. There is no lease, no advisory lock and no fencing
here, so this Worker is safe to run exactly one of.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Final

from agent_workbench.application.task_recovery import Reconciliation, reconcile
from agent_workbench.domain.tasks import TaskState
from agent_workbench.ports.task_registry import (
    TaskRegistry,
    TaskRun,
    TaskTransitionRejectedError,
)
from agent_workbench.ports.task_workflow import (
    GraphVersion,
    TaskWorkflowPort,
)

logger = logging.getLogger(__name__)

#: How many times one claimed Task may be decided about. Three covers the
#: longest legitimate chain -- decide, run, settle -- and turns anything longer
#: into a recorded failure rather than a Worker that never returns.
DEFAULT_MAX_DECISIONS: Final[int] = 3

LoadState = Callable[[TaskRun], Awaitable[TaskState]]


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
    max_decisions: int = DEFAULT_MAX_DECISIONS

    async def run_once(self) -> TaskOutcome | None:
        """Claim one Task and drive it, or return ``None`` if none is queued."""

        claimed = await self.registry.start_next()
        if claimed is None:
            return None

        task = claimed
        decisions: list[Reconciliation] = []
        for _ in range(self.max_decisions):
            # Re-read rather than trust the claim: a cancellation that landed
            # while the graph was running is exactly the fact this has to see,
            # and it is the reconciliation's first branch.
            current = await self.registry.get(task.task_id)
            if current is None:  # pragma: no cover - deletion is not a thing here
                raise RuntimeError(f"task {task.task_id} vanished mid-run")
            task = current

            position = await self.workflow.inspect(task.thread_id)
            decision = reconcile(
                status=task.status,
                graph_version=task.graph_version,
                position=position,
                buildable_versions=self.buildable_versions,
                # No approval can be pending: `approval` is a placeholder node
                # until WP10, so no graph in this build interrupts. When one
                # does, this is where the decision is read from.
                approval_decision=None,
            )
            decisions.append(decision)

            if not decision.keeps_executing:
                task = await self._settle(task, decision)
                return TaskOutcome(task=task, decisions=tuple(decisions))

            failure = await self._execute(task, decision)
            if failure is not None:
                task = await self._fail(task, failure)
                return TaskOutcome(task=task, decisions=tuple(decisions))

        task = await self._fail(
            task,
            f"the graph did not settle within {self.max_decisions} decisions",
        )
        return TaskOutcome(task=task, decisions=tuple(decisions))

    async def _execute(self, task: TaskRun, decision: Reconciliation) -> str | None:
        """Run or resume the graph. Returns a reason when it failed."""

        try:
            if decision.action == "start":
                await self.workflow.run(
                    await self.load_state(task),
                    thread_id=task.thread_id,
                    graph_version=task.graph_version,
                )
            else:
                await self.workflow.resume(
                    thread_id=task.thread_id,
                    graph_version=task.graph_version,
                )
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
        return None

    async def _settle(self, task: TaskRun, decision: Reconciliation) -> TaskRun:
        if decision.action == "propagate_terminal":
            # Already terminal. Writing anything here would be re-recording a
            # fact somebody else established, conditionally, and failing.
            return task
        if decision.action == "settle_succeeded":
            return await self.registry.mark_succeeded(task.task_id)
        if decision.action == "wait_for_migration":
            return await self.registry.park_for_migration(
                task.task_id, reason=decision.detail
            )
        if decision.action == "wait_for_approval":
            return await self.registry.await_approval(task.task_id)
        raise AssertionError(f"no settlement for {decision.action}")

    async def _fail(self, task: TaskRun, reason: str) -> TaskRun:
        try:
            return await self.registry.mark_failed(task.task_id, reason=reason)
        except TaskTransitionRejectedError:
            # The Task moved underneath this Worker -- cancelled, most likely.
            # Its status is the current fact and this failure is not.
            logger.info("task %s was already settled elsewhere", task.task_id)
            settled = await self.registry.get(task.task_id)
            return settled if settled is not None else task


__all__ = ["DEFAULT_MAX_DECISIONS", "TaskOutcome", "TaskWorker"]
