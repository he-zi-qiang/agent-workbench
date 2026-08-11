"""The workspace manifest (ADR-028, stage 1 PR-1.1).

Every rejection here is paired with the control that must still be accepted. A
test that only asserts "this name is refused" cannot tell a working validator
from one that refuses everything.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.workspace import (
    MAX_WORKSPACE_ENTRIES,
    WorkspaceManifest,
    WorkspaceOverflowError,
)


def ref(name: str = "a.md", size: int = 10) -> ArtifactRef:
    return ArtifactRef(
        artifact_id="art_" + "0" * 32,
        tenant_id="tenant_local",
        kind="workspace",
        media_type="text/markdown",
        size_bytes=size,
        sha256="a" * 64,
        filename=name,
    )


def test_an_empty_workspace_is_a_valid_starting_point() -> None:
    manifest = WorkspaceManifest()

    assert manifest.entries == {}
    assert manifest.total_bytes == 0
    assert manifest.names() == ()


def test_a_name_may_not_be_a_path() -> None:
    # ArtifactRef refuses to carry a path for exactly this reason; the
    # workspace must not reintroduce one at the name layer.
    for rejected in ("a/b.md", "../secret", "/etc/passwd", "a\\b", "", "."):
        with pytest.raises(ValidationError):
            WorkspaceManifest(entries={rejected: ref()})

    # Control: ordinary flat names, including the shapes a model actually
    # produces when it wants hierarchy without directories.
    for accepted in ("notes.md", "draft-v2.md", "data_1.csv", "a.b.c.json"):
        manifest = WorkspaceManifest(entries={accepted: ref()})
        assert manifest.names() == (accepted,)


def test_with_entry_refuses_every_name_the_constructor_refuses() -> None:
    """The gap the test above left, and what fell through it.

    That one builds a manifest directly; nothing in production does. Every
    write goes through ``with_entry``, which used to end in ``model_copy`` --
    and ``model_copy`` does not run validators. So a name the constructor
    rejects was accepted here, serialized, and committed, and the workspace
    only failed on the *next* read.

    Measured on this machine: a work node wrote ``季度总结.docx``, the tool
    reported success, and from then on every list, read, grep and write in
    that Task failed -- including writes of perfectly legal names, because a
    write loads the manifest first. The Task could not recover, and the error
    named a call that had already returned.
    """

    rejected_names = (
        "a/b.md",
        "../secret",
        "/etc/passwd",
        "a\\b",
        "",
        ".",
        "季度.docx",
    )
    for rejected in rejected_names:
        with pytest.raises(ValidationError):
            WorkspaceManifest().with_entry(rejected, ref())

    # Control: the same names the constructor accepts, accepted here too.
    for accepted in ("notes.md", "draft-v2.md", "data_1.csv", "a.b.c.json"):
        assert WorkspaceManifest().with_entry(accepted, ref()).names() == (accepted,)


def test_a_rejected_write_leaves_the_previous_version_untouched() -> None:
    """Refusing has to be inert, or a failed write is a corrupted workspace.

    The manifest a caller already holds is the one its node keeps working
    from, so a refusal that mutated it in passing would poison the version
    that was fine.
    """

    before = WorkspaceManifest().with_entry("kept.md", ref())

    with pytest.raises(ValidationError):
        before.with_entry("季度总结.docx", ref())

    assert before.names() == ("kept.md",)
    # And it still round-trips, which is the property the poisoned one lost.
    assert WorkspaceManifest.model_validate_json(before.model_dump_json()) == before


def test_names_come_back_sorted_so_a_listing_is_reproducible() -> None:
    manifest = WorkspaceManifest(entries={"c.md": ref(), "a.md": ref(), "b.md": ref()})

    assert manifest.names() == ("a.md", "b.md", "c.md")


def test_writing_a_name_replaces_it_and_leaves_the_old_manifest_alone() -> None:
    first = WorkspaceManifest().with_entry("notes.md", ref(size=10))
    second = first.with_entry("notes.md", ref(size=99))

    assert first.entries["notes.md"].size_bytes == 10
    assert second.entries["notes.md"].size_bytes == 99
    assert first.total_bytes == 10
    assert second.total_bytes == 99


def test_the_entry_count_is_bounded() -> None:
    manifest = WorkspaceManifest(
        entries={f"f{index}.md": ref() for index in range(MAX_WORKSPACE_ENTRIES)}
    )

    # Control: replacing an existing name at the ceiling is not growth.
    assert manifest.with_entry("f0.md", ref(size=5)).total_bytes

    with pytest.raises(WorkspaceOverflowError):
        manifest.with_entry("one-too-many.md", ref())


def test_the_total_byte_budget_is_enforced_on_write() -> None:
    small = WorkspaceManifest().with_entry("a.md", ref(size=1))

    with pytest.raises(WorkspaceOverflowError):
        small.with_entry("b.md", ref(size=10**12))

    # Control: the same write under the ceiling succeeds, and the rejected one
    # left nothing behind.
    assert small.with_entry("b.md", ref(size=2)).total_bytes == 3
    assert small.names() == ("a.md",)


def test_replacing_a_large_entry_with_a_small_one_frees_its_bytes() -> None:
    # Without this the budget would ratchet: a workspace that once held a big
    # file could never be written to again even after replacing it.
    manifest = WorkspaceManifest().with_entry("big.bin", ref(size=1_000_000))
    replaced = manifest.with_entry("big.bin", ref(size=1))

    assert replaced.total_bytes == 1
