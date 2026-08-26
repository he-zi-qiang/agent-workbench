"""The ``ProjectFileStore`` contract, against a real directory.

One implementation today, so this is not yet parameterised the way
``tests/contracts`` parameterises the two conversation stores. It is written as
if it will be: every test goes through the port's methods and asserts on the
port's DTOs, and nothing here reaches for ``Path`` except to *set up* the
fixture or to check what the store actually did to the disk.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_workbench.adapters.filesystem.project_files import (
    FilesystemProjectFileStore,
)
from agent_workbench.adapters.filesystem.sandbox import (
    ProjectSandbox,
    ProjectSandboxError,
)
from agent_workbench.domain.errors import NotFoundError, OutputTooLargeError
from agent_workbench.domain.project_files import ProjectFileChangedError
from agent_workbench.ports.project_files import (
    MAX_LISTING_ENTRIES,
    MAX_READ_BYTES,
    ProjectFileStore,
    ProjectFileVersion,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(tmp_path: Path) -> FilesystemProjectFileStore:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')\n")
    (root / "README.md").write_text("# Project\n")
    # Two of the four always-skipped directories, with something inside each so
    # a walk that descended would be visibly wrong rather than merely slower.
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "node_modules" / "left-pad" / "index.js").write_text("module.exports={}\n")
    return FilesystemProjectFileStore(ProjectSandbox(root))


def test_the_store_satisfies_the_port(store: FilesystemProjectFileStore) -> None:
    assert isinstance(store, ProjectFileStore)


class TestListing:
    async def test_the_root_lists_directories_first_then_by_name(
        self, store: FilesystemProjectFileStore
    ) -> None:
        listing = await store.list_directory()
        assert [entry.path for entry in listing.entries] == ["src", "README.md"]
        assert listing.truncated is False

    async def test_generated_directories_are_skipped(
        self, store: FilesystemProjectFileStore
    ) -> None:
        # `.git` most of all: it holds every version of every file the project
        # ever had, including ones deleted precisely because they should not
        # have been committed.
        names = {entry.path for entry in (await store.list_directory()).entries}
        assert ".git" not in names
        assert "node_modules" not in names

    async def test_a_directory_has_no_size_rather_than_a_size_of_zero(
        self, store: FilesystemProjectFileStore
    ) -> None:
        listing = await store.list_directory()
        directory = next(e for e in listing.entries if e.kind == "directory")
        file = next(e for e in listing.entries if e.kind == "file")
        assert directory.size_bytes is None
        assert file.size_bytes == len("# Project\n")

    async def test_a_subdirectory_lists_with_paths_relative_to_the_root(
        self, store: FilesystemProjectFileStore
    ) -> None:
        # Relative to the *root*, not to the listed directory. A client that
        # received bare names would have to rejoin them itself, and the first
        # one to join them wrongly would send a path naming a different file.
        listing = await store.list_directory("src")
        assert [entry.path for entry in listing.entries] == ["src/main.py"]

    async def test_listing_something_that_is_not_a_directory_is_not_found(
        self, store: FilesystemProjectFileStore
    ) -> None:
        with pytest.raises(NotFoundError):
            await store.list_directory("README.md")

    async def test_a_listing_is_capped_and_says_so(self, tmp_path: Path) -> None:
        root = tmp_path / "big"
        root.mkdir()
        for index in range(MAX_LISTING_ENTRIES + 10):
            (root / f"file-{index:05d}.txt").write_text("x")
        listing = await FilesystemProjectFileStore(
            ProjectSandbox(root)
        ).list_directory()
        assert len(listing.entries) == MAX_LISTING_ENTRIES
        # The flag, not `len(entries) == MAX`: a caller deriving it would call a
        # directory of exactly the ceiling's size truncated.
        assert listing.truncated is True

    async def test_a_full_directory_at_exactly_the_ceiling_is_not_truncated(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "exact"
        root.mkdir()
        for index in range(MAX_LISTING_ENTRIES):
            (root / f"file-{index:05d}.txt").write_text("x")
        listing = await FilesystemProjectFileStore(
            ProjectSandbox(root)
        ).list_directory()
        assert len(listing.entries) == MAX_LISTING_ENTRIES
        assert listing.truncated is False


class TestWalk:
    async def test_walk_returns_files_from_every_level(
        self, store: FilesystemProjectFileStore
    ) -> None:
        listing = await store.walk()
        assert [entry.path for entry in listing.entries] == [
            "README.md",
            "src/main.py",
        ]

    async def test_walk_does_not_descend_into_skipped_directories(
        self, store: FilesystemProjectFileStore
    ) -> None:
        paths = {entry.path for entry in (await store.walk()).entries}
        assert not any(path.startswith(".git/") for path in paths)
        assert not any(path.startswith("node_modules/") for path in paths)

    async def test_walk_returns_no_directories(
        self, store: FilesystemProjectFileStore
    ) -> None:
        # A walk answers "what files are there"; the directories are recoverable
        # from the paths, and returning both makes the ceiling count things the
        # caller did not ask about.
        assert all(entry.kind == "file" for entry in (await store.walk()).entries)


class TestRead:
    async def test_reading_text(self, store: FilesystemProjectFileStore) -> None:
        content = await store.read("src/main.py")
        assert content.is_text is True
        assert content.text == "print('hi')\n"
        assert content.size_bytes == len("print('hi')\n")

    async def test_binary_is_reported_as_such_rather_than_mangled(
        self, store: FilesystemProjectFileStore, tmp_path: Path
    ) -> None:
        # The failure this prevents: a PNG decoded with `errors="replace"` is a
        # string of U+FFFD that a model reads as text and tries to edit.
        (store.working_directory / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        content = await store.read("logo.png")
        assert content.is_text is False
        assert content.text is None
        assert content.size_bytes == 10

    async def test_an_empty_file_is_text_with_empty_text(
        self, store: FilesystemProjectFileStore
    ) -> None:
        # The case that makes `is_text` necessary rather than derivable: `text`
        # is falsy here and the file is perfectly readable.
        (store.working_directory / "empty.txt").write_bytes(b"")
        content = await store.read("empty.txt")
        assert content.is_text is True
        assert content.text == ""

    async def test_a_missing_file_is_not_found(
        self, store: FilesystemProjectFileStore
    ) -> None:
        with pytest.raises(NotFoundError):
            await store.read("nope.txt")

    async def test_a_directory_is_not_a_file(
        self, store: FilesystemProjectFileStore
    ) -> None:
        with pytest.raises(NotFoundError):
            await store.read("src")

    async def test_an_oversized_file_is_refused_not_truncated(
        self, store: FilesystemProjectFileStore
    ) -> None:
        # Truncating would hand back a source file missing its last half, which
        # to a model is indistinguishable from a file that ends there.
        (store.working_directory / "big.bin").write_bytes(b"x" * (MAX_READ_BYTES + 1))
        with pytest.raises(OutputTooLargeError):
            await store.read("big.bin")


class TestWrite:
    async def test_writing_creates_parent_directories(
        self, store: FilesystemProjectFileStore
    ) -> None:
        entry = await store.write("docs/adr/0072.md", "# ADR-072\n")
        assert entry.path == "docs/adr/0072.md"
        assert entry.kind == "file"
        assert (
            store.working_directory / "docs" / "adr" / "0072.md"
        ).read_text() == "# ADR-072\n"

    async def test_writing_replaces_whole_contents(
        self, store: FilesystemProjectFileStore
    ) -> None:
        await store.write("src/main.py", "print('bye')\n")
        assert (await store.read("src/main.py")).text == "print('bye')\n"

    async def test_bytes_round_trip(self, store: FilesystemProjectFileStore) -> None:
        await store.write("logo.png", b"\x89PNG\xff")
        assert (store.working_directory / "logo.png").read_bytes() == b"\x89PNG\xff"

    async def test_no_partial_file_is_left_when_the_write_is_refused(
        self, store: FilesystemProjectFileStore
    ) -> None:
        # The atomicity claim, checked on the failure path: the ceiling is
        # enforced before anything is written, so the target must be untouched.
        with pytest.raises(OutputTooLargeError):
            await store.write("src/main.py", "x" * (MAX_READ_BYTES + 1))
        assert (await store.read("src/main.py")).text == "print('hi')\n"

    async def test_a_write_leaves_no_temporary_behind(
        self, store: FilesystemProjectFileStore
    ) -> None:
        # The temp is consumed by `os.replace` on the success path. A leftover
        # would show up in every listing of the directory it was written to.
        await store.write("src/main.py", "print('bye')\n")
        assert [item.name for item in (store.working_directory / "src").iterdir()] == [
            "main.py"
        ]

    async def test_an_oversized_write_is_refused(
        self, store: FilesystemProjectFileStore
    ) -> None:
        # The same ceiling as reads: a store that accepts what it will refuse to
        # read back lets an agent write a file it can never see again.
        with pytest.raises(OutputTooLargeError):
            await store.write("big.txt", "x" * (MAX_READ_BYTES + 1))


class TestAConditionalWrite:
    """``if_unchanged``, the half of ADR-0078 that lives below the tools.

    The tool layer decides *whether* a write is allowed; this decides that
    nothing slipped in between that decision and the bytes landing. It is a
    precondition, not a lock: another process can still interleave, and what it
    closes is the window this process opens by stat-ing and then writing.
    """

    async def test_it_writes_when_the_file_is_as_described(
        self, store: FilesystemProjectFileStore, tmp_path: Path
    ) -> None:
        seen = await store.read("README.md")

        entry = await store.write(
            "README.md",
            "# changed\n",
            if_unchanged=ProjectFileVersion(
                size_bytes=seen.size_bytes, modified_at=seen.modified_at
            ),
        )

        assert entry.size_bytes == len("# changed\n")
        assert (tmp_path / "project" / "README.md").read_text() == "# changed\n"

    async def test_a_changed_mtime_refuses_and_writes_nothing(
        self, store: FilesystemProjectFileStore, tmp_path: Path
    ) -> None:
        seen = await store.read("README.md")
        target = tmp_path / "project" / "README.md"
        target.write_text("# somebody else got here first\n")

        with pytest.raises(ProjectFileChangedError) as refused:
            await store.write(
                "README.md",
                "# mine\n",
                if_unchanged=ProjectFileVersion(
                    size_bytes=seen.size_bytes, modified_at=seen.modified_at
                ),
            )

        # Nothing written is the assertion that matters; the message is what
        # the model reads, so both sizes and both timestamps are in it.
        assert target.read_text() == "# somebody else got here first\n"
        assert "changed after you read it" in str(refused.value)
        assert "Read it again" in str(refused.value)

    async def test_a_same_mtime_different_size_still_refuses(
        self, store: FilesystemProjectFileStore, tmp_path: Path
    ) -> None:
        """Why the precondition carries a size and not only a timestamp.

        An mtime is a good check on a filesystem that keeps nanoseconds and a
        poor one where it keeps whole seconds -- there, a user's save inside the
        same second as the read is invisible. Forced here by restoring the
        timestamp, which is what a coarse clock does for free.
        """

        seen = await store.read("README.md")
        target = tmp_path / "project" / "README.md"
        before = target.stat()
        target.write_text("# a different length entirely\n")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

        with pytest.raises(ProjectFileChangedError):
            await store.write(
                "README.md",
                "# mine\n",
                if_unchanged=ProjectFileVersion(
                    size_bytes=seen.size_bytes, modified_at=seen.modified_at
                ),
            )

        assert target.read_text() == "# a different length entirely\n"

    async def test_a_file_that_vanished_refuses_rather_than_recreating_it(
        self, store: FilesystemProjectFileStore, tmp_path: Path
    ) -> None:
        # An unconditional write would make the file again, which reads as
        # success and is a resurrection nobody asked for -- the caller's whole
        # claim was "replace the thing I read", and there is nothing to replace.
        seen = await store.read("README.md")
        (tmp_path / "project" / "README.md").unlink()

        with pytest.raises(ProjectFileChangedError) as refused:
            await store.write(
                "README.md",
                "# mine\n",
                if_unchanged=ProjectFileVersion(
                    size_bytes=seen.size_bytes, modified_at=seen.modified_at
                ),
            )

        assert "is gone" in str(refused.value)
        assert not (tmp_path / "project" / "README.md").exists()

    async def test_no_precondition_still_writes_unconditionally(
        self, store: FilesystemProjectFileStore, tmp_path: Path
    ) -> None:
        # The console's own `PUT` is a person acting on their own files and has
        # nobody to be raced by, so the default stays what it always was.
        await store.write("README.md", "# unconditional\n")

        assert (tmp_path / "project" / "README.md").read_text() == "# unconditional\n"


class TestDelete:
    async def test_deleting_reports_whether_it_was_there(
        self, store: FilesystemProjectFileStore
    ) -> None:
        assert await store.delete("README.md") is True
        assert await store.delete("README.md") is False

    async def test_a_directory_is_refused_rather_than_removed_recursively(
        self, store: FilesystemProjectFileStore
    ) -> None:
        # `rm -rf` behind a method called `delete` is how a project loses a
        # directory somebody meant to remove one file from.
        with pytest.raises(NotFoundError):
            await store.delete("src")
        assert (store.working_directory / "src" / "main.py").exists()


class TestTheSandboxIsTheOnlyWayIn:
    """Whatever the method, the path rules are the sandbox's."""

    @pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "a\x00b"])
    async def test_every_method_refuses_a_bad_path(
        self, store: FilesystemProjectFileStore, path: str
    ) -> None:
        from agent_workbench.domain.project_files import ProjectPathError

        for call in (
            store.read(path),
            store.write(path, "x"),
            store.delete(path),
            store.list_directory(path),
        ):
            with pytest.raises(ProjectPathError):
                await call

    async def test_a_symlink_out_of_the_project_is_refused_on_read(
        self, store: FilesystemProjectFileStore, tmp_path: Path
    ) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("PRIVATE\n")
        (store.working_directory / "link.txt").symlink_to(secret)
        with pytest.raises(ProjectSandboxError):
            await store.read("link.txt")

    async def test_a_listing_describes_a_symlink_as_itself(
        self, store: FilesystemProjectFileStore, tmp_path: Path
    ) -> None:
        # `lstat`, not `stat`: reporting the target's size would tell the reader
        # a file is there that is not.
        secret = tmp_path / "secret.txt"
        secret.write_text("PRIVATE CONTENTS THAT ARE LONG\n")
        (store.working_directory / "link.txt").symlink_to(secret)
        entry = next(
            e for e in (await store.list_directory()).entries if e.path == "link.txt"
        )
        assert entry.size_bytes != len("PRIVATE CONTENTS THAT ARE LONG\n")


async def test_a_cjk_path_round_trips(store: FilesystemProjectFileStore) -> None:
    # This repository's own directory is `agent工作台`.
    await store.write("文档/设计稿.md", "稿子\n")
    assert (await store.read("文档/设计稿.md")).text == "稿子\n"
    assert "文档/设计稿.md" in {e.path for e in (await store.walk()).entries}
