"""Reading and writing a Task's working set (ADR-028, stage 1 PR-1.2).

The property under test is the one the whole design exists for: a node that
died half-way through writing must not be visible to the attempt that replaces
it. Every rejection is paired with the control that must still be accepted.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.application.workspace import (
    Workspace,
    WorkspaceEntryNotFoundError,
)
from agent_workbench.domain.workspace import (
    MAX_WORKSPACE_TOTAL_BYTES,
    WorkspaceOverflowError,
)

TENANT = "tenant_local"
OWNER = "user_local"


def workspace() -> Workspace:
    return Workspace(
        artifacts=InMemoryArtifactStore(),
        tenant_id=TENANT,
        principal_id=OWNER,
    )


def test_an_absent_version_reads_as_an_empty_workspace() -> None:
    # A Task that has never written anything has no manifest, and that is not
    # an error state: it is where every Task starts.
    assert asyncio.run(workspace().load(None)).names() == ()


def test_a_write_produces_a_new_version_and_leaves_the_old_readable() -> None:
    async def scenario() -> None:
        space = workspace()
        first = await space.write(None, "notes.md", b"one", media_type="text/plain")
        second = await space.write(first, "notes.md", b"two", media_type="text/plain")

        assert first != second
        assert await space.read(first, "notes.md") == b"one"
        assert await space.read(second, "notes.md") == b"two"

    asyncio.run(scenario())


def test_a_replay_sees_its_entry_version_not_the_dead_attempt() -> None:
    """The reason mutable names sit on immutable bytes.

    A node writes, dies before returning its state update, and is replaced.
    The replacement resumes from the checkpoint, which still holds the *entry*
    version -- so the half-finished write is unreachable rather than merged in.
    """

    async def scenario() -> None:
        space = workspace()
        entry = await space.write(None, "input.txt", b"given", media_type="text/plain")

        # The attempt that died: it wrote, and its update never reached the graph.
        abandoned = await space.write(
            entry, "scratch.txt", b"half", media_type="text/plain"
        )
        assert await space.read(abandoned, "scratch.txt") == b"half"

        replayed = await space.load(entry)
        assert replayed.names() == ("input.txt",)
        with pytest.raises(WorkspaceEntryNotFoundError):
            await space.read(entry, "scratch.txt")

    asyncio.run(scenario())


def test_the_next_node_does_see_a_committed_write() -> None:
    # Control for the test above. Without it, a service that always returned an
    # empty workspace would pass that one.
    async def scenario() -> None:
        space = workspace()
        entry = await space.write(None, "a.md", b"x", media_type="text/markdown")
        committed = await space.write(entry, "b.md", b"y", media_type="text/markdown")

        assert (await space.load(committed)).names() == ("a.md", "b.md")

    asyncio.run(scenario())


def test_reading_a_name_that_is_not_there_is_an_error_not_an_empty_read() -> None:
    async def scenario() -> None:
        space = workspace()
        version = await space.write(None, "a.md", b"x", media_type="text/markdown")

        with pytest.raises(WorkspaceEntryNotFoundError):
            await space.read(version, "missing.md")

        assert await space.read(version, "a.md") == b"x"

    asyncio.run(scenario())


def test_an_oversized_write_is_refused_and_changes_nothing() -> None:
    async def scenario() -> None:
        space = workspace()
        version = await space.write(None, "a.md", b"x", media_type="text/markdown")

        with pytest.raises(WorkspaceOverflowError):
            await space.write(
                version,
                "huge.bin",
                b"z" * (MAX_WORKSPACE_TOTAL_BYTES + 1),
                media_type="application/octet-stream",
            )

        # The refused write left no trace: same version, same contents.
        assert (await space.load(version)).names() == ("a.md",)
        assert await space.read(version, "a.md") == b"x"

    asyncio.run(scenario())


def test_a_name_that_is_a_path_is_refused() -> None:
    async def scenario() -> None:
        space = workspace()

        with pytest.raises(ValueError):
            await space.write(None, "../escape", b"x", media_type="text/plain")

        version = await space.write(None, "escape.md", b"x", media_type="text/plain")
        assert (await space.load(version)).names() == ("escape.md",)

    asyncio.run(scenario())


def test_listing_reports_size_and_media_type() -> None:
    async def scenario() -> None:
        space = workspace()
        version = await space.write(None, "a.csv", b"1,2,3", media_type="text/csv")

        listed = await space.list(version)

        assert [(i.name, i.size_bytes, i.media_type) for i in listed] == [
            ("a.csv", 5, "text/csv")
        ]

    asyncio.run(scenario())
