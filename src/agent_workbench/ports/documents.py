"""The document boundary: what was uploaded, and who may see it.

PostgreSQL is the authority on documents, their versions and their ACL. The
vector index is a derived copy that can be dropped and rebuilt from these rows,
which is only true while nothing ever writes a fact to the index that is not
here first.

An upload is two steps because the bytes and the metadata travel different
paths. The intent records what the client promised -- size, digest, media type
-- before anything is transferred; completion compares the stored object
against that promise and only then does the document exist. A transfer that
delivered something else can therefore never become a version.

Revisions are monotonic per document. Every change takes the next one, and an
outbox event carries the revision it describes, so an event that arrives late
can be recognized as describing a past state rather than applied over a newer
one.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from agent_workbench.domain.artifacts import Sha256
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import VersionedModel

UploadStatus = Literal["pending", "completed"]

MediaType = str


class UploadIntent(VersionedModel):
    """A declared, not yet delivered, upload."""

    upload_id: Identifier
    tenant_id: Identifier
    owner_id: Identifier
    declared_size_bytes: int = Field(ge=0)
    declared_sha256: Sha256
    media_type: MediaType
    filename: str | None = None
    status: UploadStatus = "pending"
    version_id: Identifier | None = None


class DocumentVersion(VersionedModel):
    """One immutable version of a document's content."""

    version_id: Identifier
    document_id: Identifier
    source_revision: int = Field(ge=1)
    artifact_id: Identifier
    content_sha256: Sha256


class Document(VersionedModel):
    """The document aggregate: identity, ownership and current revision."""

    document_id: Identifier
    tenant_id: Identifier
    owner_id: Identifier
    knowledge_base_id: Identifier
    source_revision: int = Field(ge=1)
    deleted: bool = False


@runtime_checkable
class DocumentStore(Protocol):
    """Documents, their versions and the ACL that governs them."""

    async def create_upload(
        self,
        *,
        upload_id: str,
        tenant_id: str,
        owner_id: str,
        declared_size_bytes: int,
        declared_sha256: str,
        media_type: str,
        filename: str | None = None,
    ) -> UploadIntent: ...

    async def upload_intent(
        self,
        *,
        upload_id: str,
        tenant_id: str,
    ) -> UploadIntent:
        """Raises ``NotFoundError`` for an unknown id or another tenant's."""
        ...

    async def commit_version(
        self,
        *,
        upload_id: str,
        tenant_id: str,
        document_id: str,
        knowledge_base_id: str,
        version_id: str,
        artifact_id: str,
        content_sha256: str,
        granted_principals: tuple[str, ...] = (),
    ) -> DocumentVersion:
        """Record a version, its ACL and its outbox event in one transaction.

        Either all of it is visible or none of it is. A document the index is
        never told about is a document that silently stops being searchable.
        """
        ...

    async def document(self, *, document_id: str, tenant_id: str) -> Document:
        """Raises ``NotFoundError`` for an unknown id or another tenant's."""
        ...

    async def versions(
        self,
        *,
        document_id: str,
        tenant_id: str,
    ) -> tuple[DocumentVersion, ...]:
        """Versions oldest first."""
        ...

    async def authorized_principals(
        self,
        *,
        document_id: str,
        tenant_id: str,
    ) -> tuple[str, ...]:
        """The owner plus every principal granted access, sorted."""
        ...


__all__ = [
    "Document",
    "DocumentStore",
    "DocumentVersion",
    "UploadIntent",
    "UploadStatus",
]
