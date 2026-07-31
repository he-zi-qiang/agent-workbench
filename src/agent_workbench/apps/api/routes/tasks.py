"""Submitting, inspecting and cancelling the caller's own Tasks.

Task execution is intentionally absent from this route module.  HTTP records
the immutable input and Registry row; the separately deployed Worker later
claims and runs it.  This keeps an API restart from becoming an implicit graph
runner and lets Task submission work while optional Chat dependencies are
unavailable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.events import EventEnvelope
from agent_workbench.domain.identifiers import ID_PATTERN, Identifier
from agent_workbench.domain.task_inputs import TaskInput
from agent_workbench.domain.task_registry import TaskStatus
from agent_workbench.ports.event_log import EventCursor
from agent_workbench.ports.task_registry import TaskRun

TASKS_PREFIX = "/v1/tasks"
MAX_CURSOR_LENGTH = 148
MAX_CANCEL_REASON_LENGTH = 1024


class InvalidTaskCursorError(ValueError):
    """A Task timeline cursor could not be decoded safely."""


class CreateTaskRequest(BaseModel):
    """The bounded user input for one general-purpose Task."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=4096)
    max_revisions: int = Field(default=2, ge=0, le=20)
    knowledge_base_id: Identifier | None = None


class TaskView(BaseModel):
    """The caller-safe portion of a Task Registry row."""

    task_id: Identifier
    status: TaskStatus
    status_detail: str | None
    created_at: datetime
    updated_at: datetime


class TaskTimelineResponse(BaseModel):
    task_id: Identifier
    events: tuple[EventEnvelope, ...]
    cursor: str | None = None


class CancelTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=MAX_CANCEL_REASON_LENGTH)


router = APIRouter(prefix=TASKS_PREFIX, tags=["tasks"])


@router.post("", response_model=TaskView, status_code=status.HTTP_201_CREATED)
async def submit(
    body: CreateTaskRequest,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=ID_PATTERN,
        ),
    ],
) -> TaskView:
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    task = await dependencies.task_inputs.submit(
        principal=principal,
        task_input=TaskInput(
            objective=body.objective,
            max_revisions=body.max_revisions,
            knowledge_base_id=body.knowledge_base_id,
        ),
        submission_dedup_key=idempotency_key,
    )
    return _view(task)


@router.get("/{task_id}", response_model=TaskView)
async def get(task_id: str, request: Request) -> TaskView:
    dependencies = dependencies_of(request)
    task = await dependencies.task_service.get(
        dependencies.principals.resolve(request), task_id
    )
    return _view(task)


@router.get("/{task_id}/timeline", response_model=TaskTimelineResponse)
async def timeline(
    task_id: str,
    request: Request,
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> TaskTimelineResponse:
    dependencies = dependencies_of(request)
    after = _decode_cursor(cursor)
    recorded = await dependencies.task_service.timeline(
        dependencies.principals.resolve(request),
        task_id,
        after=after,
        limit=limit,
    )
    return TaskTimelineResponse(
        task_id=recorded.task_id,
        events=recorded.events,
        cursor=None if recorded.cursor is None else recorded.cursor.encode(),
    )


@router.post("/{task_id}/cancel", response_model=TaskView)
async def cancel(
    task_id: str,
    body: CancelTaskRequest,
    request: Request,
) -> TaskView:
    dependencies = dependencies_of(request)
    task = await dependencies.task_service.cancel(
        dependencies.principals.resolve(request),
        task_id,
        reason=body.reason,
    )
    return _view(task)


def _decode_cursor(raw: str | None) -> EventCursor | None:
    if raw is None:
        return None
    try:
        return EventCursor.decode(raw)
    except Exception as error:
        # A cursor is transport input.  Do not let a lower-level schema error
        # turn an invalid request into a server error or disclose why a stream
        # name failed validation.
        raise InvalidTaskCursorError("invalid task timeline cursor") from error


def _view(task: TaskRun) -> TaskView:
    """Project a row without exposing owner, tenant or submitted semantics."""

    return TaskView(
        task_id=task.task_id,
        status=task.status,
        status_detail=task.status_detail,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


__all__ = ["TASKS_PREFIX", "InvalidTaskCursorError", "router"]
