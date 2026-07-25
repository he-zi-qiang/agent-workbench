"""In-memory artifact store.

Object keys are generated here, never supplied by a caller, and every read is
scoped by tenant. Both properties are the point of the adapter rather than
incidental: they are the behaviours the local-filesystem and S3-compatible
stores will have to reproduce, and the ones a test can pin now.
"""

from __future__ import annotations

import hashlib

from agent_workbench.domain.artifacts import ArtifactKind, ArtifactRef
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import new_artifact_id


class InMemoryArtifactStore:
    """Content-addressed byte storage held in process memory."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[ArtifactRef, bytes]] = {}

    async def put(
        self,
        *,
        tenant_id: str,
        kind: ArtifactKind,
        media_type: str,
        content: bytes,
        filename: str | None = None,
    ) -> ArtifactRef:
        ref = ArtifactRef(
            artifact_id=new_artifact_id(),
            tenant_id=tenant_id,
            kind=kind,
            media_type=media_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            filename=filename,
        )
        self._objects[ref.artifact_id] = (ref, content)
        return ref

    async def get(self, *, tenant_id: str, artifact_id: str) -> bytes:
        _, content = self._resolve(tenant_id=tenant_id, artifact_id=artifact_id)
        return content

    async def head(self, *, tenant_id: str, artifact_id: str) -> ArtifactRef:
        ref, _ = self._resolve(tenant_id=tenant_id, artifact_id=artifact_id)
        return ref

    def _resolve(
        self,
        *,
        tenant_id: str,
        artifact_id: str,
    ) -> tuple[ArtifactRef, bytes]:
        stored = self._objects.get(artifact_id)
        # A wrong tenant and a missing id fail identically. Any difference --
        # message, error type or timing -- would confirm the object exists.
        if stored is None or stored[0].tenant_id != tenant_id:
            raise NotFoundError("artifact not found")
        return stored


__all__ = ["InMemoryArtifactStore"]
