"""A session's working set survives the run that wrote it, and only in step.

Two properties, and they pull in opposite directions on purpose.

The first is that a cancelled turn keeps its files. A Task deliberately loses
them -- its version rides in graph state, so an attempt that dies publishes
nothing and its writes stay unreachable. A Code session must not: a user who
watches three files appear and cancels on the fourth has not asked for the
three to vanish. So the pointer is written through per write, and the test that
pins it is paired with one that pins the Task behaviour still being the other
way. Without that pair, "the session keeps its files" could as easily be a
statement that every workspace now does.

The second is that writing through is compare-and-set. Two runs on one session
would otherwise each build a manifest from the version it read and store it,
and whichever finished last would leave the session naming only its own files.
Nothing is deleted in that story, which is why it needs a test: the bytes are
all still there, and every one of the loser's files is unreachable anyway.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_workbench.adapters.memory import (
    InMemoryArtifactStore,
    InMemoryConversationStore,
)
from agent_workbench.application.session_workspace import SessionWorkspace
from agent_workbench.application.workspace import Workspace, WorkspaceSession
from agent_workbench.ports.conversation_store import WorkspacePointerConflictError

TENANT = "tenant_a"
OWNER = "user_1"
SESSION = "ses_code_1"


def _run(scenario: Any) -> Any:
    return asyncio.run(scenario())


async def _session_workspace() -> tuple[SessionWorkspace, InMemoryConversationStore]:
    conversations = InMemoryConversationStore()
    await conversations.create_session(
        session_id=SESSION, tenant_id=TENANT, owner_id=OWNER, mode="code"
    )
    workspace = SessionWorkspace(
        workspace=Workspace(
            artifacts=InMemoryArtifactStore(),
            tenant_id=TENANT,
            principal_id=OWNER,
        ),
        conversations=conversations,
        session_id=SESSION,
        tenant_id=TENANT,
        principal_id=OWNER,
    )
    return workspace, conversations


async def _pointer(conversations: InMemoryConversationStore) -> str | None:
    session = await conversations.session(
        session_id=SESSION, tenant_id=TENANT, principal_id=OWNER
    )
    return session.workspace_version


def test_a_write_records_the_version_it_returns() -> None:
    """The control for the refusal below: an uncontended write goes through."""

    async def scenario() -> tuple[str, str | None]:
        workspace, conversations = await _session_workspace()
        version = await workspace.write(
            None, "notes.md", b"first", media_type="text/plain"
        )
        return version, await _pointer(conversations)

    version, stored = _run(scenario)

    assert stored == version


def test_a_write_against_a_version_the_session_left_behind_is_refused() -> None:
    """And the pointer keeps the value the other writer put there.

    The refusal alone would not settle it: an implementation that moved the
    pointer and then noticed the mismatch would raise this same error while
    having already overwritten the other run's manifest.
    """

    async def scenario() -> tuple[str | None, str]:
        workspace, conversations = await _session_workspace()
        first = await workspace.write(
            None, "notes.md", b"first", media_type="text/plain"
        )
        # Another run on the same session, finishing while this one was still
        # holding `first`.
        await conversations.advance_workspace_version(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            expected=first,
            next_version="art_from_the_other_run",
        )
        with pytest.raises(WorkspacePointerConflictError):
            await workspace.write(
                first, "notes.md", b"second", media_type="text/plain"
            )
        return await _pointer(conversations), first

    stored, first = _run(scenario)

    assert stored == "art_from_the_other_run"
    assert stored != first


def test_a_cancelled_turn_keeps_the_files_it_wrote() -> None:
    """The next turn starts from the pointer, not from where this one began."""

    async def scenario() -> tuple[str, ...]:
        workspace, conversations = await _session_workspace()
        turn = WorkspaceSession(workspace=workspace)
        # Two writes, assigned back exactly the way the tool handlers do it.
        turn.version = await workspace.write(
            turn.version, "notes.md", b"one", media_type="text/plain"
        )
        turn.version = await workspace.write(
            turn.version, "plan.md", b"two", media_type="text/plain"
        )

        # The turn is cancelled: the object holding the version is simply gone,
        # and nothing hands its version to anybody.
        del turn

        resumed = WorkspaceSession(
            workspace=workspace, version=await _pointer(conversations)
        )
        listing = await workspace.list(resumed.version)
        return tuple(entry.name for entry in listing)

    assert _run(scenario) == ("notes.md", "plan.md")


def test_a_task_still_loses_the_writes_of_an_attempt_that_died() -> None:
    """The control, and it is the point: this is not a change to every workspace.

    A graph node publishes its version by returning a state update. The attempt
    that replaces a dead one re-reads the version pinned at entry, so the files
    the dead attempt wrote are still stored and named by no manifest it holds.
    Pinning that here is what keeps the test above a statement about sessions
    rather than about workspaces in general.
    """

    async def scenario() -> tuple[str, ...]:
        workspace = Workspace(
            artifacts=InMemoryArtifactStore(),
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        entry_version = None
        attempt = WorkspaceSession(workspace=workspace, version=entry_version)
        attempt.version = await workspace.write(
            attempt.version, "notes.md", b"one", media_type="text/plain"
        )
        attempt.version = await workspace.write(
            attempt.version, "plan.md", b"two", media_type="text/plain"
        )

        # The node dies before returning a state update, so the retry is handed
        # the version its predecessor entered with.
        del attempt

        retry = WorkspaceSession(workspace=workspace, version=entry_version)
        listing = await workspace.list(retry.version)
        return tuple(entry.name for entry in listing)

    assert _run(scenario) == ()
