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
from agent_workbench.domain.project_files import ProjectPathError
from agent_workbench.ports.project_files import (
    DirectoryListing,
    ProjectEntryKind,
    ProjectFileContent,
    ProjectListing,
)
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
    """Rename, archive, register a directory, or any combination.

    ``root_path`` needs the same ``null`` / absent distinction that
    ``AssignProjectRequest`` documents, and for the same reason: ``null`` is how
    "stop pointing this project at that folder" is said, and an absent field has
    to keep meaning "leave it alone". The route asks ``model_fields_set``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None
    root_path: str | None = Field(default=None, max_length=2048)


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
    #: The directory this project is (ADR-072), or null. Present-and-null for
    #: the same reason as ``archived_at``: a client has to be able to tell "no
    #: directory registered" from "this build does not do directories", and an
    #: omitted field says neither.
    root_path: str | None


class ProjectFileEntryView(BaseModel):
    path: str
    kind: ProjectEntryKind
    size_bytes: int | None
    modified_at: datetime


class ProjectListingResponse(BaseModel):
    path: str
    entries: tuple[ProjectFileEntryView, ...]
    #: Whether the ceiling cut this listing short. A client that ignores it
    #: renders a partial tree as a whole one, which reads to a person as *this
    #: project has 500 files*.
    truncated: bool


class ProjectFileContentResponse(BaseModel):
    path: str
    text: str | None
    size_bytes: int
    is_text: bool
    modified_at: datetime


class DirectoryEntryView(BaseModel):
    name: str
    path: str


class DirectoryListingResponse(BaseModel):
    path: str
    #: Null at the filesystem root. Sent rather than derived so the client never
    #: does path arithmetic of its own.
    parent: str | None
    entries: tuple[DirectoryEntryView, ...]
    truncated: bool


class WriteProjectFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    #: Text only, over this interface. Bytes would have to arrive base64-encoded
    #: and the first thing a browser would do with the result is decode it as
    #: text anyway; a binary upload is a different endpoint with a different
    #: content type, not a flag on this one.
    content: str


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


# --- choosing which directory (ADR-074) -------------------------------------
#
# Above `/{project_id}` in the file deliberately: FastAPI matches in declaration
# order, and `/v1/projects/directories` would otherwise be swallowed by
# `/v1/projects/{project_id}` and arrive as a project id of "directories".


@router.get("/directories", response_model=DirectoryListingResponse)
async def browse_directories(
    request: Request, path: str | None = None
) -> DirectoryListingResponse:
    dependencies = dependencies_of(request)
    try:
        listing = await dependencies.projects.browse_directories(
            dependencies.principals.resolve(request), path=path
        )
    except ProjectPathError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error
    return _directory_view(listing)


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
    # `root_path` is asked of `model_fields_set`, not of the value: `null` is a
    # request to unregister and absent is a request to leave it alone, and the
    # parsed value is `None` for both.
    sets_root = "root_path" in body.model_fields_set
    if body.name is None and body.archived is None and not sets_root:
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
    if sets_root:
        try:
            record = await dependencies.projects.set_root_path(
                principal, project_id, root_path=body.root_path
            )
        except ProjectPathError as error:
            # 400, not 500: a path that is not absolute, not there, or not a
            # directory is a bad request, and the person who typed it is the one
            # who can fix it. The message is the sandbox's own -- it names which
            # of those it was, and flattening that to "invalid path" would make
            # the one useful thing about the refusal disappear.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
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


# --- the directory a project is (ADR-072) -----------------------------------
#
# The path is a query parameter, not a path segment. A project-relative path
# contains `/`, and a path segment holding one either needs a `:path` converter
# -- which then also swallows the trailing segments of any route added after it
# -- or arrives percent-encoded and gets decoded twice somewhere. A query
# parameter has one unambiguous encoding and no interaction with routing.


@router.get("/{project_id}/files", response_model=ProjectListingResponse)
async def list_files(
    project_id: str, request: Request, path: str = "", recursive: bool = False
) -> ProjectListingResponse:
    dependencies = dependencies_of(request)
    store = await dependencies.projects.open_files(
        dependencies.principals.resolve(request), project_id
    )
    try:
        listing = await (store.walk(path) if recursive else store.list_directory(path))
    except ProjectPathError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error
    return _listing_view(listing)


@router.get("/{project_id}/file", response_model=ProjectFileContentResponse)
async def read_file(
    project_id: str, request: Request, path: str
) -> ProjectFileContentResponse:
    dependencies = dependencies_of(request)
    store = await dependencies.projects.open_files(
        dependencies.principals.resolve(request), project_id
    )
    try:
        return _content_view(await store.read(path))
    except ProjectPathError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error


@router.put("/{project_id}/file", response_model=ProjectFileEntryView)
async def write_file(
    project_id: str, body: WriteProjectFileRequest, request: Request
) -> ProjectFileEntryView:
    dependencies = dependencies_of(request)
    store = await dependencies.projects.open_files(
        dependencies.principals.resolve(request), project_id
    )
    try:
        entry = await store.write(body.path, body.content)
    except ProjectPathError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error
    return ProjectFileEntryView(
        path=entry.path,
        kind=entry.kind,
        size_bytes=entry.size_bytes,
        modified_at=entry.modified_at,
    )


@router.delete("/{project_id}/file", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(project_id: str, request: Request, path: str) -> Response:
    dependencies = dependencies_of(request)
    store = await dependencies.projects.open_files(
        dependencies.principals.resolve(request), project_id
    )
    try:
        await store.delete(path)
    except ProjectPathError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error
    # 204 whether or not it was there. The store reports the difference and this
    # route drops it on purpose: DELETE is idempotent, and a 404 for "already
    # gone" makes a retry after a dropped response look like a failure.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    "/v1/code/sessions/{session_id}/project", status_code=status.HTTP_204_NO_CONTENT
)
async def set_code_session_project(
    session_id: str, body: AssignProjectRequest, request: Request
) -> Response:
    """File a coding session into a project, or take it out.

    Same application call as the chat one directly above, because a coding
    session *is* a `conversation_sessions` row -- `CodeSession.open` creates it
    with ``mode="code"`` and both features read the one store. So membership
    needed no column, no migration and no second code path.

    It still gets its own path rather than reusing ``/v1/chat/sessions/...``.
    The URL is what a reader of the API sees, and telling somebody to PATCH a
    *chat* session in order to file a *coding* one is asking them to know an
    implementation detail that the rest of this surface is careful to hide:
    every other coding route lives under ``/v1/code``.
    """

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
        root_path=record.root_path,
    )


def _directory_view(listing: DirectoryListing) -> DirectoryListingResponse:
    return DirectoryListingResponse(
        path=listing.path,
        parent=listing.parent,
        entries=tuple(
            DirectoryEntryView(name=entry.name, path=entry.path)
            for entry in listing.entries
        ),
        truncated=listing.truncated,
    )


def _listing_view(listing: ProjectListing) -> ProjectListingResponse:
    return ProjectListingResponse(
        path=listing.path,
        entries=tuple(
            ProjectFileEntryView(
                path=entry.path,
                kind=entry.kind,
                size_bytes=entry.size_bytes,
                modified_at=entry.modified_at,
            )
            for entry in listing.entries
        ),
        truncated=listing.truncated,
    )


def _content_view(content: ProjectFileContent) -> ProjectFileContentResponse:
    return ProjectFileContentResponse(
        path=content.path,
        text=content.text,
        size_bytes=content.size_bytes,
        is_text=content.is_text,
        modified_at=content.modified_at,
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
