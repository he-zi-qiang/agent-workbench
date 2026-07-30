"""Framework-neutral boundary for human approval of a paused Task.

Two operations, and the second is the one with teeth. Requesting an approval is
idempotent by the graph operation it belongs to, so a node that is re-entered
after a crash asks the same question rather than a second one. Deciding is a
single transaction that must do four things together or none of them: record the
decision, move the Task from ``waiting_approval`` back to ``queued``, name the
approval as the reason it was requeued, and write the durable event.

A decision arriving for a Task that is no longer waiting is refused, not
applied. The Task may have been cancelled while a human was thinking, and a late
approval that reopened it would resurrect work somebody stopped -- so the
transition matches zero rows and this says so.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel
from agent_workbench.domain.task_registry import ApprovalDecision

ApprovalStatus = Literal["pending", "approved", "rejected"]


class ApprovalRecord(DomainModel):
    """One approval, pending or decided."""

    approval_id: Identifier
    task_id: Identifier
    graph_node_operation_id: Identifier
    tenant_id: Identifier
    owner_id: Identifier
    status: ApprovalStatus
    decision_version: int = Field(default=0, ge=0)
    decided_by: Identifier | None = None
    decided_at: datetime | None = None
    created_at: datetime


class ApprovalTaskNotFoundError(RuntimeError):
    """An approval was requested for a Task the ledger cannot find.

    An approval belongs to a Task's timeline, so there is nowhere to record one
    for a Task that is not there. Raised rather than tolerated: the alternative
    is a pending approval nobody can discover and nobody can decide.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"cannot open an approval for unknown task {task_id}")


class ApprovalNotDecidableError(RuntimeError):
    """The approval or its Task is not in a state a decision may be applied to.

    Carries what was found, because "no rows matched" cannot distinguish a Task
    somebody cancelled from an approval that was already decided -- and the
    caller is a person waiting to hear why their click did nothing.
    """

    def __init__(
        self,
        *,
        approval_id: str,
        task_status: str | None,
        approval_status: str | None,
    ) -> None:
        self.approval_id = approval_id
        self.task_status = task_status
        self.approval_status = approval_status
        super().__init__(
            f"approval {approval_id} cannot be decided: "
            f"task is {task_status or 'missing'}, "
            f"approval is {approval_status or 'missing'}"
        )


@runtime_checkable
class ApprovalStore(Protocol):
    """Ask a human, and record what they answered."""

    async def request(
        self,
        *,
        task_id: Identifier,
        graph_node_operation_id: Identifier,
        tenant_id: Identifier,
        owner_id: Identifier,
    ) -> ApprovalRecord:
        """Open an approval, or return the one this operation already opened."""
        ...

    async def get(self, approval_id: Identifier) -> ApprovalRecord | None: ...

    async def decide(
        self,
        approval_id: Identifier,
        *,
        decision: ApprovalDecision,
        decision_version: int,
        decided_by: Identifier,
    ) -> ApprovalRecord:
        """Record the decision and requeue the Task, in one transaction.

        Replaying a decision at a version already recorded is a no-op that
        returns the stored record: the same answer arriving twice must leave one
        row and requeue once. A *newer* version supersedes.
        """
        ...


__all__ = [
    "ApprovalNotDecidableError",
    "ApprovalRecord",
    "ApprovalStatus",
    "ApprovalStore",
    "ApprovalTaskNotFoundError",
]
