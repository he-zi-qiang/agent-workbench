"""``ProjectFileStore`` over a real directory (ADR-072).

Every path this class touches goes through ``ProjectSandbox`` -- there is no
second way in, and that is the whole of its security argument. The class itself
holds no path logic: if it did, the sandbox would stop being the one place the
rules live, and the next store would have to reimplement them correctly.

The blocking work runs off the loop through ``offload``, with one exception that
is deliberate rather than overlooked. ``call_runner``'s contract admits only
work whose partial execution is unobservable, and writes qualify *because* they
are atomic (``ProjectSandbox.write_bytes`` renames into place). A write built
from ``open``-truncate-``write`` would not qualify, and would have had to stay
on the loop or block it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from agent_workbench.adapters.concurrency.call_runner import (
    BlockingCallRunner,
    offload,
)
from agent_workbench.adapters.filesystem.sandbox import ProjectSandbox
from agent_workbench.domain.errors import NotFoundError, OutputTooLargeError
from agent_workbench.ports.project_files import (
    ALWAYS_SKIPPED_DIRECTORIES,
    MAX_LISTING_ENTRIES,
    MAX_READ_BYTES,
    ProjectFileContent,
    ProjectFileEntry,
    ProjectListing,
)


def _modified_at(status: os.stat_result) -> datetime:
    """A UTC timestamp from an mtime.

    Explicitly UTC rather than naive. A naive datetime here would be serialized
    without an offset and read by a browser in its own zone -- so a file written
    at 09:00 would be shown as having been written at 17:00 to anybody eight
    hours away, with nothing in the payload to say otherwise.
    """

    return datetime.fromtimestamp(status.st_mtime, tz=UTC)


class FilesystemProjectFileStore:
    """One project's tree, on this machine's disk."""

    __slots__ = ("_runner", "_sandbox")

    def __init__(
        self,
        sandbox: ProjectSandbox,
        *,
        runner: BlockingCallRunner | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._runner = runner

    @property
    def root(self) -> Path:
        return self._sandbox.root

    def _target(self, path: str) -> Path:
        """The real path, or the root itself for the empty path.

        ``""`` is the root and is the only path the sandbox does not validate,
        because ``validate_relative_path`` requires a non-empty string. Special
        casing it here rather than loosening that requirement keeps "empty is a
        valid path" from becoming true everywhere the validator is used.
        """

        return self._sandbox.root if path == "" else self._sandbox.resolve(path)

    def _entry(self, absolute: Path, relative: str) -> ProjectFileEntry:
        # `lstat`, not `stat`: a symlink is described as what it is rather than
        # as what it points at. A listing that reported a link's target size
        # would be telling the reader a file is there that is not.
        status = absolute.lstat()
        directory = absolute.is_dir() and not absolute.is_symlink()
        return ProjectFileEntry(
            path=relative,
            kind="directory" if directory else "file",
            size_bytes=None if directory else status.st_size,
            modified_at=_modified_at(status),
        )

    async def list_directory(self, path: str = "") -> ProjectListing:
        def work() -> ProjectListing:
            target = self._target(path)
            if not target.is_dir():
                raise NotFoundError(f"not a directory: {path!r}")
            entries: list[ProjectFileEntry] = []
            truncated = False
            # Sorted before the ceiling is applied, so a truncated listing is
            # the *first* N by the order the reader sees rather than N in
            # whatever order the filesystem happened to return -- which changes
            # between calls and would make a truncated tree flicker.
            for child in sorted(target.iterdir(), key=lambda item: item.name):
                if len(entries) >= MAX_LISTING_ENTRIES:
                    truncated = True
                    break
                if child.is_dir() and child.name in ALWAYS_SKIPPED_DIRECTORIES:
                    continue
                relative = child.name if path == "" else f"{path}/{child.name}"
                entries.append(self._entry(child, relative))
            # Directories first, then by name. Sorted here rather than by the
            # caller: every caller wants this order, and one that sorted for
            # itself would have to know that `kind` is the primary key.
            entries.sort(key=lambda entry: (entry.kind != "directory", entry.path))
            return ProjectListing(
                path=path, entries=tuple(entries), truncated=truncated
            )

        return await offload(self._runner, work, name="project_files.list_directory")

    async def walk(self, path: str = "") -> ProjectListing:
        def work() -> ProjectListing:
            target = self._target(path)
            if not target.is_dir():
                raise NotFoundError(f"not a directory: {path!r}")
            root = self._sandbox.root
            entries: list[ProjectFileEntry] = []
            truncated = False
            for directory, subdirectories, filenames in os.walk(target):
                # Pruned in place, which is what `os.walk` documents as the way
                # to stop it descending. Filtering the yielded names instead
                # would still walk every file in `node_modules` first.
                subdirectories[:] = sorted(
                    name
                    for name in subdirectories
                    if name not in ALWAYS_SKIPPED_DIRECTORIES
                )
                for filename in sorted(filenames):
                    if len(entries) >= MAX_LISTING_ENTRIES:
                        truncated = True
                        break
                    absolute = Path(directory) / filename
                    entries.append(
                        self._entry(absolute, absolute.relative_to(root).as_posix())
                    )
                if truncated:
                    break
            return ProjectListing(
                path=path, entries=tuple(entries), truncated=truncated
            )

        return await offload(self._runner, work, name="project_files.walk")

    async def read(self, path: str) -> ProjectFileContent:
        def work() -> ProjectFileContent:
            target = self._sandbox.resolve(path)
            if not target.exists():
                raise NotFoundError(f"no such file: {path!r}")
            if target.is_dir():
                raise NotFoundError(f"not a file: {path!r}")
            status = target.lstat()
            if status.st_size > MAX_READ_BYTES:
                # Checked before reading, not after. Reading first would put the
                # bytes in memory to then refuse them, which is the resource
                # exhaustion the ceiling exists to prevent.
                raise OutputTooLargeError(
                    f"file is larger than {MAX_READ_BYTES} bytes: {path!r}"
                )
            payload = self._sandbox.read_bytes(path)
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                # Not `errors="replace"`. A PNG decoded with replacement is a
                # string of U+FFFD that a model will read as text and try to
                # edit, and the edit will destroy the file. Saying "this is not
                # text" is the answer that lets a caller do something correct.
                return ProjectFileContent(
                    path=path,
                    text=None,
                    size_bytes=status.st_size,
                    is_text=False,
                    modified_at=_modified_at(status),
                )
            return ProjectFileContent(
                path=path,
                text=text,
                size_bytes=status.st_size,
                is_text=True,
                modified_at=_modified_at(status),
            )

        return await offload(self._runner, work, name="project_files.read")

    async def write(self, path: str, content: str | bytes) -> ProjectFileEntry:
        def work() -> ProjectFileEntry:
            payload = content.encode("utf-8") if isinstance(content, str) else content
            if len(payload) > MAX_READ_BYTES:
                # The same ceiling as reads, on purpose: a store that accepts
                # what it will later refuse to read back is one where an agent
                # can write a file it can never see again.
                raise OutputTooLargeError(
                    f"content is larger than {MAX_READ_BYTES} bytes: {path!r}"
                )
            self._sandbox.write_bytes(path, payload)
            target = self._sandbox.resolve(path)
            return self._entry(target, path)

        return await offload(self._runner, work, name="project_files.write")

    async def delete(self, path: str) -> bool:
        def work() -> bool:
            target = self._sandbox.resolve(path)
            if target.is_dir() and not target.is_symlink():
                # Refused rather than made recursive. `rm -rf` behind a method
                # named `delete` is how a project loses a directory somebody
                # meant to remove one file from.
                raise NotFoundError(f"not a file: {path!r}")
            # `missing_ok=False` plus the catch, rather than `missing_ok=True`:
            # this method's return value *is* "was it there", and `missing_ok`
            # would erase the distinction it exists to report.
            try:
                target.unlink()
            except FileNotFoundError:
                return False
            return True

        return await offload(self._runner, work, name="project_files.delete")

    async def exists(self, path: str) -> bool:
        def work() -> bool:
            # `lexists` semantics: a dangling symlink inside the project exists
            # as an entry even though it resolves to nothing, and a caller
            # asking "is there something here" should be told yes -- otherwise
            # a write that then refuses the link looks like a contradiction.
            return self._target(path).is_symlink() or self._target(path).exists()

        return await offload(self._runner, work, name="project_files.exists")


class FilesystemProjectFileStoreFactory:
    """Builds a store per project root, sharing one blocking-call runner.

    The runner is shared and the sandbox is not. A sandbox resolves its root at
    construction, so one per root is the point -- sharing it would be sharing
    the containment boundary between two projects. The runner is the opposite:
    it is the bound on how many of these calls run at once across the process,
    and one per project would be no bound at all.
    """

    __slots__ = ("_runner",)

    def __init__(self, *, runner: BlockingCallRunner | None = None) -> None:
        self._runner = runner

    def open(self, root_path: str) -> FilesystemProjectFileStore:
        # `ProjectSandbox.__init__` is what refuses a relative, missing or
        # non-directory root, and it raises `ProjectSandboxError`. Not caught
        # and re-wrapped here: the caller registering a path wants to know which
        # of those it was, and every wrapping layer that flattens the reason
        # makes the message somebody eventually reads less useful.
        return FilesystemProjectFileStore(
            ProjectSandbox(root_path), runner=self._runner
        )


__all__ = ["FilesystemProjectFileStore", "FilesystemProjectFileStoreFactory"]
