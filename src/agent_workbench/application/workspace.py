"""Reading and writing one Task's working set (ADR-028).

A workspace version is an artifact id: the id of the stored manifest. That is
what lets a checkpoint hold the whole working set in one identifier, and it is
why this service takes a version *in* and returns a version *out* rather than
holding one.

The replay property falls out of that rather than being enforced here. A graph
node receives the version pinned at its entry and only commits a new one by
returning a state update; a node that dies before returning commits nothing, so
the attempt that replaces it reads the same entry version and cannot see the
half-finished writes. The bytes it wrote are still in the store -- nothing is
deleted -- they are simply unreachable, because no manifest anybody holds names
them.

Ownership is not negotiable here either. Every write records the Task's
principal as owner and every read passes it, so a workspace cannot be used to
launder bytes into another tenant or to read an artifact this principal could
not otherwise open.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.workspace import WorkspaceManifest
from agent_workbench.ports.artifact_store import ArtifactStore

#: The manifest is JSON and small by construction: it holds references, never
#: content. The ceiling is here so a corrupted or hostile id cannot be turned
#: into an unbounded read.
MANIFEST_MEDIA_TYPE = "application/json"
MANIFEST_FILENAME = "workspace.json"


class WorkspaceEntryNotFoundError(KeyError):
    """A name that the manifest at this version does not bind.

    Distinct from an empty read: a tool that answered "" for a missing file
    would let a model build on something that was never there, and the mistake
    would surface much later as content that quietly lacks a source.
    """


@dataclass(frozen=True, slots=True)
class WorkspaceListing:
    """One row of what a workspace holds, without its bytes."""

    name: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True, slots=True)
class TaskWorkspace:
    """The working set of one Task, addressed by manifest version."""

    artifacts: ArtifactStore
    tenant_id: str
    principal_id: str

    async def load(self, version: Identifier | None) -> WorkspaceManifest:
        """The manifest at ``version``; an empty one when there is none yet.

        ``None`` is where every Task starts and is not an error: a Task that has
        not written anything has no manifest to point at.
        """

        if version is None:
            return WorkspaceManifest()
        raw = await self.artifacts.get(
            tenant_id=self.tenant_id,
            artifact_id=version,
            principal_id=self.principal_id,
        )
        return WorkspaceManifest.model_validate_json(raw)

    async def list(self, version: Identifier | None) -> tuple[WorkspaceListing, ...]:
        manifest = await self.load(version)
        return tuple(
            WorkspaceListing(
                name=name,
                size_bytes=manifest.entries[name].size_bytes,
                media_type=manifest.entries[name].media_type,
            )
            for name in manifest.names()
        )

    async def read(self, version: Identifier | None, name: str) -> bytes:
        manifest = await self.load(version)
        entry = manifest.entries.get(name)
        if entry is None:
            raise WorkspaceEntryNotFoundError(name)
        return await self.artifacts.get(
            tenant_id=self.tenant_id,
            artifact_id=entry.artifact_id,
            principal_id=self.principal_id,
        )

    async def write(
        self,
        version: Identifier | None,
        name: str,
        content: bytes,
        *,
        media_type: str,
    ) -> Identifier:
        """Store ``content`` under ``name`` and return the next version.

        The order matters. Bytes are stored first, then the manifest is built --
        and the manifest constructor is what enforces the name and the ceilings,
        so a refused write has already stored bytes that no manifest names.
        Those are unreferenced, exactly like the ones a dead attempt leaves, and
        they are the same known cost recorded in ADR-028 §3.2: this repository
        has no artifact GC yet, and pretending otherwise would be worse than
        writing it down.

        The alternative -- validate the name before storing -- would only narrow
        the window, because the byte ceiling cannot be checked before the bytes
        exist. Doing both is the eventual answer; doing the cheap half and
        calling it closed is not.
        """

        manifest = await self.load(version)
        stored = await self.artifacts.put(
            tenant_id=self.tenant_id,
            owner_id=self.principal_id,
            kind="workspace",
            media_type=media_type,
            content=content,
            filename=name,
        )
        return await self._commit(manifest.with_entry(name, stored))

    async def write_ref(
        self,
        version: Identifier | None,
        name: str,
        ref: ArtifactRef,
    ) -> Identifier:
        """Bind ``name`` to bytes that are already stored.

        For a producer that wrote to the store on its own -- a downloaded file,
        an MCP tool result -- so the bytes are not copied a second time just to
        acquire a name.
        """

        manifest = await self.load(version)
        return await self._commit(manifest.with_entry(name, ref))

    async def _commit(self, manifest: WorkspaceManifest) -> Identifier:
        ref = await self.artifacts.put(
            tenant_id=self.tenant_id,
            owner_id=self.principal_id,
            kind="workspace",
            media_type=MANIFEST_MEDIA_TYPE,
            content=manifest.model_dump_json().encode("utf-8"),
            filename=MANIFEST_FILENAME,
        )
        return ref.artifact_id


__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_MEDIA_TYPE",
    "TaskWorkspace",
    "WorkspaceEntryNotFoundError",
    "WorkspaceListing",
]
