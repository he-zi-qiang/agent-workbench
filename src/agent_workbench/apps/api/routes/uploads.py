"""The upload surface: declare, transfer, complete.

Three requests, because the bytes and the description travel differently. The
declaration and the completion are control requests: small, JSON, size-capped.
The transfer between them is the data plane, streamed straight to the artifact
store, and it is the one route the control limit does not apply to.

The transfer never names a location. A caller supplies an upload id it was
given and a body; where those bytes land is the store's decision. That is the
difference between an upload endpoint and a remote write primitive.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.artifacts import Sha256
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.ports.documents import DocumentVersion

UPLOADS_PREFIX = "/v1/uploads"
CONTENT_SUFFIX = "/content"

# Bounded here as well as in the database, so an over-long name is a 422 at the
# edge rather than a driver error three layers in.
FILENAME_MAX_LENGTH = 255


def is_data_plane_path(path: str) -> bool:
    """Only the transfer route carries document bytes.

    Deliberately narrow: the declaration and the completion live under the same
    prefix and must stay size-capped.
    """

    return path.startswith(f"{UPLOADS_PREFIX}/") and path.endswith(CONTENT_SUFFIX)


class CreateUploadRequest(BaseModel):
    """What a client promises before it transfers anything."""

    model_config = ConfigDict(extra="forbid")

    declared_size_bytes: int = Field(ge=0)
    declared_sha256: Sha256
    media_type: str
    filename: str | None = Field(default=None, max_length=FILENAME_MAX_LENGTH)


class CreateUploadResponse(BaseModel):
    upload_id: Identifier
    content_path: str


class ContentResponse(BaseModel):
    artifact_id: Identifier
    size_bytes: int
    sha256: Sha256


class CompleteUploadRequest(BaseModel):
    """Where the transferred object should become a document version."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: Identifier
    document_id: Identifier
    knowledge_base_id: Identifier
    granted_principals: tuple[Identifier, ...] = ()


router = APIRouter(prefix=UPLOADS_PREFIX, tags=["uploads"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_upload(
    body: CreateUploadRequest,
    request: Request,
) -> CreateUploadResponse:
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    intent = await dependencies.uploads.create_upload(
        tenant_id=principal.tenant_id,
        owner_id=principal.principal_id,
        declared_size_bytes=body.declared_size_bytes,
        declared_sha256=body.declared_sha256,
        media_type=body.media_type,
        filename=body.filename,
    )
    return CreateUploadResponse(
        upload_id=intent.upload_id,
        content_path=f"{UPLOADS_PREFIX}/{intent.upload_id}{CONTENT_SUFFIX}",
    )


@router.put("/{upload_id}/content")
async def transfer(upload_id: str, request: Request) -> ContentResponse:
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    # The intent is read first, so an upload that is unknown, another tenant's
    # or another principal's is refused before a single byte is stored.
    intent = await dependencies.documents.upload_intent(
        upload_id=upload_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
    )

    async def chunks() -> AsyncIterator[bytes]:
        async for chunk in request.stream():
            yield chunk

    stored = await dependencies.artifacts.put_stream(
        tenant_id=principal.tenant_id,
        owner_id=principal.principal_id,
        kind="source_document",
        media_type=intent.media_type,
        chunks=chunks(),
        max_bytes=dependencies.max_artifact_bytes,
        filename=intent.filename,
    )
    return ContentResponse(
        artifact_id=stored.artifact_id,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )


@router.post("/{upload_id}/complete", status_code=status.HTTP_201_CREATED)
async def complete(
    upload_id: str,
    body: CompleteUploadRequest,
    request: Request,
) -> DocumentVersion:
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    return await dependencies.uploads.complete_upload(
        upload_id=upload_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        artifact_id=body.artifact_id,
        document_id=body.document_id,
        knowledge_base_id=body.knowledge_base_id,
        granted_principals=body.granted_principals,
    )


__all__ = [
    "CONTENT_SUFFIX",
    "FILENAME_MAX_LENGTH",
    "UPLOADS_PREFIX",
    "CompleteUploadRequest",
    "ContentResponse",
    "CreateUploadRequest",
    "CreateUploadResponse",
    "is_data_plane_path",
    "router",
]
