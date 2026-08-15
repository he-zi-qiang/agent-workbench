"""A working set whose version outlives the run that advanced it.

A Task's workspace version is carried by the graph: a node is handed the
version pinned at its entry and publishes a new one only by returning a state
update, so a node that dies publishes nothing and its writes become unreachable
-- still stored, but named by no manifest anybody holds. That is a deliberate
property, and ``application/workspace.py`` says why.

A Code session cannot have it. Its turn is one process doing one thing, and
when the turn is cancelled or the process dies there is no state update to
withhold and no replacement attempt to hand an entry version to. If the version
lived only in the run, a cancelled turn would take every file it wrote with it:
the user would watch three files being written, cancel on the fourth, and find
an empty workspace -- the bytes still stored and permanently unreachable.

So the pointer is written through, per successful write, to the session row.
The cost is the mirror image of the Task property and is not an oversight: a
cancelled Code turn leaves its partial work in place, because "what the files
are" is what the user was building, not a transaction that either commits whole
or not at all.

Written through *with* a compare-and-set, not blindly. Two runs on one session
would otherwise each build a manifest from the version it read and each store
it; the last to finish would leave the session pointing at a manifest that
names only its own files, and the other run's files would be gone -- not
deleted, but unreachable, which for a user is the same thing. The comparison
turns that silent loss into a refusal the loser can see.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.application.workspace import (
    Workspace,
    WorkspaceListing,
)
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.workspace import WorkspaceManifest
from agent_workbench.ports.conversation_store import ConversationStore


@dataclass(frozen=True, slots=True)
class SessionWorkspace:
    """One session's working set, with the version pointer persisted.

    Reads pass straight through: a version is a version, and where it is stored
    changes nothing about what it names. Only the two writes differ, and they
    differ by one step -- record the version they are about to return.
    """

    workspace: Workspace
    conversations: ConversationStore
    session_id: str
    tenant_id: str
    principal_id: str

    async def load(self, version: Identifier | None) -> WorkspaceManifest:
        return await self.workspace.load(version)

    async def list(self, version: Identifier | None) -> tuple[WorkspaceListing, ...]:
        return await self.workspace.list(version)

    async def read(self, version: Identifier | None, name: str) -> bytes:
        return await self.workspace.read(version, name)

    async def write(
        self,
        version: Identifier | None,
        name: str,
        content: bytes,
        *,
        media_type: str,
    ) -> Identifier:
        return await self._advance(
            version,
            await self.workspace.write(version, name, content, media_type=media_type),
        )

    async def write_ref(
        self,
        version: Identifier | None,
        name: str,
        ref: ArtifactRef,
    ) -> Identifier:
        return await self._advance(
            version,
            await self.workspace.write_ref(version, name, ref),
        )

    async def _advance(
        self,
        expected: Identifier | None,
        next_version: Identifier,
    ) -> Identifier:
        """Record ``next_version``, or refuse and leave the pointer alone.

        The manifest is already stored when this runs, and a refusal does not
        remove it. That is the same unreferenced-artifact cost ADR-028 §3.2
        records for every refused write, and it is why the pointer -- not the
        manifest -- is the thing being contended: bytes are cheap to leave
        behind, a wrong answer about which files exist is not.
        """

        await self.conversations.advance_workspace_version(
            session_id=self.session_id,
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            expected=expected,
            next_version=next_version,
        )
        return next_version


__all__ = ["SessionWorkspace"]
