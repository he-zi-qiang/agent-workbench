"""Projects, and the membership between them and everything else.

Two routers. The first is the project itself, under ``/v1/projects``. The second
carries the membership PATCHes, which belong on the thing being filed rather
than on the project -- ``/v1/chat/sessions/{id}/project`` reads as "this
session's project", which is what it sets.

The one subtle piece of the contract is in ``AssignProjectRequest``: ``null``
means *no membership* and an absent field means *change nothing*, and pydantic
cannot tell those apart from the parsed value alone. ``model_fields_set`` can,
so the route asks it. Without that distinction "take this out of the project"
has no way of being said at all (ADR-071 4).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.ports.projects import (
    ProjectContents,
    ProjectItemKind,
    ProjectRecord,
)

PROJECTS_PREFIX = "/v1/projects"


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class UpdateProjectRequest(BaseModel):
    """Rename, archive, or both. Absent fields are left alone."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class AssignProjectRequest(BaseModel):
    """``{"project_id": null}`` takes it out; an absent field changes nothing."""

    model_config = ConfigDict(extra="forbid")

    project_id: Identifier | None = None


class ProjectView(BaseModel):
    project_id: Identifier
    name: str
    created_at: datetime
    updated_at: datetime
    #: Present and null for a live project rather than omitted, so a client can
    #: tell "not archived" from "this build does not report archiving".
    archived_at: datetime | None


class ProjectListResponse(BaseModel):
    projects: tuple[ProjectView, ...]


class ProjectItemView(BaseModel):
    kind: ProjectItemKind
    item_id: Identifier
    title: str | None
    ordered_at: datetime


class ProjectContentsResponse(BaseModel):
    project_id: Identifier
    items: tuple[ProjectItemView, ...]


router = APIRouter(prefix=PROJECTS_PREFIX, tags=["projects"])
#: Membership lives on the thing being filed, so these paths do too.
membership_router = APIRouter(tags=["projects"])


@router.post("", response_model=ProjectView, status_code=status.HTTP_201_CREATED)
async def create(body: CreateProjectRequest, request: Request) -> ProjectView:
    dependencies = dependencies_of(request)
    record = await dependencies.projects.create(
        dependencies.principals.resolve(request), name=body.name
    )
    return _view(record)


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    request: Request, include_archived: bool = False
) -> ProjectListResponse:
    dependencies = dependencies_of(request)
    records = await dependencies.projects.list(
        dependencies.principals.resolve(request), include_archived=include_archived
    )
    return ProjectListResponse(projects=tuple(_view(record) for record in records))


@router.get("/{project_id}", response_model=ProjectView)
async def get(project_id: str, request: Request) -> ProjectView:
    dependencies = dependencies_of(request)
    return _view(
        await dependencies.projects.get(
            dependencies.principals.resolve(request), project_id
        )
    )


@router.patch("/{project_id}", response_model=ProjectView)
async def update(
    project_id: str, body: UpdateProjectRequest, request: Request
) -> ProjectView:
    if body.name is None and body.archived is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="这个请求什么也没要求改。",
        )
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    record: ProjectRecord | None = None
    if body.name is not None:
        record = await dependencies.projects.rename(
            principal, project_id, name=body.name
        )
    if body.archived is not None:
        record = await dependencies.projects.set_archived(
            principal, project_id, archived=body.archived
        )
    assert record is not None
    return _view(record)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(project_id: str, request: Request) -> Response:
    dependencies = dependencies_of(request)
    await dependencies.projects.delete(
        dependencies.principals.resolve(request), project_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/items", response_model=ProjectContentsResponse)
async def items(project_id: str, request: Request) -> ProjectContentsResponse:
    dependencies = dependencies_of(request)
    found = await dependencies.projects.contents(
        dependencies.principals.resolve(request), project_id
    )
    return _contents_view(found)


@router.put(
    "/{project_id}/knowledge-bases/{knowledge_base_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def link_knowledge_base(
    project_id: str, knowledge_base_id: str, request: Request
) -> Response:
    dependencies = dependencies_of(request)
    await dependencies.projects.link_knowledge_base(
        dependencies.principals.resolve(request),
        project_id,
        knowledge_base_id=knowledge_base_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{project_id}/knowledge-bases/{knowledge_base_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unlink_knowledge_base(
    project_id: str, knowledge_base_id: str, request: Request
) -> Response:
    dependencies = dependencies_of(request)
    await dependencies.projects.unlink_knowledge_base(
        dependencies.principals.resolve(request),
        project_id,
        knowledge_base_id=knowledge_base_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@membership_router.patch(
    "/v1/chat/sessions/{session_id}/project", status_code=status.HTTP_204_NO_CONTENT
)
async def set_session_project(
    session_id: str, body: AssignProjectRequest, request: Request
) -> Response:
    _require_stated(body)
    dependencies = dependencies_of(request)
    await dependencies.projects.assign_session(
        dependencies.principals.resolve(request),
        session_id,
        project_id=body.project_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@membership_router.patch(
    "/v1/tasks/{task_id}/project", status_code=status.HTTP_204_NO_CONTENT
)
async def set_task_project(
    task_id: str, body: AssignProjectRequest, request: Request
) -> Response:
    _require_stated(body)
    dependencies = dependencies_of(request)
    await dependencies.projects.assign_task(
        dependencies.principals.resolve(request), task_id, project_id=body.project_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _require_stated(body: AssignProjectRequest) -> None:
    """Refuse a PATCH that did not say anything.

    ``{"project_id": null}`` and ``{}`` parse to the same value and mean
    different things. Treating the second as "take it out" would make an empty
    body destructive, so it is refused instead.
    """

    if "project_id" not in body.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="要说清楚放进哪个项目，或者用 null 表示拿出来。",
        )


def _view(record: ProjectRecord) -> ProjectView:
    return ProjectView(
        project_id=record.project_id,
        name=record.name,
        created_at=record.created_at,
        updated_at=record.updated_at,
        archived_at=record.archived_at,
    )


def _contents_view(found: ProjectContents) -> ProjectContentsResponse:
    return ProjectContentsResponse(
        project_id=found.project_id,
        items=tuple(
            ProjectItemView(
                kind=item.kind,
                item_id=item.item_id,
                title=item.title,
                ordered_at=item.ordered_at,
            )
            for item in found.items
        ),
    )
