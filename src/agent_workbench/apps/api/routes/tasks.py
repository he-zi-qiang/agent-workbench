"""Submitting, inspecting and cancelling the caller's own Tasks.

Task execution is intentionally absent from this route module.  HTTP records
the immutable input and Registry row; the separately deployed Worker later
claims and runs it.  This keeps an API restart from becoming an implicit graph
runner and lets Task submission work while optional Chat dependencies are
unavailable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.application.tasks import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    TaskGraphChoice,
)
from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.events import EventEnvelope
from agent_workbench.domain.identifiers import ID_PATTERN, Identifier
from agent_workbench.domain.pagination import (
    MAX_CURSOR_LENGTH as MAX_LIST_CURSOR_LENGTH,
)
from agent_workbench.domain.pagination import ListCursor
from agent_workbench.domain.task_inputs import TaskInput
from agent_workbench.domain.task_intent import TaskIntent
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
    # Whether this Task should end in a downloadable file. Defaulted False so a
    # client that does not know about the field submits the cheaper shape --
    # one that never stops to ask a human to authorize an export.
    wants_report: bool = False
    # Which pipeline this Task runs: "research" writes a grounded report,
    # "general" does a thing and reviews whether it is done (ADR-031).
    #
    # Absent means the deployment's default, which is what every existing
    # client sends and is byte-for-byte the behaviour they have today. There
    # is deliberately no "auto" value here: a model may *propose* a shape via
    # the triage endpoint below, but what this endpoint receives is always an
    # explicit choice or nothing, so freezing and idempotency never depend on
    # a value a model produced (ADR-036, superseding ADR-031 §2.3's blanket
    # refusal).
    graph: TaskGraphChoice | None = None
    # Who decided graph and wants_report, when the client knows (ADR-036).
    # Provenance for the timeline, never authority: the Registry stores it on
    # the TaskSubmitted event and reads none of it back.
    intent: TaskIntent | None = None


class TaskView(BaseModel):
    """The caller-safe portion of a Task Registry row."""

    task_id: Identifier
    status: TaskStatus
    status_detail: str | None
    # The caller's own submitted objective, cut to a label. Safe to return for
    # the same reason status_detail is: list and get are already scoped to the
    # caller's own Tasks, so this returns their text to them. Absent on Tasks
    # submitted before the column existed.
    objective_preview: str | None = None
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    """One page of the caller's own Tasks.

    ``cursor`` is absent at the end of the list. A client stops when it is
    absent rather than when the page is short, so the two are never allowed to
    disagree about whether there is more.
    """

    tasks: tuple[TaskView, ...]
    cursor: str | None = None


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
            wants_report=body.wants_report,
        ),
        submission_dedup_key=idempotency_key,
        graph=body.graph,
        intent=body.intent,
    )
    return _view(task)


class TriageTaskRequest(BaseModel):
    """What triage may read: the objective and two facts about the form."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=4096)
    knowledge_base_selected: bool = False
    # Names only, and few: context for the classifier, not an upload channel.
    attachment_names: tuple[str, ...] = Field(default=(), max_length=16)


class TriageOption(BaseModel):
    """One choice a client renders when triage asks."""

    graph: TaskGraphChoice
    label: str


class TriageTaskResponse(BaseModel):
    """One of three outcomes; only that outcome's fields are set.

    ``default`` is deliberately indistinguishable across its causes -- triage
    disabled, no model, timeout, unreadable output -- because every cause has
    the same instruction to the client: submit exactly what you would have
    submitted before this endpoint existed (ADR-036).
    """

    status: Literal["decided", "ask", "default"]
    graph: TaskGraphChoice | None = None
    wants_report: bool | None = None
    reason: str | None = None
    question: str | None = None
    options: tuple[TriageOption, ...] = ()


#: What a client shows beside each chip when triage asks. Server-supplied so
#: a client that knows nothing about the graphs can still render the choice.
TRIAGE_OPTIONS = (
    TriageOption(graph="research", label="调研报告"),
    TriageOption(graph="general", label="通用执行"),
)


@router.post("/triage", response_model=TriageTaskResponse)
async def triage(body: TriageTaskRequest, request: Request) -> TriageTaskResponse:
    """Propose a shape for this objective, ask, or say "default".

    No side effects and no Task: this runs before submission, which is what
    lets uncertainty become a question instead of a status (ADR-036 §2.1).
    Registered on a literal path; FastAPI matches it before ``/{task_id}``
    only because routes are matched in declaration order, so it stays above
    the parameterized routes in this module.
    """

    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    service = dependencies.triage
    if service is None:
        return TriageTaskResponse(status="default")
    result = await service.triage(
        principal,
        objective=body.objective,
        knowledge_base_selected=body.knowledge_base_selected,
        attachment_names=body.attachment_names,
    )
    if result.status == "ask":
        return TriageTaskResponse(
            status="ask",
            question=result.question,
            options=TRIAGE_OPTIONS,
        )
    return TriageTaskResponse(
        status=result.status,
        graph=result.graph,
        wants_report=result.wants_report,
        reason=result.reason,
    )


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    request: Request,
    status_filter: Annotated[
        list[TaskStatus] | None,
        # Named ``status`` on the wire because that is what it filters, and
        # repeated rather than comma-joined so a value never has to be escaped.
        Query(alias="status"),
    ] = None,
    cursor: Annotated[str | None, Query(max_length=MAX_LIST_CURSOR_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
) -> TaskListResponse:
    dependencies = dependencies_of(request)
    page = await dependencies.task_service.list(
        dependencies.principals.resolve(request),
        statuses=tuple(status_filter or ()),
        limit=limit,
        after=_decode_list_cursor(cursor),
    )
    return TaskListResponse(
        tasks=tuple(_view(task) for task in page.tasks),
        cursor=None if page.cursor is None else page.cursor.encode(),
    )


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


def _decode_list_cursor(raw: str | None) -> ListCursor | None:
    if raw is None:
        return None
    try:
        return ListCursor.decode(raw)
    except Exception as error:
        # Same treatment as a timeline cursor: it is transport input, so a
        # lower-level schema error must not become a 500 or explain itself.
        raise InvalidTaskCursorError("invalid task list cursor") from error


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
        objective_preview=task.objective_preview,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


__all__ = ["TASKS_PREFIX", "InvalidTaskCursorError", "TaskListResponse", "router"]
