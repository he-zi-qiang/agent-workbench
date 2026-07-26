"""Artifacts on the local filesystem.

This is the store a single-machine development or demo deployment uses, and it
is where the object-key rules become concrete. The caller supplies a tenant, a
kind and bytes; the store decides where those bytes live. A caller-chosen path
is how traversal and cross-tenant writes get in, so there is no way to offer
one.

Metadata sits beside the blob rather than in a table. Nothing queries artifacts
by anything but their id yet, and a table that only ever serves point lookups
would be schema pretending to be an index. When uploads arrive and artifact
rows have to be written in the same transaction as the document version they
belong to, that is when the table earns its place.

Reads are tenant-scoped and answer identically for "not yours" and "not there"
-- the same constant message, with no id in it. Distinguishing them would
confirm that another tenant's object exists, which is the whole of what an
id-guessing probe is trying to learn.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent_workbench.domain.artifacts import ArtifactKind, ArtifactRef
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import new_artifact_id

BLOB_SUFFIX = ".bin"
METADATA_SUFFIX = ".json"


class LocalArtifactStore:
    """Content-addressed artifacts under one directory tree."""

    __slots__ = ("_root",)

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    async def put(
        self,
        *,
        tenant_id: str,
        kind: ArtifactKind,
        media_type: str,
        content: bytes,
        filename: str | None = None,
    ) -> ArtifactRef:
        artifact_id = new_artifact_id()
        reference = ArtifactRef(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            kind=kind,
            media_type=media_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            filename=filename,
        )

        directory = self._tenant_directory(tenant_id)
        directory.mkdir(parents=True, exist_ok=True)
        # Metadata is written after the blob: a crash between the two leaves an
        # unreferenced blob, which is garbage, rather than a reference to bytes
        # that are not there, which is a lie.
        self._blob_path(tenant_id, artifact_id).write_bytes(content)
        self._metadata_path(tenant_id, artifact_id).write_text(
            reference.model_dump_json(),
            encoding="utf-8",
        )
        return reference

    async def get(self, *, tenant_id: str, artifact_id: str) -> bytes:
        path = self._blob_path(tenant_id, artifact_id)
        if not path.is_file():
            raise NotFoundError("artifact not found")
        return path.read_bytes()

    async def head(self, *, tenant_id: str, artifact_id: str) -> ArtifactRef:
        path = self._metadata_path(tenant_id, artifact_id)
        if not path.is_file():
            raise NotFoundError("artifact not found")
        return ArtifactRef.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _tenant_directory(self, tenant_id: str) -> Path:
        return self._contained(self._root / tenant_id)

    def _blob_path(self, tenant_id: str, artifact_id: str) -> Path:
        return self._contained(self._root / tenant_id / f"{artifact_id}{BLOB_SUFFIX}")

    def _metadata_path(self, tenant_id: str, artifact_id: str) -> Path:
        return self._contained(
            self._root / tenant_id / f"{artifact_id}{METADATA_SUFFIX}"
        )

    def _contained(self, path: Path) -> Path:
        """Refuse any path that resolves outside the store's root.

        Identifiers are already constrained, so this should be unreachable.
        That is exactly why it is here: the check costs nothing and it is the
        one that has to hold if a constraint is ever loosened upstream.
        """

        resolved = (path if path.is_absolute() else self._root / path).resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise NotFoundError("artifact path escapes the store root")
        return resolved


__all__ = ["BLOB_SUFFIX", "METADATA_SUFFIX", "LocalArtifactStore"]
