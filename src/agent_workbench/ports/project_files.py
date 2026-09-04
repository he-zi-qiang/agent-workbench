"""Reading and writing the directory a project *is* (ADR-072).

Separate from ``ports/projects.py`` on purpose. That port persists rows in
PostgreSQL and answers "what is filed under this project"; this one operates on
a directory tree the user chose, on the machine the API runs on. They have
different fact sources, different failure modes and different threat models, and
a single port covering both would be a port whose implementations share nothing
but a name.

Three things this contract insists on, each because the obvious shape gets it
wrong:

**A listing is bounded and says when it was cut.** A real project has
``node_modules``; a walk with no ceiling returns a hundred thousand entries or
takes long enough to look like a hang. Every listing therefore carries
``truncated``, and a caller that ignores it is showing a partial tree as if it
were whole -- which is worse than showing nothing, because it reads as *this
project has 500 files*.

**Reading answers with bytes, and separately with whether they are text.** A
coding agent handed the contents of a PNG as mojibake will try to edit it. The
store knows the answer cheaply and the caller does not, so the store says.

**Writes are whole-file.** There is no append and no seek. A partial write that
fails halfway leaves a file whose contents match neither version, and the
recovery story for that is "the user notices" -- which is the story this
codebase refuses everywhere else it stores anything.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, Protocol, runtime_checkable

from agent_workbench.domain.schema import VersionedModel

#: What a single listing may return before it is cut short.
#:
#: A constant, and a small one. This bounds a JSON response, a model's context
#: and a tree the browser renders, and those three want the same number for
#: different reasons. A caller that needs more asks for a subtree -- which is
#: the operation that actually scales, because it matches how anybody reads a
#: repository.
MAX_LISTING_ENTRIES: Final[int] = 2000

#: What a single read may return.
#:
#: 2 MiB. Above this the answer is not "here is your file", it is "this is not
#: a file you want in a context window" -- and a store that silently truncated
#: instead would hand a model a source file missing its last half, which is
#: indistinguishable to the model from a file that ends there.
MAX_READ_BYTES: Final[int] = 2 * 1024 * 1024

#: Directory names never walked into.
#:
#: Not configuration, and not `.gitignore`. `.gitignore` is the right long-term
#: answer and it is a parser plus a per-directory rule stack; this is the
#: honest short one, and it is stated here rather than in an adapter so every
#: implementation skips the same things. The entries are the four that are
#: always machine-generated, always large, and never what somebody opened a
#: project to read.
#:
#: `.git` is first for a reason beyond size: it holds every version of every
#: file, including ones deleted precisely because they should not have been
#: committed. A tree walk that descends into it turns "show me the project"
#: into "show me everything the project ever contained".
ALWAYS_SKIPPED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv"}
)

ProjectEntryKind = Literal["file", "directory"]


class ProjectFileEntry(VersionedModel):
    """One node in a project's tree."""

    #: Always project-relative and POSIX-spelled, never absolute. The absolute
    #: path is the server's business: sending it would put the machine's
    #: directory layout into a browser, and a client that received one would
    #: eventually send it back as if it were a valid request.
    path: str
    kind: ProjectEntryKind
    #: ``None`` for a directory. Not ``0`` -- a directory does not have a size
    #: of zero, it does not have a size, and a UI that renders ``0 B`` next to
    #: every folder is reporting a measurement nobody made.
    size_bytes: int | None = None
    modified_at: datetime


class ProjectListing(VersionedModel):
    """One level, or one bounded walk, of a project's tree."""

    #: The subtree this lists, ``""`` for the root.
    path: str
    entries: tuple[ProjectFileEntry, ...] = ()
    #: Whether entries were dropped for the ceiling. Carried rather than
    #: inferred from ``len(entries) == MAX_LISTING_ENTRIES``: a directory with
    #: exactly the ceiling's worth of files is not truncated, and a caller
    #: deriving the flag would say it was.
    truncated: bool = False


class ProjectFileContent(VersionedModel):
    """What one file holds."""

    path: str
    #: Decoded text when the bytes are valid UTF-8, otherwise ``None``.
    text: str | None = None
    size_bytes: int
    #: ``False`` when the bytes are not UTF-8. The caller needs this to decide
    #: whether to show, edit or refuse -- and it cannot work it out from
    #: ``text is None`` alone once a legitimately empty file exists.
    is_text: bool
    modified_at: datetime


class ProjectFileVersion(VersionedModel):
    """A file as some caller last saw it, offered back as a write precondition.

    Both fields, not just the timestamp. An mtime alone is a good check on a
    filesystem that keeps nanoseconds and a poor one on a filesystem that keeps
    whole seconds, where a user's edit inside the same second as the agent's
    read is invisible. The size costs nothing to carry and closes the common
    half of that: an edit that changes a file's length is caught even when the
    clock cannot tell the two apart.

    What neither field catches is a change that preserves both -- a same-length
    rewrite with the mtime restored. That is `touch -r` territory, deliberate,
    and named here rather than defended against.
    """

    size_bytes: int
    modified_at: datetime


@runtime_checkable
class ProjectFileStore(Protocol):
    """The file tree of one project, with the root already fixed.

    Bound to a root at construction rather than taking one per call. A method
    that accepted a root would make "which project is this" a per-call argument,
    and the first caller to get it wrong would read one project's files while
    believing it was in another -- with every path check passing, because the
    checks are relative to whichever root was handed in.
    """

    @property
    def working_directory(self) -> Path:
        """The directory this store is bound to, for a caller that must *be* in it.

        Named for the one legitimate use rather than for what it is. ADR-077's
        ``project_run`` has to hand a real cwd to a real process, and there is
        no relative spelling of "here" -- every other method on this port takes
        a project-relative path precisely so that no caller ever holds an
        absolute one.

        So the name is the constraint: this is a directory to enter, never a
        base to join a path onto. Joining is ``resolve``'s job and the sandbox's,
        and the moment a caller writes ``store.working_directory / path`` the
        lexical and physical checks in ``domain/project_files.py`` and
        ``adapters/filesystem/sandbox.py`` both stop being the single place
        those rules live -- which is the failure the module docstring of
        ``adapters/tools/project_files.py`` was written to prevent.

        It does not go to a browser. ``ProjectFileEntry.path`` says why: a
        client that received an absolute path would eventually send one back.
        """

        ...

    async def list_directory(self, path: str = "") -> ProjectListing:
        """One directory's immediate children, directories first then by name.

        Not a recursive walk. A tree is drawn by asking for the level somebody
        expanded, which is bounded by what the directory holds rather than by
        what the project holds.
        """

        ...

    async def walk(self, path: str = "") -> ProjectListing:
        """Every file under ``path``, up to ``MAX_LISTING_ENTRIES``.

        For the cases that genuinely need the shape of the whole subtree -- a
        model orienting itself, a search. Directories are not returned; a walk
        answers "what files are there", and the directories are recoverable from
        the paths.
        """

        ...

    async def read(self, path: str) -> ProjectFileContent:
        """One file's contents.

        Raises ``NotFoundError`` when it is not there, and ``OutputTooLargeError``
        above ``MAX_READ_BYTES`` -- refused rather than truncated, because a
        truncated source file is indistinguishable from a short one.
        """

        ...

    def open_bytes(self, path: str) -> tuple[ProjectFileEntry, AsyncIterator[bytes]]:
        """Describe one file and yield its bytes, for serving it onward.

        The other half of ``read``, and the two are not redundant. ``read``
        decodes, refuses above ``MAX_READ_BYTES`` and answers "is this text" --
        it exists to put a file in front of a *model*, and every one of those
        properties is about a context window. This one exists to put a file in
        front of a *browser*, where none of them apply: a PNG has no encoding
        to succeed at, and 2 MiB is an ordinary size for a photograph somebody
        keeps in their repository.

        **No ceiling here, and that is deliberate.** A whole-file read would
        need one, and any number picked for it would be wrong for somebody's
        real directory. Streaming needs none: the bytes never all exist at
        once, so the file's size stops being this process's problem. The
        console declines large files before it asks (`BlobPreview` judges from
        the listing's own count), but that is an interaction courtesy and
        never the bound -- a server that only survives because the client was
        polite is not bounded at all.

        Deliberately **not** ``async def``, the same way ``ArtifactStore.
        iter_chunks`` is not. Everything that can refuse -- the path check, the
        symlinked leaf, the missing file, the directory -- has to refuse when
        this is *called*, while the caller can still turn it into a 404. A
        coroutine that only failed on the first chunk would hand back a 200
        that stops partway, which a client cannot tell from a dropped
        connection.

        The entry comes back beside the iterator rather than being fetched
        separately for the same reason ``routes/code.py`` takes both from one
        call: headers described from a second stat could disagree with the
        bytes that follow them.
        """

        ...

    async def write(
        self,
        path: str,
        content: str | bytes,
        *,
        if_unchanged: ProjectFileVersion | None = None,
        create_only: bool = False,
    ) -> ProjectFileEntry:
        """Create or replace one file, making parent directories as needed.

        Whole-file. Returns the entry as it now stands so a caller does not have
        to read back to learn the size and mtime it just caused.

        ``if_unchanged`` makes the write conditional: the file must still be
        the one that version describes, or ``ProjectFileChangedError`` is
        raised and nothing is written. It defaults to ``None`` -- an
        unconditional write -- because the console's own `PUT` is a person
        acting on their own files and has nobody to be raced by.

        ``create_only`` is the other mood: nothing may be at the path, or
        ``ProjectFileExistsError`` is raised. The two are contradictory and
        setting both is a ``ValueError`` rather than a precedence rule.

        The checks belong here rather than in the caller, and that is the only
        reason these parameters exist. A caller that stats the file and then
        writes has a window between the two in which the user's editor can
        save; a store that checks inside the same offloaded call does not
        (ADR-0078). It is not a lock: two *processes* can still interleave, and
        this one closes the window that this process opens.
        """

        ...

    async def delete(self, path: str) -> bool:
        """Remove one file. Returns whether it was there.

        Files only. Removing a directory is recursive by nature and therefore a
        different, louder operation than this one -- it is not offered here
        rather than offered with a flag, because a flag is how it gets passed by
        accident.
        """

        ...

    async def exists(self, path: str) -> bool:
        """Whether the path names something inside this project."""

        ...


@runtime_checkable
class ProjectFileStoreFactory(Protocol):
    """Turns a registered root path into a store, or refuses it.

    This exists so the application layer can ask "is this a directory an agent
    could be pointed at" without holding a filesystem. That question has no
    pure answer -- it needs ``realpath``, an ``is_dir``, and the machine the
    path is on -- so it belongs behind a port rather than inside a use case.

    The refusal is the useful half. ``open`` raising is how registering a
    mistyped path fails at the moment somebody typed it, rather than later when
    an agent tries to read a file and reports something that reads like its own
    fault.
    """

    def open(self, root_path: str) -> ProjectFileStore:
        """A store over ``root_path``.

        Raises ``ProjectPathError`` (or its sandbox subclass) when the path is
        not absolute, does not exist, or is not a directory.
        """

        ...


#: How many subdirectories one browse step may return.
#:
#: Smaller than the file ceiling, because this feeds a picker somebody scrolls
#: rather than a listing a model reads. A directory with more than this many
#: subdirectories is one nobody picks from by eye anyway -- they type or narrow.
MAX_BROWSE_ENTRIES: Final[int] = 500


class DirectoryEntry(VersionedModel):
    """One directory a person could choose."""

    name: str
    #: Absolute. This is the one place in this API that returns an absolute
    #: path, and it has to: the caller's next request *is* this path, and a
    #: picker that returned relative names would make the client join them --
    #: which is the client-side path arithmetic ADR-072 exists to avoid.
    path: str


class DirectoryListing(VersionedModel):
    """One level of the machine's directory tree, for choosing a project root."""

    path: str
    #: ``None`` at the filesystem root, where there is nowhere further up. Sent
    #: rather than derived so the client never computes a parent path itself.
    parent: str | None = None
    entries: tuple[DirectoryEntry, ...] = ()
    truncated: bool = False


@runtime_checkable
class DirectoryBrowser(Protocol):
    """Enumerate directories so somebody can choose one (ADR-074).

    Deliberately **directories only**, and deliberately no file contents. This
    exists to answer "which folder", and a browser that also listed files would
    be a filesystem read endpoint wearing a picker's clothes -- reachable before
    any root has been registered, which is precisely the moment nothing has been
    authorised yet.
    """

    async def browse(self, path: str | None = None) -> DirectoryListing:
        """Subdirectories of ``path``, or of the user's home when ``None``.

        Raises ``ProjectPathError`` for a relative path -- the same refusal, and
        the same wording, that Claude Desktop gives for the equivalent input.
        """

        ...

    def home(self) -> str:
        """Where a picker opens when it has nowhere better."""

        ...

    async def create(self, parent: str, name: str) -> DirectoryEntry:
        """Make one empty directory called ``name`` inside ``parent``.

        The one write this port does, and it is the write the picker's own
        contract needs: ADR-074 says the start screen is "choose a folder, or
        make one", and starting in a fresh empty folder is the ordinary way to
        begin a piece of work. Everything the module docstring refuses still
        holds -- no file is read, no file is listed, nothing is created *under*
        the new directory. ``name`` is one path segment: a slash, ``.`` or
        ``..`` is refused as ``ProjectPathError`` before anything touches the
        disk, and a name already taken raises ``DirectoryExistsError`` so the
        route can answer 409 rather than 400.
        """

        ...


__all__ = [
    "ALWAYS_SKIPPED_DIRECTORIES",
    "MAX_BROWSE_ENTRIES",
    "MAX_LISTING_ENTRIES",
    "MAX_READ_BYTES",
    "DirectoryBrowser",
    "DirectoryEntry",
    "DirectoryListing",
    "ProjectEntryKind",
    "ProjectFileContent",
    "ProjectFileEntry",
    "ProjectFileStore",
    "ProjectFileStoreFactory",
    "ProjectListing",
]
