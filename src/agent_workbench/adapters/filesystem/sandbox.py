"""The half of the project-path sandbox that needs a disk to be true.

``domain/project_files.py`` refuses paths that *say* they leave the project.
This refuses paths that *go* there anyway, and the difference is one symlink:
``notes -> /etc`` makes every segment of ``notes/passwd`` lexically innocent
while the open(2) lands in ``/etc``. No amount of string inspection sees that,
which is why the lexical check is documented as necessary and not sufficient.

Three rules, and each one exists because the other two do not cover it.

**Resolve, then compare whole segments.** ``Path.resolve()`` expands every
symlink in the prefix -- including for a path that does not exist yet, which is
the normal case for a write. The comparison is ``is_within``, segment-wise,
because ``/srv/alpha-secrets`` starts with ``/srv/alpha`` as a string.

**Never follow a symlink on the final component when writing.** Resolution
happens before the syscall, so between them an agent that can write can plant a
symlink and have the next write go through it. ``O_NOFOLLOW`` closes that
window at the kernel: the open fails rather than following. This is the one
rule that does not depend on the check having been run recently.

**Never create a symlink through this API at all.** There is no operation here
that makes one. An agent that cannot create the link cannot exploit the race
that the rule above is a backstop for -- but the backstop stays, because "the
agent" is not the only thing that can write into a project directory. The user
has a shell.

### What this does not claim

It is not a container, and the root is not a mount namespace. A project root
that itself sits inside something sensitive is trusted as far as the person who
registered it trusted it -- containment is answered relative to a root, never
"is that root a reasonable thing to hand an agent". That judgement is
registration's (ADR-072 §4), and registration is explicit for exactly this
reason.

Hard links are not defended against and cannot be: a hard link inside the root
to a file outside it is indistinguishable from the file itself, because it *is*
the file -- there is no path component to resolve and no bit to inspect. The
mitigations are the ones registration already implies (do not register a root
someone hostile can write into) rather than something this module can add.

Case-insensitive filesystems (APFS by default, and this project's development
machines are macOS) make ``Proj/a`` and ``proj/a`` one file while
``Path.resolve()`` preserves whichever case the caller wrote. That makes
containment *stricter* than the filesystem, never looser: a mismatch is refused,
not admitted. A usability edge, not a hole, and it is left visible rather than
papered over with a ``casefold()`` that would make the comparison looser on the
platforms where the filesystem is case-sensitive.
"""

from __future__ import annotations

import contextlib
import errno
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from agent_workbench.domain.project_files import (
    ProjectPathError,
    is_within,
    validate_relative_path,
)


class ProjectSandboxError(ProjectPathError):
    """A path was refused by the physical check.

    A subclass, not a sibling: a caller that wants to answer "was this path
    rejected" should not have to know which half rejected it, and every such
    caller today does exactly that. The two are distinguishable when it matters
    -- a log line wants to say *symlink escape* rather than *bad path* -- and
    identical when it does not.
    """


class ProjectSandbox:
    """One project root, and the only way to turn a request into a real path.

    Constructed per root rather than per call, so the root's own resolution
    happens once. That is not only a saving: resolving the root on every call
    would let the root change underneath a sequence of operations that a caller
    believes are all in one project.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path | str) -> None:
        candidate = Path(root).expanduser()
        if not candidate.is_absolute():
            raise ProjectSandboxError(f"a project root must be absolute: {root!r}")
        resolved = candidate.resolve()
        if not resolved.is_dir():
            # Checked at construction, the way `resolve_web_directory` checks
            # the console directory: a sandbox over a root that is not there is
            # a deployment that looks healthy and fails on first use, and the
            # person who mistyped the path finds out from an agent's error.
            raise ProjectSandboxError(
                f"a project root must be an existing directory: {resolved}"
            )
        self._root = resolved

    @property
    def root(self) -> Path:
        return self._root

    def _checked(self, relative: str) -> tuple[Path, Path]:
        """Both forms of the target: the literal join, and the resolved real path.

        Two values, and handing back only one was a real defect rather than an
        inelegance. The first version returned only the resolved path, and
        ``open_for_write`` then passed *that* to ``os.open`` -- so the symlink
        had already been followed, in Python, before the kernel was asked not to
        follow it. ``O_NOFOLLOW`` on a path with no link at its leaf refuses
        nothing. The test for a leaf link that resolves back inside the root is
        what caught it, because that is the one case where the containment check
        cannot also happen to catch the escape.

        So the two are used for different jobs and neither substitutes:

        * the **resolved** path answers containment -- every intermediate
          symlink is expanded, which is the only way to see one;
        * the **literal** path is what gets opened, so ``O_NOFOLLOW`` sees the
          leaf exactly as the caller named it.

        Together they cover the whole path: intermediate components by
        resolution, the final component by the open flag.
        """

        checked: PurePosixPath = validate_relative_path(relative)
        literal = self._root / Path(*checked.parts)
        # `strict=False` (the default): the target of a write does not exist
        # yet, and refusing to resolve an absent path would make this usable
        # only for reads. The existing prefix still has its symlinks expanded,
        # which is where an escape would be planted.
        resolved = literal.resolve()
        if not is_within(
            PurePosixPath(self._root.as_posix()), PurePosixPath(resolved.as_posix())
        ):
            # The message names neither the resolved path nor the root. It is
            # reporting the outcome of following a link the caller may not have
            # created, and echoing where it pointed answers "what is outside the
            # project" -- which is what the probe was asking.
            raise ProjectSandboxError(f"path escapes the project root: {relative!r}")
        return literal, resolved

    def resolve(self, relative: str) -> Path:
        """The real path this request names, once it is known to be inside the root.

        For callers that need to *locate* a file -- statting it, listing around
        it, showing where it is. Callers that need to *open* one must use
        ``open_for_write`` / ``read_bytes`` instead, which keep the literal path
        so the open flag can still see the leaf.
        """

        return self._checked(relative)[1]

    def open_for_write(self, relative: str, *, exist_ok: bool = True) -> int:
        """Open a file descriptor for writing, refusing to follow a final symlink.

        Returns a raw descriptor rather than a file object because the flags are
        the point: ``O_NOFOLLOW`` has no expression in ``open()``'s mode string,
        and a caller that wrapped this in ``Path.open()`` would silently lose it.

        The parent directory is created; the leaf is not followed. ``O_NOFOLLOW``
        applies to the final component only, which is precisely the component
        ``resolve()`` could not have checked -- every earlier one was expanded
        and compared.
        """

        literal, _ = self._checked(relative)
        # The *literal* path, not the resolved one -- see `_checked`. Opening
        # the resolved path would mean the link was already followed here, and
        # O_NOFOLLOW would have nothing left to refuse.
        literal.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        if not exist_ok:
            flags |= os.O_EXCL
        try:
            return os.open(literal, flags, 0o600)
        except OSError as error:
            # ELOOP is what O_NOFOLLOW raises on a symlinked leaf. Translated so
            # a caller sees the sandbox refusing rather than an errno it would
            # have to know to interpret -- and so the refusal reads the same as
            # the one `resolve` raises, which is the same event.
            if error.errno in (errno.ELOOP, errno.EMLINK):
                raise ProjectSandboxError(
                    f"refusing to write through a symlink: {relative!r}"
                ) from error
            raise

    def write_bytes(self, relative: str, payload: bytes) -> None:
        """Replace a file's contents, all at once or not at all.

        Temp-then-``os.replace`` rather than ``open_for_write`` and a ``write``,
        and the reason is a constraint this project already wrote down elsewhere:
        ``adapters/concurrency/call_runner.py`` says only work whose *partial
        execution is unobservable* may be handed to a thread, and names
        ``LocalArtifactStore.put`` as the counter-example. A plain
        ``O_TRUNC``-and-write is exactly that counter-example -- abandon it
        halfway and the file on disk is neither the old version nor the new one,
        and nothing is left that could tell the difference.

        With ``os.replace`` there is no halfway. The rename is atomic within a
        filesystem, so a reader sees the old bytes or the new bytes. A cancelled
        write leaves a temp file behind, which is litter rather than corruption
        -- and litter with a recognisable name, so it can be swept.

        The symlink guarantee is kept by checking the leaf *before* writing, not
        by relying on the rename. ``os.replace`` does not follow a symlinked
        destination -- it replaces the link itself -- which would technically be
        safe and would silently turn somebody's symlink into a regular file. A
        refusal is the honest outcome: the caller named a link, and this store
        does not know what they meant by it.
        """

        literal, _ = self._checked(relative)
        if literal.is_symlink():
            raise ProjectSandboxError(
                f"refusing to write through a symlink: {relative!r}"
            )
        literal.parent.mkdir(parents=True, exist_ok=True)
        # In the same directory, so the rename stays within one filesystem --
        # `os.replace` across a mount point is not atomic and on some platforms
        # is not permitted at all. A temp in `/tmp` would reintroduce exactly
        # the halfway state this method exists to remove.
        descriptor, temporary = tempfile.mkstemp(
            dir=literal.parent, prefix=f".{literal.name}.", suffix=".partial"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                # Flush to the platter before the rename. Without it the rename
                # can land while the bytes are still in the page cache, and a
                # crash leaves a file that exists, has the right name, and is
                # empty -- the one outcome worse than no file.
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, literal)
        except BaseException:
            # The temp is this method's to clean up on the failure path; on the
            # success path `os.replace` consumed it.
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def open_for_read(self, relative: str) -> BinaryIO:
        """A handle on a file, with the leaf link refused before it is opened.

        The checks are `read_bytes`'s -- this is that method with the final
        ``.read()`` taken off, and the caller taking ownership of the handle.
        Split out for the one caller that must **not** hold the whole file: a
        project directory is somebody's real repository and can contain a video,
        so the route that serves those bytes to a browser streams them.

        Ownership is the whole reason this is separate rather than a flag. A
        `bytes` return has no lifetime to get wrong; a handle does, and the
        caller closing it is a thing the type says out loud.
        """

        literal, _ = self._checked(relative)
        try:
            descriptor = os.open(literal, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ProjectSandboxError(
                    f"refusing to read through a symlink: {relative!r}"
                ) from error
            raise
        try:
            return os.fdopen(descriptor, "rb")
        except BaseException:
            # `fdopen` takes ownership on success; if it raised, nothing did.
            os.close(descriptor)
            raise

    def read_bytes(self, relative: str) -> bytes:
        """Read a file, refusing a symlinked leaf for the same reason writes do.

        A read through a link is how a project's contents get to include
        ``~/.ssh/id_ed25519`` without any path ever having said so.
        """

        with self.open_for_read(relative) as handle:
            return handle.read()


__all__ = ["ProjectSandbox", "ProjectSandboxError"]
