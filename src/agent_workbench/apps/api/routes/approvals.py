"""Finding an approval, reading it, and answering it.

A human needs three things, and until now only had two. Which approval to answer
came from the Task's own timeline -- ``TaskApprovalRequested`` carries the id --
which meant a person could only answer a question they had already been told
about by some other channel. A queue nobody can see is not a queue.

Listing is not the enumeration this module used to refuse. The distinction is
whether the caller supplies an id: ``GET /{approval_id}`` can be probed, so it
answers 404 identically for absent and not-yours. The collection takes no id at
all -- it returns what this principal owns, and another owner's approvals are
not absent from it, they were never in it. That is the same rule ``/v1/search``
follows, and for the same reason.

``decision_version`` is the idempotency key and is supplied by the client, the
way ``Idempotency-Key`` is on submission. A repeated version is the same answer
arriving twice and leaves one row and one requeue; a higher one supersedes. It
is the caller's own approval either way, so a caller that picks a large version
can only outrank itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.application.approvals import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT
from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.pagination import (
    MAX_CURSOR_LENGTH as MAX_LIST_CURSOR_LENGTH,
)
from agent_workbench.domain.pagination import ListCursor
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


class ApprovalListResponse(BaseModel):
    """One page of the caller's own approvals."""

    approvals: tuple[ApprovalView, ...]
    cursor: str | None = None


class InvalidApprovalCursorError(ValueError):
    """An approval list cursor could not be decoded safely."""


class DecideApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    decision_version: int = Field(ge=1, le=MAX_DECISION_VERSION)


router = APIRouter(prefix=APPROVALS_PREFIX, tags=["approvals"])


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    request: Request,
    status_filter: Annotated[
        list[ApprovalStatus] | None,
        # The filter a person actually wants is ``status=pending``. It is not
        # the default: a queue that silently hid decided approvals would make
        # "I already answered that" indistinguishable from "it is gone".
        Query(alias="status"),
    ] = None,
    cursor: Annotated[str | None, Query(max_length=MAX_LIST_CURSOR_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
) -> ApprovalListResponse:
    dependencies = dependencies_of(request)
    page = await dependencies.approvals.list(
        dependencies.principals.resolve(request),
        statuses=tuple(status_filter or ()),
        limit=limit,
        after=_decode_cursor(cursor),
    )
    return ApprovalListResponse(
        approvals=tuple(_view(record) for record in page.approvals),
        cursor=None if page.cursor is None else page.cursor.encode(),
    )


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


def _decode_cursor(raw: str | None) -> ListCursor | None:
    if raw is None:
        return None
    try:
        return ListCursor.decode(raw)
    except Exception as error:
        raise InvalidApprovalCursorError("invalid approval list cursor") from error


def _view(record: ApprovalRecord) -> ApprovalView:
    return ApprovalView(
        approval_id=record.approval_id,
        task_id=record.task_id,
        status=record.status,
        decision_version=record.decision_version,
        decided_at=record.decided_at,
        created_at=record.created_at,
    )


__all__ = [
    "APPROVALS_PREFIX",
    "ApprovalListResponse",
    "InvalidApprovalCursorError",
    "router",
]
