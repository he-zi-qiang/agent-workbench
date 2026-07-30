"""Reading an approval, and answering it.

Two routes, because a human needs exactly two things: to see what is being asked
and to answer it. Which approval to answer comes from the Task's own timeline --
``TaskApprovalRequested`` carries the id -- so nothing here lists or searches,
and there is no endpoint that would enumerate approvals for a probing caller.

``decision_version`` is the idempotency key and is supplied by the client, the
way ``Idempotency-Key`` is on submission. A repeated version is the same answer
arriving twice and leaves one row and one requeue; a higher one supersedes. It
is the caller's own approval either way, so a caller that picks a large version
can only outrank itself.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.task_registry import ApprovalDecision
from agent_workbench.ports.approvals import ApprovalRecord, ApprovalStatus

APPROVALS_PREFIX = "/v1/approvals"

#: A version is a client-supplied integer, so it is bounded like any other. The
#: ceiling is arbitrarily generous and exists only to keep the value an integer
#: a column can hold rather than to express a limit on how often humans change
#: their minds.
MAX_DECISION_VERSION = 1_000_000


class ApprovalView(BaseModel):
    """The caller-safe portion of a ledger row.

    Owner and tenant are absent for the same reason ``TaskView`` omits them: the
    caller had to *be* the owner to reach this, so echoing it back adds nothing
    and turns every response into a place identity can leak from. ``decided_by``
    is absent for a sharper reason -- it is a principal id, and today it can
    only ever be the caller's own, so returning it would be inventing a field
    whose meaning changes the moment somebody else is allowed to decide.
    """

    approval_id: Identifier
    task_id: Identifier
    status: ApprovalStatus
    decision_version: int
    decided_at: datetime | None
    created_at: datetime


class DecideApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    decision_version: int = Field(ge=1, le=MAX_DECISION_VERSION)


router = APIRouter(prefix=APPROVALS_PREFIX, tags=["approvals"])


@router.get("/{approval_id}", response_model=ApprovalView)
async def get(approval_id: str, request: Request) -> ApprovalView:
    dependencies = dependencies_of(request)
    record = await dependencies.approvals.get(
        dependencies.principals.resolve(request), approval_id
    )
    return _view(record)


@router.post("/{approval_id}/decisions", response_model=ApprovalView)
async def decide(
    approval_id: str,
    body: DecideApprovalRequest,
    request: Request,
) -> ApprovalView:
    dependencies = dependencies_of(request)
    record = await dependencies.approvals.decide(
        dependencies.principals.resolve(request),
        approval_id,
        decision=body.decision,
        decision_version=body.decision_version,
    )
    return _view(record)


def _view(record: ApprovalRecord) -> ApprovalView:
    return ApprovalView(
        approval_id=record.approval_id,
        task_id=record.task_id,
        status=record.status,
        decision_version=record.decision_version,
        decided_at=record.decided_at,
        created_at=record.created_at,
    )


__all__ = ["APPROVALS_PREFIX", "router"]
