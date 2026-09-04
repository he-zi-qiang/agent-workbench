"""Enumerating directories so somebody can choose one (ADR-074).

Claude Desktop does this with the OS dialog, and on macOS that dialog *is* the
grant: picking a folder in ``NSOpenPanel`` is what hands the app a TCC scope for
it, which is why that app carries a re-pick flow saying "Claude lost permission
to access X. Select the folder again to restore access."

This console is a browser page, so that mechanism is unavailable -- not
inconvenient, unavailable: ``showDirectoryPicker()`` yields a handle and never
an absolute path, and an absolute path is exactly what the server needs. So the
browsing happens here, on the machine that holds the disk, and the picker in the
browser is a view of these listings.

What that trades away is written down rather than glossed: the native dialog
enumerates nothing until the user acts, while this endpoint can enumerate
directory *names* on request. Three things bound it, and none of them is
"trust the client":

* **Names only, never contents.** No file is read and no file is listed. The
  question is "which folder", and a browser that also listed files would be a
  filesystem read endpoint wearing a picker's clothes -- reachable before any
  root exists, which is the moment nothing has been authorised yet.
* **Directories only**, so the shape of somebody's documents does not leak
  through a control meant to choose among folders.
* **The process is loopback-bound local development** (ADR-044). It already
  runs as the user; it is not a service exposed to anybody else.
"""

from __future__ import annotations

from pathlib import Path

from agent_workbench.adapters.concurrency.call_runner import (
    BlockingCallRunner,
    offload,
)
from agent_workbench.domain.project_files import ProjectPathError
from agent_workbench.ports.project_files import (
    MAX_BROWSE_ENTRIES,
    DirectoryEntry,
    DirectoryListing,
)


class FilesystemDirectoryBrowser:
    """The machine's directory tree, one level at a time."""

    __slots__ = ("_runner", "_start")

    def __init__(
        self,
        *,
        runner: BlockingCallRunner | None = None,
        start: str | None = None,
    ) -> None:
        self._runner = runner
        self._start = start

    def home(self) -> str:
        """Where the picker opens: the configured root, else the user's home.

        The configured root wins only while it *is* a directory. A Compose
        stack names ``/projects`` (ADR-0109), and the bind mount behind it is
        the one thing in that topology a person can leave out -- start the
        stack from a checkout without the folder and the mount is an empty
        directory Docker made, which is fine, but a topology edited to drop
        the mount would leave a path that does not exist. Falling back to the
        home directory then is the same picker the native path shows, rather
        than a 400 on the first request the page makes.
        """

        if self._start is not None and Path(self._start).is_dir():
            return self._start
        return str(Path.home())

    async def browse(self, path: str | None = None) -> DirectoryListing:
        def work() -> DirectoryListing:
            raw = path or self.home()
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                # The same refusal Claude Desktop gives for the equivalent
                # input, and worth matching: a relative path here would be
                # resolved against the *server's* working directory, which is
                # not a place the person browsing has any model of.
                raise ProjectPathError(
                    f"relative paths are not allowed; use an absolute path: {raw!r}"
                )
            resolved = candidate.resolve()
            if not resolved.is_dir():
                raise ProjectPathError(f"not a directory: {resolved}")

            entries: list[DirectoryEntry] = []
            truncated = False
            try:
                children = sorted(
                    resolved.iterdir(), key=lambda item: item.name.casefold()
                )
            except PermissionError as error:
                # A directory the user cannot read is an ordinary thing to walk
                # into (`/Library/...`, another account's home). Reported as a
                # refusal the picker can show rather than a 500, because from
                # the person's side nothing went wrong -- they clicked a folder
                # that is not theirs.
                raise ProjectPathError(f"not readable: {resolved}") from error

            for child in children:
                if len(entries) >= MAX_BROWSE_ENTRIES:
                    truncated = True
                    break
                # `is_dir()` follows symlinks on purpose here, unlike the file
                # store's listing: a symlinked directory is a perfectly ordinary
                # thing to *choose* as a project root, and `ProjectSandbox`
                # resolves the root at construction anyway. The containment
                # rules apply to paths inside a root, not to which root.
                try:
                    if not child.is_dir():
                        continue
                except OSError:
                    # A broken symlink or a mount that is not responding. Skipped
                    # rather than failing the whole listing: one bad entry should
                    # not make a folder unpickable.
                    continue
                entries.append(DirectoryEntry(name=child.name, path=str(child)))

            parent = None if resolved.parent == resolved else str(resolved.parent)
            return DirectoryListing(
                path=str(resolved),
                parent=parent,
                entries=tuple(entries),
                truncated=truncated,
            )

        return await offload(self._runner, work, name="project_files.browse")


__all__ = ["FilesystemDirectoryBrowser"]
