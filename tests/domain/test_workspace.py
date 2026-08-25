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
    MAX_INLINE_READ_CHARS,
    MAX_WORKSPACE_ENTRIES,
    WorkspaceManifest,
    WorkspaceOverflowError,
    describe_read_window,
    read_window,
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


class TestReadWindow:
    """The slice a long file comes back in (`read_window`).

    Written against the two ways a window can end -- the caller's `limit` and
    the context ceiling -- because the whole point of the type is that a model
    can tell them apart. A `limit` that bit means "ask for more per call"; a
    ceiling that bit means the opposite.
    """

    def test_a_short_file_comes_back_whole_and_says_nothing(self) -> None:
        # The behaviour every existing read depends on. A header on a file that
        # fits would put a sentence in front of every `pyproject.toml` in the
        # repository for no reader's benefit.
        window = read_window("one\ntwo\nthree\n")

        assert window.text == "one\ntwo\nthree\n"
        assert window.is_whole_file
        assert window.next_offset is None
        assert describe_read_window("notes.md", window) is None

    def test_a_limit_bounds_the_window_and_names_where_to_resume(self) -> None:
        window = read_window("a\nb\nc\nd\n", offset=2, limit=2)

        assert window.text == "b\nc\n"
        assert (window.first_line, window.last_line, window.total_lines) == (2, 3, 4)
        assert window.next_offset == 4
        assert not window.stopped_at_char_ceiling
        described = describe_read_window("a.txt", window)
        assert described == "a.txt: lines 2-3 of 4; pass offset=4 to continue."

    def test_the_char_ceiling_bites_before_a_generous_limit(self) -> None:
        # The reason the ceiling could not simply be replaced by a line count:
        # `limit=100000` on a generated file would spend the entire context
        # budget in one call, and the ceiling is the only thing that stops it.
        line = "x" * 1_000 + "\n"
        window = read_window(line * 100, limit=100_000)

        assert window.stopped_at_char_ceiling
        assert len(window.text) <= MAX_INLINE_READ_CHARS
        assert window.last_line < window.total_lines
        described = describe_read_window("big.log", window)
        assert described is not None
        assert "stopped at the 48000-character ceiling" in described
        assert f"offset={window.next_offset}" in described

    def test_the_window_never_ends_mid_line_unless_one_line_is_too_long(
        self,
    ) -> None:
        # Slicing by characters alone gives back a window whose edges are not
        # lines, and there is no offset a model can send to ask for "the rest"
        # of that. So the cut lands on a line boundary...
        window = read_window(("y" * 100 + "\n") * 2_000)

        assert window.text.endswith("\n")
        assert window.text.count("\n") == window.last_line

    def test_a_single_over_long_line_is_cut_and_says_that_it_was(self) -> None:
        # ...except when one line is longer than the whole ceiling: a minified
        # bundle, a one-line JSON document. Returning nothing would make the
        # file unreadable at any offset, so it is cut -- and that is the one
        # case where continuing from `next_offset` skips bytes, which is why it
        # is a separate flag with its own sentence rather than a truncation.
        window = read_window("z" * (MAX_INLINE_READ_CHARS + 500) + "\nafter\n")

        assert window.line_cut
        assert window.stopped_at_char_ceiling
        assert len(window.text) == MAX_INLINE_READ_CHARS
        assert window.withheld_chars == 501
        described = describe_read_window("bundle.js", window)
        assert described is not None
        assert "shown cut" in described
        assert "501 characters" in described
        assert "offset=2 continues" in described

    def test_a_cut_line_that_is_the_last_line_says_no_offset_reaches_it(self) -> None:
        # The whole minified-bundle case: one line, no line after it. `offset`
        # addresses lines, so the tail is genuinely unreachable -- and the
        # first version of this sentence interpolated `next_offset` anyway and
        # told the model to `pass offset=None`, a value `OFFSET_SCHEMA` rejects
        # (minimum 1, no null). Its likely sequel is the identical read again,
        # which `MAX_IDENTICAL_CALLS` ends the turn on at the third.
        window = read_window("z" * (MAX_INLINE_READ_CHARS + 500))

        assert window.line_cut
        assert window.next_offset is None
        described = describe_read_window("bundle.js", window)
        assert described is not None
        assert "offset=None" not in described
        assert "no offset reaches them" in described
        assert "500 characters" in described

    def test_following_the_offsets_reconstructs_the_file_exactly(self) -> None:
        """The property the whole window exists for, and the only one that
        catches a silent gap.

        Every other test here checks one window. This walks the chain a model
        would walk -- read, take `next_offset`, read again -- and asserts the
        pieces join back into the original bytes. Two edges were found by
        exactly this and by nothing else: a line whose text fitted the ceiling
        but whose "\n" did not came back cut, losing one character and a whole
        call to fetch it; and a window that ended on a cut line named an offset
        that skipped the rest of that line.
        """

        def walk(text: str, limit: int | None = None) -> tuple[str, int]:
            pieces: list[str] = []
            offset, windows = 1, 0
            while True:
                window = read_window(text, offset=offset, limit=limit)
                if not window.text:
                    break
                pieces.append(window.text)
                windows += 1
                assert windows < 20_000, "the offsets are not advancing"
                if window.next_offset is None:
                    break
                offset = window.next_offset
            return "".join(pieces), windows

        ceiling = MAX_INLINE_READ_CHARS
        whole = {
            "": "empty",
            "\n": "one newline",
            "\n\n\n": "blank lines only",
            "a\nb\nc": "no trailing newline",
            "a\r\nb\r\nc\r\n": "CRLF",
            "".join(f"line {n}\n" for n in range(20_000)): "many windows",
            "x" * (ceiling - 1) + "\ntail\n": "a line just under the ceiling",
            # The one that was broken: the text fits, the terminator does not.
            "x" * ceiling + "\ntail\n": "a line whose newline crosses it",
        }
        for text, why in whole.items():
            for limit in (None, 3):
                joined, _ = walk(text, limit)
                assert joined == text, f"{why} (limit={limit}) lost bytes"

        # The two cases that legitimately lose bytes do so by exactly the
        # number they announced, and by no more.
        for text in ("z" * (ceiling + 500), "z" * (ceiling + 500) + "\nafter\n"):
            joined, _ = walk(text)
            first = read_window(text)
            assert first.line_cut
            assert len(text) - len(joined) == first.withheld_chars

    def test_an_offset_past_the_end_is_answered_rather_than_clamped(self) -> None:
        # Clamping to the last line would answer a question nobody asked, and
        # the model would build on it without ever learning the file is 3 lines
        # long.
        window = read_window("a\nb\nc\n", offset=900)

        assert window.text == ""
        described = describe_read_window("a.txt", window)
        assert described == "a.txt has 3 lines; offset 900 is past the end."

    def test_a_file_without_a_trailing_newline_still_counts_its_last_line(
        self,
    ) -> None:
        window = read_window("a\nb", offset=2)

        assert window.text == "b"
        assert (window.last_line, window.total_lines) == (2, 2)
        assert window.next_offset is None
