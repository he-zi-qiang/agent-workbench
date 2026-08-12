"""Knowledge bases and the readable documents they contain.

A knowledge base is a durable product object, not a string a caller happens to
repeat on uploads and searches.  PostgreSQL owns its name, creator and document
membership; the vector index remains only a derived search projection.

Read visibility deliberately follows both ownership and the existing document
ACL.  An owner can see an empty knowledge base.  A principal granted one of its
documents can discover the containing knowledge base and only the documents
they may read.  Write authority is narrower: today only the knowledge-base
owner may add documents.

The summary carries that write authority as a fact rather than leaving callers
to guess it.  Without it every readable base looked writable, so a reader of a
shared base was offered an upload control, transferred the whole file, and only
then met the refusal -- with an orphaned artifact left behind.  ``can_write``
is a projection for the interface, never the decision: the refusal still comes
from ``KnowledgeBaseService.require_writable`` on the write path itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field, StringConstraints

from agent_workbench.domain.errors import ErrorCode
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import VersionedModel

KnowledgeBaseName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
KnowledgeBaseDescription = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
]
#: ``failed`` exists because its absence was a lie with no expiry date.  A
#: document whose bytes no parser in this build can read is retried, refused
#: and retried again, and while only ``processing`` and ``ready`` existed the
#: page said "indexing" about it for as long as anybody kept looking.
KnowledgeDocumentStatus = Literal["processing", "ready", "failed"]


class KnowledgeBaseRecord(VersionedModel):
    """The durable identity and display metadata of one knowledge base."""

    knowledge_base_id: Identifier
    tenant_id: Identifier
    owner_id: Identifier
    name: KnowledgeBaseName
    description: KnowledgeBaseDescription | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseSummary(VersionedModel):
    """A caller-specific knowledge-base projection with readable counts."""

    knowledge_base_id: Identifier
    name: KnowledgeBaseName
    description: KnowledgeBaseDescription | None = None
    #: Whether this caller may add documents here.  Advisory: an interface uses
    #: it to decide what to offer, and the write path decides what to allow.
    can_write: bool
    document_count: int = Field(ge=0)
    ready_document_count: int = Field(ge=0)
    processing_document_count: int = Field(ge=0)
    #: Counted apart from ``processing_document_count`` rather than folded into
    #: it, because a base that shows "3 processing" forever is the same lie as
    #: a document that does -- one level up, where the reader looks first.
    failed_document_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class KnowledgeDocument(VersionedModel):
    """One currently readable document with its truthful ingestion state."""

    document_id: Identifier
    filename: str | None = None
    media_type: str
    size_bytes: int = Field(ge=0)
    source_revision: int = Field(ge=1)
    last_applied_revision: int = Field(ge=0)
    status: KnowledgeDocumentStatus
    #: Why the last ingestion attempt refused this revision, and only while it
    #: still describes the revision being waited on -- a marker a later retry
    #: cleared is not a reason.  It is an ``ErrorCode`` rather than the
    #: exception's text for the reason ``ErrorInfo.from_exception`` exists: a
    #: parser's message quotes the document's own bytes back at every principal
    #: who can read the base.
    failure_code: ErrorCode | None = None
    created_at: datetime
    updated_at: datetime


@runtime_checkable
class KnowledgeBaseStore(Protocol):
    """Persist knowledge bases and project their ACL-filtered contents."""

    async def create(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord:
        """Persist one new knowledge base."""

        ...

    async def get(
        self, *, tenant_id: str, knowledge_base_id: str
    ) -> KnowledgeBaseRecord | None:
        """Return the tenant-scoped record without making an auth decision."""

        ...

    async def describe_readable(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        knowledge_base_id: str,
    ) -> KnowledgeBaseSummary | None:
        """Return a caller-safe summary, or ``None`` when it is not readable."""

        ...

    async def list_readable(
        self, *, tenant_id: str, principal_id: str
    ) -> tuple[KnowledgeBaseSummary, ...]:
        """Every knowledge base visible to this principal, newest first."""

        ...

    async def list_readable_documents(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        knowledge_base_id: str,
    ) -> tuple[KnowledgeDocument, ...]:
        """Readable, non-deleted documents in one knowledge base."""

        ...


__all__ = [
    "KnowledgeBaseDescription",
    "KnowledgeBaseName",
    "KnowledgeBaseRecord",
    "KnowledgeBaseStore",
    "KnowledgeBaseSummary",
    "KnowledgeDocument",
    "KnowledgeDocumentStatus",
]
