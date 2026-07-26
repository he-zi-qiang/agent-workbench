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

That metadata now records an owner. Reads answer identically for "not yours",
"not your tenant's" and "not there" -- the same constant message, with no id in
it. Distinguishing them would confirm that an object somebody guessed at
exists, which is the whole of what an id-guessing probe is trying to learn.

The owner sits in the store's own envelope rather than inside the serialized
``ArtifactRef``, because the reference travels in messages and events and must
stay a pointer. An envelope this store does not recognise is treated as
missing: metadata written before ownership existed cannot say who may read it,
and the safe reading of "no answer" is no.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

from agent_workbench.domain.artifacts import ArtifactKind, ArtifactRef
from agent_workbench.domain.errors import NotFoundError, OutputTooLargeError
from agent_workbench.domain.identifiers import new_artifact_id
from agent_workbench.ports.artifact_store import DEFAULT_CHUNK_BYTES

METADATA_FORMAT = 2

BLOB_SUFFIX = ".bin"
METADATA_SUFFIX = ".json"
QUARANTINE_SUFFIX = ".part"


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
        owner_id: str,
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
        self._write_metadata(tenant_id, artifact_id, owner_id, reference)
        return reference

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
        """Write chunks straight to disk, hashing and counting as they land.

        Nothing is published under its final name until the whole stream has
        arrived, so a transfer that fails or overruns leaves a quarantine file
        rather than a half-written artifact somebody could read.

        The writes are ordinary blocking file calls. That is acceptable for a
        local development store and is not what a deployment should run: the
        bounded executor that keeps blocking adapters off the event loop
        belongs to the coordination work package, and the object store this
        stands in for is async to begin with.
        """

        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")

        artifact_id = new_artifact_id()
        directory = self._tenant_directory(tenant_id)
        directory.mkdir(parents=True, exist_ok=True)
        quarantine = self._contained(
            directory / f"{artifact_id}{QUARANTINE_SUFFIX}",
        )

        digest = hashlib.sha256()
        size = 0
        try:
            with quarantine.open("wb") as handle:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise OutputTooLargeError(
                            f"the upload exceeds the {max_bytes} byte ceiling"
                        )
                    digest.update(chunk)
                    handle.write(chunk)

            reference = ArtifactRef(
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                kind=kind,
                media_type=media_type,
                size_bytes=size,
                sha256=digest.hexdigest(),
                filename=filename,
            )
            quarantine.replace(self._blob_path(tenant_id, artifact_id))
            self._write_metadata(tenant_id, artifact_id, owner_id, reference)
            return reference
        finally:
            quarantine.unlink(missing_ok=True)

    async def get(
        self, *, tenant_id: str, artifact_id: str, principal_id: str
    ) -> bytes:
        # Authorized from the metadata before the blob is opened, so an
        # unauthorized read never touches the bytes it was refused.
        self._authorized(tenant_id, artifact_id, principal_id)
        path = self._blob_path(tenant_id, artifact_id)
        if not path.is_file():
            raise NotFoundError("artifact not found")
        return path.read_bytes()

    async def head(
        self, *, tenant_id: str, artifact_id: str, principal_id: str
    ) -> ArtifactRef:
        return self._authorized(tenant_id, artifact_id, principal_id)

    def iter_chunks(
        self,
        *,
        tenant_id: str,
        artifact_id: str,
        principal_id: str,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> AsyncIterator[bytes]:
        """Read the blob a piece at a time.

        Authorization happens here, not inside the generator: a caller gets its
        refusal when it asks, rather than after whatever it was building around
        the answer has already committed to succeeding.
        """

        if chunk_bytes < 1:
            raise ValueError("chunk_bytes must be positive")
        self._authorized(tenant_id, artifact_id, principal_id)
        path = self._blob_path(tenant_id, artifact_id)
        if not path.is_file():
            raise NotFoundError("artifact not found")
        return self._read(path, chunk_bytes)

    @staticmethod
    async def _read(path: Path, chunk_bytes: int) -> AsyncIterator[bytes]:
        """Blocking reads, as everywhere else in this store.

        Acceptable for a local development store and not what a deployment
        should run; the bounded executor that keeps blocking adapters off the
        event loop belongs to the coordination work package, and the object
        store this stands in for is async to begin with.
        """

        with path.open("rb") as handle:
            while chunk := handle.read(chunk_bytes):
                yield chunk

    def _write_metadata(
        self,
        tenant_id: str,
        artifact_id: str,
        owner_id: str,
        reference: ArtifactRef,
    ) -> None:
        self._metadata_path(tenant_id, artifact_id).write_text(
            json.dumps(
                {
                    "format": METADATA_FORMAT,
                    "owner_id": owner_id,
                    "reference": reference.model_dump(mode="json"),
                }
            ),
            encoding="utf-8",
        )

    def _authorized(
        self, tenant_id: str, artifact_id: str, principal_id: str
    ) -> ArtifactRef:
        """The reference, if this principal stored it. Otherwise not found."""

        path = self._metadata_path(tenant_id, artifact_id)
        if not path.is_file():
            raise NotFoundError("artifact not found")
        envelope: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict):
            raise NotFoundError("artifact not found")
        stored = cast("dict[str, object]", envelope)
        if (
            stored.get("format") != METADATA_FORMAT
            or stored.get("owner_id") != principal_id
        ):
            raise NotFoundError("artifact not found")
        return ArtifactRef.model_validate(stored["reference"])

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


__all__ = [
    "BLOB_SUFFIX",
    "METADATA_FORMAT",
    "METADATA_SUFFIX",
    "QUARANTINE_SUFFIX",
    "LocalArtifactStore",
]
