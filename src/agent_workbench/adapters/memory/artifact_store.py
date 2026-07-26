"""In-memory artifact store.

Object keys are generated here, never supplied by a caller, and every read is
scoped by tenant *and* principal. All three are the point of the adapter rather
than incidental: they are the behaviours the local-filesystem and
S3-compatible stores have to reproduce, and the ones a test can pin now.

The owner is held beside the reference, not inside it. An ``ArtifactRef``
travels in messages and events, and who may read the bytes is a fact about the
stored object rather than about the pointer to it.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

from agent_workbench.domain.artifacts import ArtifactKind, ArtifactRef
from agent_workbench.domain.errors import NotFoundError, OutputTooLargeError
from agent_workbench.domain.identifiers import new_artifact_id
from agent_workbench.ports.artifact_store import DEFAULT_CHUNK_BYTES


class InMemoryArtifactStore:
    """Content-addressed byte storage held in process memory."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[ArtifactRef, str, bytes]] = {}

    async def put(
        self,
        *,
        tenant_id: str,
        owner_id: str,
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
        self._objects[ref.artifact_id] = (ref, owner_id, content)
        return ref

    async def put_stream(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        kind: ArtifactKind,
        media_type: str,
        chunks: AsyncIterator[bytes],
        max_bytes: int,
        filename: str | None = None,
    ) -> ArtifactRef:
        """Accumulate the stream, refusing it the moment it overruns.

        Held in memory by definition, so the ceiling is the only thing keeping
        a sender from deciding how much of it to use.
        """

        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")

        parts: list[bytes] = []
        size = 0
        async for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise OutputTooLargeError(
                    f"the upload exceeds the {max_bytes} byte ceiling"
                )
            parts.append(chunk)

        return await self.put(
            tenant_id=tenant_id,
            owner_id=owner_id,
            kind=kind,
            media_type=media_type,
            content=b"".join(parts),
            filename=filename,
        )

    async def get(
        self, *, tenant_id: str, artifact_id: str, principal_id: str
    ) -> bytes:
        _, _, content = self._resolve(tenant_id, artifact_id, principal_id)
        return content

    async def head(
        self, *, tenant_id: str, artifact_id: str, principal_id: str
    ) -> ArtifactRef:
        ref, _, _ = self._resolve(tenant_id, artifact_id, principal_id)
        return ref

    def iter_chunks(
        self,
        *,
        tenant_id: str,
        artifact_id: str,
        principal_id: str,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> AsyncIterator[bytes]:
        """Slice what is already in memory.

        Pointless here on its own -- the object is in memory either way. It
        exists so the contract tests run against two implementations rather
        than one, which is what makes them a contract.
        """

        if chunk_bytes < 1:
            raise ValueError("chunk_bytes must be positive")
        _, _, content = self._resolve(tenant_id, artifact_id, principal_id)
        return self._slice(content, chunk_bytes)

    @staticmethod
    async def _slice(content: bytes, chunk_bytes: int) -> AsyncIterator[bytes]:
        for start in range(0, len(content), chunk_bytes):
            yield content[start : start + chunk_bytes]

    def _resolve(
        self, tenant_id: str, artifact_id: str, principal_id: str
    ) -> tuple[ArtifactRef, str, bytes]:
        stored = self._objects.get(artifact_id)
        # A wrong tenant, a wrong principal and a missing id fail identically.
        # Any difference -- message, error type or timing -- would confirm the
        # object exists.
        if (
            stored is None
            or stored[0].tenant_id != tenant_id
            or stored[1] != principal_id
        ):
            raise NotFoundError("artifact not found")
        return stored


__all__ = ["InMemoryArtifactStore"]
