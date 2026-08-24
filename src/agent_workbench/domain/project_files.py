"""A project is a directory, and this is the only way to name something in it.

ADR-072. Until now this codebase had exactly one file-shaped surface, and it
was deliberately not a filesystem: a Task workspace is a flat map from a name to
an ``ArtifactRef``, and ``domain/workspace.py`` says why in one sentence worth
repeating -- *a client-supplied path is exactly how path traversal and
cross-tenant reads enter a system*. That property is bought by refusing to let a
path be spelled at all: ``WorkspaceName`` excludes ``/`` and ``\\``, and ``.``
and ``..`` are not names.

A project that is a real directory cannot buy it that way. A coding agent that
can only write flat names cannot create ``src/agent_workbench/domain/`` -- and a
directory tree is the thing the user asked for. So the property has to be bought
again, with a different coin, and this module is that purchase.

Two checks, not one, because they catch different things:

**Lexically** (here): a relative path is refused if it is absolute, if any
segment is ``..``, if it carries a NUL or a drive letter, or if it exceeds the
caps below. This is pure, total, and testable without a filesystem.

**Physically** (``adapters/filesystem/project_files.py``): the resolved real
path must still be inside the resolved real root. This is the check that catches
what no amount of string inspection can -- a symlink at ``notes`` pointing at
``/etc``, where every segment of ``notes/passwd`` is lexically innocent.

Neither check is sufficient. Lexical validation alone is defeated by a symlink.
Realpath alone is defeated by nothing in principle, but it is IO: it cannot run
in ``domain``, it cannot run before the path exists, and a rule that only exists
inside an adapter is a rule the next adapter will not have. So both, and the
lexical one lives here where it is reachable by anything that needs to reject a
path early -- an API route validating a request body has no business touching
the disk to find out the body was malformed.

What this module does **not** do is decide *which* roots exist. A project's root
is registered explicitly and separately (``ProjectRoot``); a path is meaningful
only against a root somebody already allowed. Containment answers "is this
inside that", never "is that allowed".
"""

from __future__ import annotations

import unicodedata
from pathlib import PurePosixPath
from typing import Annotated, Final

from pydantic import StringConstraints

#: The tool that runs a command on this machine, in the project's directory.
#:
#: Spelled here rather than beside the adapter because two things outside that
#: adapter have to name it and neither may import it: ``CODE_PROJECT_TOOLS``
#: in ``application/code_session.py``, which decides what a turn is offered,
#: and the risk-ceiling derivation next to it. Its four file-shaped siblings
#: are still bare literals in both places -- they are named in exactly one
#: list each, and moving them is a separate change to make deliberately rather
#: than as a side effect of this one.
#:
#: ``project_`` and not ``shell`` or ``bash``, for the reason ADR-073 gave the
#: file tools their prefix: the name is frozen into an authorization envelope
#: and is what answers "what was this run allowed to do" afterwards. `project_`
#: says the answer, which is *in this session's project directory, on the
#: machine the API is running on* -- and it keeps the name from reading as a
#: sibling of ``sandbox_run``, which ADR-057 spent a whole decision separating
#: from exactly this.
PROJECT_RUN_TOOL: Final[str] = "project_run"

#: What a principal must hold before ``project_run`` may be dispatched.
#:
#: Its own scope rather than ``sandbox:run``. The two grants are not the same
#: grant and ADR-057 spent a whole decision saying so: one is a network-less
#: container destroyed after the call, the other is this machine. A principal
#: that was given the sandbox has not been given the machine, and reusing the
#: name would make that upgrade happen silently, to every principal that
#: already held it, on the day the tool shipped.
PROJECT_RUN_SCOPE: Final[str] = "project:run"

#: Depth ceiling for a project-relative path.
#:
#: A constant rather than configuration, for the reason the workspace caps are:
#: a deployment that could raise it would be one where the value is unbounded,
#: and every consumer downstream (a tree the UI renders, a listing a model
#: reads) is sized by it. 32 is far past any real source tree -- this
#: repository's deepest path is 6 -- and far short of what it takes to make a
#: recursive walk interesting.
MAX_PATH_SEGMENTS: Final[int] = 32

#: Byte ceiling on one segment. 255 is what ext4, APFS and NTFS each stop at, so
#: a longer name is not a policy choice being made here -- it is a write that
#: was going to fail at the syscall, failing earlier and with a reason.
MAX_SEGMENT_BYTES: Final[int] = 255

#: Byte ceiling on the whole relative path. Kept well under the 4096 a Linux
#: `PATH_MAX` allows, because the root is prepended to it and the root is not
#: bounded by anything this module can see.
MAX_RELATIVE_PATH_BYTES: Final[int] = 1024

#: Segments a project-relative path may never contain, whatever the platform.
#:
#: ``..`` is the traversal itself. ``.`` is refused rather than normalised away:
#: normalising means two spellings name one file, and "the same file under two
#: names" is how a check that ran on one spelling gets bypassed with the other.
#: The empty segment catches ``a//b`` for the same reason.
_FORBIDDEN_SEGMENTS: Final[frozenset[str]] = frozenset({"", ".", ".."})

#: Names Windows refuses regardless of extension. Checked even though this
#: project runs on POSIX: a path that is legal here and reserved there becomes a
#: repository somebody cannot check out, and the failure surfaces on a machine
#: nobody was looking at. Cheap to refuse now, expensive to discover later.
_WINDOWS_RESERVED: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in "123456789"}
    | {f"lpt{digit}" for digit in "123456789"}
)

#: A single path segment. Excludes the separators, the NUL, and every C0 control
#: character -- a newline inside a filename is legal on POSIX and turns any
#: line-oriented listing (a diff, a `git status`, this project's own event
#: payloads) into something that can be forged by naming a file.
ProjectPathSegment = Annotated[
    str,
    StringConstraints(pattern=r"^[^/\\\x00-\x1f]+$", min_length=1, max_length=255),
]


class ProjectPathError(ValueError):
    """A path was refused before anything touched the disk.

    A ``ValueError`` rather than an ``AgentWorkbenchError``: this is a malformed
    argument, and the runtime already turns those into ``invalid_tool_input``.
    Giving it a code of its own would create a second way to say the same thing
    and a second thing for a caller to forget to handle.
    """


def _reject(path: str, reason: str) -> ProjectPathError:
    """Build the refusal.

    The offending path is quoted back. That is safe here and not everywhere:
    this string came from the caller's own request, so echoing it leaks nothing
    the caller did not already send -- unlike a provider exception, whose text
    ``domain/errors.py`` drops for exactly the opposite reason.
    """

    return ProjectPathError(f"{reason}: {path!r}")


def normalize_segment(segment: str) -> str:
    """Put one segment into the form the filesystem will compare it in.

    NFC, because macOS does not store what you hand it. APFS and HFS+ normalise
    filenames on write, so a name containing a composed ``é`` (U+00E9) comes
    back decomposed (``e`` + U+0301). Two Python strings that are not ``==``
    therefore name one file, and any bookkeeping this project keeps beside the
    disk -- a manifest, a dedup set, an "did I already write this" check --
    silently disagrees with the disk about how many files exist.

    Normalising on the way in makes that disagreement impossible to introduce,
    at the cost of refusing to distinguish two names the filesystem was never
    going to distinguish anyway.
    """

    return unicodedata.normalize("NFC", segment)


def validate_relative_path(raw: str) -> PurePosixPath:
    """Refuse anything that is not a plain relative path inside a project.

    Returns the parsed path so a caller cannot accept the validation and then
    re-parse the raw string differently -- the returned value *is* the checked
    thing, and the raw string should not be used again.

    This is lexical only. It is necessary and **not sufficient**: see the module
    docstring, and see ``adapters/filesystem/project_files.py`` for the realpath
    half that catches the symlink this function cannot see.
    """

    if not raw:
        raise _reject(raw, "a project path may not be empty")

    if "\x00" in raw:
        # Before anything else: a NUL truncates the path at the syscall
        # boundary, so `"safe.txt\x00/../../etc/passwd"` is `safe.txt` to every
        # check written in Python and something else entirely to open(2).
        raise _reject(raw, "a project path may not contain a NUL")

    if len(raw.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise _reject(
            raw, f"a project path may not exceed {MAX_RELATIVE_PATH_BYTES} bytes"
        )

    # Backslash is normalised to `/` rather than refused. On Windows it is a
    # separator, and a caller that sends `src\main.py` means two segments; if
    # this function let it through as one, `..\..\etc` would be a single
    # innocent-looking segment that the platform then splits into a traversal.
    candidate = raw.replace("\\", "/")

    if candidate.startswith("/"):
        raise _reject(raw, "a project path must be relative")

    # `C:` and friends. `PurePosixPath` does not know what a drive is, so an
    # absolute Windows path arrives here looking like an ordinary first segment.
    head = candidate.split("/", 1)[0]
    if len(head) == 2 and head[1] == ":" and head[0].isalpha():
        raise _reject(raw, "a project path may not carry a drive letter")

    # Split the string, rather than reading `PurePosixPath(...).parts`.
    #
    # `PurePosixPath` normalises before you can look: `./src`, `a//b` and `a/`
    # come back as `('src',)`, `('a', 'b')` and `('a',)`. The `.` and empty
    # segments this function means to refuse are gone by the time `.parts`
    # answers, so a check written against it silently never fires -- which is
    # how the very hazard documented above ("two spellings name one file") gets
    # introduced by the code meant to prevent it. Found by the test for it.
    #
    # `..` survives `.parts`, because it cannot be normalised away without
    # resolving symlinks. That difference is exactly why relying on `.parts`
    # looked like it worked.
    segments = tuple(candidate.split("/"))

    if not segments:
        raise _reject(raw, "a project path must name something")

    if len(segments) > MAX_PATH_SEGMENTS:
        raise _reject(
            raw, f"a project path may not exceed {MAX_PATH_SEGMENTS} segments"
        )

    for segment in segments:
        if segment in _FORBIDDEN_SEGMENTS:
            raise _reject(raw, f"a project path may not contain a {segment!r} segment")
        if "\x00" in segment or any(character < " " for character in segment):
            raise _reject(raw, "a project path may not contain control characters")
        if len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES:
            raise _reject(
                raw, f"a path segment may not exceed {MAX_SEGMENT_BYTES} bytes"
            )
        # `stem` of `nul.txt` is `nul`, and Windows reserves the stem, not the
        # whole name.
        if segment.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
            raise _reject(raw, f"{segment!r} is a reserved filename")
        if segment != segment.strip() or segment.endswith("."):
            # Trailing dots and spaces are silently stripped by Windows, which
            # makes `report.txt ` and `report.txt` the same file there and two
            # files here -- the same two-names-one-file hazard as `.`.
            raise _reject(raw, f"{segment!r} may not begin or end with a space or dot")

    return PurePosixPath(*(normalize_segment(segment) for segment in segments))


def is_within(root: PurePosixPath, candidate: PurePosixPath) -> bool:
    """Whether ``candidate`` is at or under ``root``, comparing whole segments.

    Segment-wise, never ``str.startswith``. ``/srv/projects/alpha-secrets``
    starts with ``/srv/projects/alpha`` as a string and is a different project;
    a containment check written on the string form hands one project's files to
    another whenever a name is a prefix of a name.

    Both arguments must already be resolved by the caller. This function cannot
    tell a resolved path from an unresolved one, and answering ``True`` for an
    unresolved pair is exactly the symlink hole -- which is why the only
    production caller is the adapter, immediately after it calls ``realpath``.
    """

    if not root.is_absolute() or not candidate.is_absolute():
        raise ProjectPathError("containment is only defined between absolute paths")
    return candidate == root or root in candidate.parents


__all__ = [
    "MAX_PATH_SEGMENTS",
    "MAX_RELATIVE_PATH_BYTES",
    "MAX_SEGMENT_BYTES",
    "PROJECT_RUN_SCOPE",
    "PROJECT_RUN_TOOL",
    "ProjectPathError",
    "ProjectPathSegment",
    "is_within",
    "normalize_segment",
    "validate_relative_path",
]
