"""What the lexical half of the project-path sandbox must refuse.

Organised by *what the attacker is trying*, not by which branch of the function
runs, because the branches are an implementation detail and the attacks are the
requirement. A refactor that merges two checks should not make a test
disappear.

The realpath half lives with its adapter; a symlink cannot be expressed without
a filesystem and is therefore not testable here. That split is the point of the
module docstring's "neither check is sufficient", and
``tests/adapters/test_project_files_sandbox.py`` carries the other half.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from agent_workbench.domain.project_files import (
    MAX_PATH_SEGMENTS,
    MAX_RELATIVE_PATH_BYTES,
    MAX_SEGMENT_BYTES,
    ProjectPathError,
    is_within,
    normalize_segment,
    validate_relative_path,
)


class TestTraversal:
    """Getting out of the project by naming your way out."""

    @pytest.mark.parametrize(
        "raw",
        [
            "..",
            "../etc/passwd",
            "src/../../etc/passwd",
            "src/../..",
            "a/b/../../../..",
            # The traversal is in the middle, and every other segment is real.
            "src/agent_workbench/../../../../etc/passwd",
        ],
    )
    def test_a_parent_segment_is_refused_anywhere_in_the_path(self, raw: str) -> None:
        with pytest.raises(ProjectPathError, match="'\\.\\.' segment"):
            validate_relative_path(raw)

    @pytest.mark.parametrize(
        "raw",
        ["/etc/passwd", "/", "//etc/passwd", "///"],
    )
    def test_an_absolute_path_is_refused(self, raw: str) -> None:
        # Refused as absolute rather than quietly re-rooted. Stripping the
        # leading slash and continuing would turn a request for `/etc/passwd`
        # into a request for the project's own `etc/passwd`, which is a
        # different file and a silently different answer.
        with pytest.raises(ProjectPathError, match="must be relative"):
            validate_relative_path(raw)

    @pytest.mark.parametrize("raw", ["C:/Windows", "c:/windows", "D:\\data"])
    def test_a_drive_letter_is_refused(self, raw: str) -> None:
        with pytest.raises(ProjectPathError, match="drive letter"):
            validate_relative_path(raw)

    def test_a_backslash_is_split_not_swallowed(self) -> None:
        # The hazard this encodes: if `\` stayed inside a segment, `..\..\etc`
        # would be one segment that passes every check here, and the platform
        # would then split it into the traversal that was refused above.
        assert validate_relative_path("src\\main.py") == PurePosixPath("src/main.py")
        with pytest.raises(ProjectPathError, match="'\\.\\.' segment"):
            validate_relative_path("..\\..\\etc\\passwd")


class TestSpellingOneFileTwoWays:
    """Two names for one file is how a check on one name gets bypassed."""

    @pytest.mark.parametrize("raw", [".", "./src", "src/./main.py", "a//b", "a/"])
    def test_a_redundant_segment_is_refused_not_normalised(self, raw: str) -> None:
        with pytest.raises(ProjectPathError):
            validate_relative_path(raw)

    def test_unicode_is_normalised_because_the_filesystem_normalises_it(self) -> None:
        # macOS stores NFC; a decomposed name handed to open(2) comes back
        # composed. Leaving both spellings alive means any bookkeeping this
        # project keeps beside the disk disagrees with the disk.
        decomposed = "cafe\u0301.md"
        composed = "caf\u00e9.md"
        assert decomposed != composed
        assert validate_relative_path(decomposed) == validate_relative_path(composed)
        assert str(validate_relative_path(decomposed)) == composed

    @pytest.mark.parametrize("raw", ["report.txt ", " report.txt", "report."])
    def test_trailing_space_or_dot_is_refused(self, raw: str) -> None:
        # Windows strips them, so these are one file there and two files here.
        with pytest.raises(ProjectPathError, match="space or dot"):
            validate_relative_path(raw)


class TestForgedNames:
    """Names that lie to something downstream rather than to the filesystem."""

    def test_a_nul_is_refused_first(self) -> None:
        # `open(2)` truncates at the NUL, so every check written in Python sees
        # `safe.txt` and the kernel sees something else. Asserted as its own
        # case because ordering matters: a length or segment check running
        # first would report the wrong reason for a genuinely dangerous input.
        with pytest.raises(ProjectPathError, match="NUL"):
            validate_relative_path("safe.txt\x00/../../etc/passwd")

    @pytest.mark.parametrize("raw", ["a\nb.txt", "a\rb.txt", "a\tb.txt", "\x1b[2Kx"])
    def test_control_characters_are_refused(self, raw: str) -> None:
        # Legal on POSIX. A newline in a filename forges any line-oriented
        # listing -- a diff, a `git status`, this project's own event payloads
        # -- and the escape prefix rewrites a terminal that prints it.
        with pytest.raises(ProjectPathError):
            validate_relative_path(raw)

    @pytest.mark.parametrize("raw", ["nul", "CON.txt", "com1", "LPT9.md", "aux"])
    def test_windows_reserved_names_are_refused(self, raw: str) -> None:
        # Checked on POSIX on purpose: a path that is legal here and reserved
        # there produces a repository somebody cannot check out, and the
        # failure lands on a machine nobody was looking at.
        with pytest.raises(ProjectPathError, match="reserved"):
            validate_relative_path(raw)


class TestCeilings:
    def test_segment_count_is_capped(self) -> None:
        assert validate_relative_path("/".join(["a"] * MAX_PATH_SEGMENTS))
        with pytest.raises(ProjectPathError, match="segments"):
            validate_relative_path("/".join(["a"] * (MAX_PATH_SEGMENTS + 1)))

    def test_segment_length_is_capped_in_bytes_not_characters(self) -> None:
        # The filesystem counts bytes. A 200-character CJK name is 600 bytes and
        # fails at the syscall, so counting characters here would pass something
        # that cannot be written.
        assert validate_relative_path("a" * MAX_SEGMENT_BYTES)
        with pytest.raises(ProjectPathError, match="segment"):
            validate_relative_path("a" * (MAX_SEGMENT_BYTES + 1))
        with pytest.raises(ProjectPathError, match="segment"):
            validate_relative_path("上" * ((MAX_SEGMENT_BYTES // 3) + 1))

    def test_total_length_is_capped(self) -> None:
        with pytest.raises(ProjectPathError, match="bytes"):
            validate_relative_path("a/" * MAX_RELATIVE_PATH_BYTES)

    def test_empty_is_refused(self) -> None:
        with pytest.raises(ProjectPathError, match="empty"):
            validate_relative_path("")


class TestWhatMustStillWork:
    """The sandbox is worthless if it refuses the paths a project is made of."""

    @pytest.mark.parametrize(
        "raw",
        [
            "README.md",
            "src/agent_workbench/domain/project_files.py",
            ".gitignore",
            ".claude/settings.json",
            "docs/adr/0072-a-project-is-a-directory.md",
            "web/src/app/AppShell.tsx",
            "文档/设计稿.md",
            "a-b_c.1.tar.gz",
        ],
    )
    def test_ordinary_paths_pass(self, raw: str) -> None:
        assert str(validate_relative_path(raw)) == raw

    def test_a_dotfile_is_not_a_dot_segment(self) -> None:
        # `.` is refused and `.gitignore` is not: the forbidden thing is the
        # segment that *is* a dot, not the character.
        assert validate_relative_path(".gitignore") == PurePosixPath(".gitignore")
        assert validate_relative_path("..gitignore") == PurePosixPath("..gitignore")


class TestContainment:
    def test_a_name_prefix_is_not_containment(self) -> None:
        # The bug this exists to prevent: `str.startswith` says alpha-secrets is
        # inside alpha, and they are two projects.
        root = PurePosixPath("/srv/projects/alpha")
        assert not is_within(root, PurePosixPath("/srv/projects/alpha-secrets/x"))
        assert is_within(root, PurePosixPath("/srv/projects/alpha/x"))

    def test_the_root_contains_itself(self) -> None:
        root = PurePosixPath("/srv/projects/alpha")
        assert is_within(root, root)

    def test_a_parent_is_not_inside_its_child(self) -> None:
        assert not is_within(
            PurePosixPath("/srv/projects/alpha"), PurePosixPath("/srv/projects")
        )

    @pytest.mark.parametrize(
        ("root", "candidate"),
        [
            ("relative/root", "/abs/candidate"),
            ("/abs/root", "relative/candidate"),
        ],
    )
    def test_relative_arguments_are_refused(self, root: str, candidate: str) -> None:
        # Answering at all on an unresolved pair is the symlink hole. Refusing
        # is what forces the caller to have called realpath first.
        with pytest.raises(ProjectPathError, match="absolute"):
            is_within(PurePosixPath(root), PurePosixPath(candidate))


def test_normalize_segment_is_idempotent() -> None:
    for raw in ["a", "caf\u00e9", "cafe\u0301", "\u4e0a"]:
        once = normalize_segment(raw)
        assert normalize_segment(once) == once
