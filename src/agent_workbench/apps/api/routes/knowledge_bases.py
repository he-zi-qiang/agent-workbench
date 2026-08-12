"""Human-facing knowledge-base discovery and document status."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.errors import ErrorCode
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.ports.knowledge_bases import (
    KnowledgeBaseSummary,
    KnowledgeDocument,
    KnowledgeDocumentStatus,
)

KNOWLEDGE_BASES_PREFIX = "/v1/knowledge-bases"


class CreateKnowledgeBaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=2000)


class KnowledgeBaseView(BaseModel):
    knowledge_base_id: Identifier
    name: str
    description: str | None
    # Told to the client so it can stop offering an upload that will be
    # refused. It is not what refuses it: the upload path calls
    # ``require_writable`` regardless of what any client believed.
    can_write: bool
    document_count: int
    ready_document_count: int
    processing_document_count: int
    failed_document_count: int
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseListResponse(BaseModel):
    knowledge_bases: tuple[KnowledgeBaseView, ...]


class KnowledgeDocumentView(BaseModel):
    document_id: Identifier
    filename: str | None
    media_type: str
    size_bytes: int
    source_revision: int
    last_applied_revision: int
    status: KnowledgeDocumentStatus
    failure_code: ErrorCode | None
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentListResponse(BaseModel):
    documents: tuple[KnowledgeDocumentView, ...]


router = APIRouter(prefix=KNOWLEDGE_BASES_PREFIX, tags=["knowledge-bases"])


@router.post("", response_model=KnowledgeBaseView, status_code=status.HTTP_201_CREATED)
async def create(
    body: CreateKnowledgeBaseRequest, request: Request
) -> KnowledgeBaseView:
    dependencies = dependencies_of(request)
    created = await dependencies.knowledge_bases.create(
        dependencies.principals.resolve(request),
        name=body.name,
        description=body.description,
    )
    return _view(created)


@router.get("", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(request: Request) -> KnowledgeBaseListResponse:
    dependencies = dependencies_of(request)
    records = await dependencies.knowledge_bases.list(
        dependencies.principals.resolve(request)
    )
    return KnowledgeBaseListResponse(
        knowledge_bases=tuple(_view(record) for record in records)
    )


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseView)
async def get(knowledge_base_id: str, request: Request) -> KnowledgeBaseView:
    dependencies = dependencies_of(request)
    record = await dependencies.knowledge_bases.get(
        dependencies.principals.resolve(request), knowledge_base_id
    )
    return _view(record)


@router.get(
    "/{knowledge_base_id}/documents", response_model=KnowledgeDocumentListResponse
)
async def list_documents(
    knowledge_base_id: str, request: Request
) -> KnowledgeDocumentListResponse:
    dependencies = dependencies_of(request)
    records = await dependencies.knowledge_bases.documents(
        dependencies.principals.resolve(request), knowledge_base_id
    )
    return KnowledgeDocumentListResponse(
        documents=tuple(_document_view(record) for record in records)
    )


def _view(record: KnowledgeBaseSummary) -> KnowledgeBaseView:
    return KnowledgeBaseView(
        knowledge_base_id=record.knowledge_base_id,
        name=record.name,
        description=record.description,
        can_write=record.can_write,
        document_count=record.document_count,
        ready_document_count=record.ready_document_count,
        processing_document_count=record.processing_document_count,
        failed_document_count=record.failed_document_count,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _document_view(record: KnowledgeDocument) -> KnowledgeDocumentView:
    return KnowledgeDocumentView(
        document_id=record.document_id,
        filename=record.filename,
        media_type=record.media_type,
        size_bytes=record.size_bytes,
        source_revision=record.source_revision,
        last_applied_revision=record.last_applied_revision,
        status=record.status,
        failure_code=record.failure_code,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


__all__ = [
    "KNOWLEDGE_BASES_PREFIX",
    "CreateKnowledgeBaseRequest",
    "KnowledgeBaseListResponse",
    "KnowledgeBaseView",
    "KnowledgeDocumentListResponse",
    "KnowledgeDocumentView",
    "router",
]
