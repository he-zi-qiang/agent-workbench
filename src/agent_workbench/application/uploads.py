"""The upload use case: declare, transfer, then verify before committing.

Bytes and metadata travel different paths on purpose. A document can be
hundreds of megabytes; a control request describing one is a few hundred bytes.
Putting the first through the second is how a control plane acquires a memory
limit it cannot enforce, so the intent is recorded first, the bytes go to the
artifact store, and completion reconciles the two.

Completion trusts neither side. It reads the stored object's own size and
digest and compares them with what the client declared before any transfer
happened. A transfer that delivered different bytes, or a client that declared
a digest it did not have, fails here rather than becoming a document version
that the index will faithfully reproduce.

What the caller never supplies is a location. Object keys come from the store,
which is the difference between "upload a document" and "write to this path".

Nor does the caller supply an identity. Every call takes the principal the
interface layer resolved, and passes it down: a tenant says whose database this
is, not who is asking, and an upload id that authorized its own completion
would be a bearer token printed in every log line that mentions it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.errors import AgentWorkbenchError, ErrorCode
from agent_workbench.domain.identifiers import new_id
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.documents import DocumentStore, DocumentVersion, UploadIntent

UPLOAD_ID_PREFIX = "upl"
VERSION_ID_PREFIX = "ver"


class UploadVerificationError(AgentWorkbenchError):
    """The stored object does not match what the upload declared."""

    code: ClassVar[ErrorCode] = "invalid_tool_input"


@dataclass(frozen=True, slots=True)
class UploadService:
    """Turns a declared upload plus stored bytes into a document version."""

    documents: DocumentStore
    artifacts: ArtifactStore

    async def create_upload(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        declared_size_bytes: int,
        declared_sha256: str,
        media_type: str,
        filename: str | None = None,
        upload_id: str | None = None,
    ) -> UploadIntent:
        """Record what is about to be transferred, before it is."""

        return await self.documents.create_upload(
            upload_id=upload_id or new_id(UPLOAD_ID_PREFIX),
            tenant_id=tenant_id,
            owner_id=owner_id,
            declared_size_bytes=declared_size_bytes,
            declared_sha256=declared_sha256,
            media_type=media_type,
            filename=filename,
        )

    async def complete_upload(
        self,
        *,
        upload_id: str,
        tenant_id: str,
        principal_id: str,
        artifact_id: str,
        document_id: str,
        knowledge_base_id: str,
        granted_principals: tuple[str, ...] = (),
        version_id: str | None = None,
    ) -> DocumentVersion:
        """Verify the transferred object, then commit the version and its event."""

        # Reads the caller's own upload, so someone else's is refused before
        # the artifact is even looked at.
        intent = await self.documents.upload_intent(
            upload_id=upload_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        # head() is tenant-scoped, so an artifact belonging to someone else is
        # not found rather than mismatched: the difference would confirm it
        # exists.
        stored = await self.artifacts.head(
            tenant_id=tenant_id,
            artifact_id=artifact_id,
        )
        self._verify(intent, stored)

        return await self.documents.commit_version(
            upload_id=upload_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            document_id=document_id,
            knowledge_base_id=knowledge_base_id,
            version_id=version_id or new_id(VERSION_ID_PREFIX),
            artifact_id=artifact_id,
            content_sha256=stored.sha256,
            granted_principals=granted_principals,
        )

    @staticmethod
    def _verify(intent: UploadIntent, stored: ArtifactRef) -> None:
        if stored.size_bytes != intent.declared_size_bytes:
            raise UploadVerificationError(
                "the stored object's size does not match the declared upload"
            )
        if stored.sha256.lower() != intent.declared_sha256.lower():
            raise UploadVerificationError(
                "the stored object's digest does not match the declared upload"
            )


__all__ = [
    "UPLOAD_ID_PREFIX",
    "VERSION_ID_PREFIX",
    "UploadService",
    "UploadVerificationError",
]
