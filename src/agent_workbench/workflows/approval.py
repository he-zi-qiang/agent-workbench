"""What the approval node does on either side of its pause.

The pause itself belongs to the workflow framework and therefore to the
adapter.  Everything around it is here, because both halves are decisions
rather than framework details: which question is being asked, and what the
answer is.

The second half is the one with teeth.  A resumed graph is handed a payload by
whoever resumed it, and this module never reads it.  It re-reads the decision
from the ledger by ``approval_id`` instead, so a forged or replayed resume
value cannot approve anything -- it can only wake a node that then asks
PostgreSQL what the human actually said.  A node that trusted the payload would
turn "a human approved this" into "somebody called resume".

Asking is idempotent by the graph operation, not by the invocation.  A node
re-entered after a crash asks the same question rather than opening a second
approval beside the one a human is already looking at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.task_registry import ApprovalDecision
from agent_workbench.domain.tasks import TaskState
from agent_workbench.ports.approvals import ApprovalRecord, ApprovalStore
from agent_workbench.ports.task_registry import TaskRegistry

#: The v1 graph has exactly one interrupt, and this names it. It is stable
#: across re-entries because it is the *operation*, not the attempt: the
#: ledger's uniqueness constraint is on ``(task_id, this)``, which is what makes
#: asking twice produce one approval.
APPROVAL_OPERATION_ID: Final[Identifier] = "approval:export"


class ApprovalTaskUnavailableError(RuntimeError):
    """The graph state names no current Task Registry row.

    An approval belongs to an owner inside a tenant, and those are facts about
    the Task rather than about the graph. Without the row there is nobody to ask
    and nobody entitled to answer, so the node fails instead of opening an
    approval attributed to whatever the checkpoint happened to carry.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"no task registry row for approval on task {task_id}")


class ApprovalNotDecidedError(RuntimeError):
    """The graph resumed at an approval the ledger has not decided.

    Reached by a resume that nobody's decision caused. Failing closed is the
    point: the alternatives are exporting without an answer and inventing a
    rejection, and neither is a thing a human said.
    """

    def __init__(self, approval_id: str) -> None:
        self.approval_id = approval_id
        super().__init__(f"approval {approval_id} has no recorded decision")


@dataclass(frozen=True, slots=True)
class TaskApprovalGate:
    """Open the approval a Task is paused on, and read back its answer."""

    approvals: ApprovalStore
    # Read per invocation rather than taken from the checkpoint: owner and
    # tenant are the Registry's facts, and a resumed graph must not attribute an
    # approval to the principal some earlier process was running as.
    registry: TaskRegistry

    async def open(self, state: TaskState) -> ApprovalRecord:
        """Ask for this Task's export approval, or return the open one."""

        task = await self.registry.get(state.task_id)
        if task is None:
            raise ApprovalTaskUnavailableError(state.task_id)
        return await self.approvals.request(
            task_id=task.task_id,
            graph_node_operation_id=APPROVAL_OPERATION_ID,
            tenant_id=task.tenant_id,
            owner_id=task.owner_id,
        )

    async def decision(self, approval_id: Identifier) -> ApprovalDecision:
        """The authoritative answer, read from the ledger and nowhere else."""

        record = await self.approvals.get(approval_id)
        if record is None or record.status == "pending":
            raise ApprovalNotDecidedError(approval_id)
        return record.status


__all__ = [
    "APPROVAL_OPERATION_ID",
    "ApprovalNotDecidedError",
    "ApprovalTaskUnavailableError",
    "TaskApprovalGate",
]
