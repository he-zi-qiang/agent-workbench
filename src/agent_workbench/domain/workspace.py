"""A Task's working set: mutable names over immutable bytes (ADR-028).

An artifact is written once and named by its content. That is what makes a
tool result safe to reference from an event and a checkpoint, and it is not
negotiable. But an agent that is *working* needs the opposite property: it puts
something down, picks it up two steps later, and replaces it with a better
version.

Both are available at once by moving the mutability up one layer. A workspace
is a mapping from a name to an ``ArtifactRef``; writing a name stores new bytes
and produces a *new manifest*. The bytes never change and nothing is deleted --
what changes is which bytes a name points at. Git resolves the same tension the
same way, and for the same reason: a tree that can be rewritten on top of blobs
that cannot is what lets history stay checkable while work stays editable.

The manifest is itself stored as an artifact, so "which version of the
workspace" is one identifier a checkpoint can hold. A node reads the version
pinned at its entry and a replay therefore sees exactly what the first attempt
saw -- not the half-finished writes of the attempt that died.

Names are names, not paths. ``ArtifactRef`` refuses to carry a filesystem path
because "a client-supplied path is exactly how path traversal and cross-tenant
reads enter a system", and re-introducing one here as a *key* would give that
back. There are no directories; a model that wants hierarchy writes
``draft-v2.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Final

import regex
from pydantic import Field, StringConstraints

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.schema import VersionedModel

#: Flat, printable, no separator of any kind. The character class excludes ``/``
#: and ``\`` so a path cannot be spelled, and requires an alphanumeric first
#: character so ``.`` and ``..`` are not names.
WorkspaceName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"),
]

#: Ceilings, as constants rather than configuration. They bound a checkpoint
#: row and a model's listing at the same time, and a deployment that could
#: raise them would be one where those two things are unbounded.
MAX_WORKSPACE_ENTRIES: Final[int] = 256
MAX_WORKSPACE_TOTAL_BYTES: Final[int] = 64 * 1024 * 1024

#: The names these tools are known by. They live in the domain rather than
#: beside the handlers because both an agent profile and the authorization
#: envelope have to name them, and neither may import an adapter.
WORKSPACE_LIST_TOOL: Final[str] = "workspace_list"
WORKSPACE_READ_TOOL: Final[str] = "workspace_read"
WORKSPACE_WRITE_TOOL: Final[str] = "workspace_write"
WORKSPACE_EDIT_TOOL: Final[str] = "workspace_edit"
WORKSPACE_GREP_TOOL: Final[str] = "workspace_grep"

#: What one read may put into the model's context in one go. Above this the
#: bytes are still there -- neither store is lossy -- but the model is handed a
#: window and told where it stopped, because a single read should not be able
#: to spend the whole context budget.
#:
#: Here rather than beside either handler because it was beside *both*: the
#: flat workspace and the project directory each declared their own 48,000 and
#: each wrote its own sentence about hitting it, so the same event was reported
#: to the model in two wordings that a reader would have to diff to compare.
MAX_INLINE_READ_CHARS: Final[int] = 48_000

#: What one grep may report, and how much it may read to get there (ADR-030
#: §2.4). Constants rather than configuration, like the workspace ceilings
#: above and for the same reason: they bound what reaches a model's context,
#: and a deployment that could raise them would be one where that is unbounded.
MAX_GREP_MATCHES: Final[int] = 100
MAX_GREP_LINE_CHARS: Final[int] = 400
MAX_GREP_SCANNED_BYTES: Final[int] = 8 * 1024 * 1024
#: The pattern comes from the model, which makes it untrusted input. A
#: catastrophically backtracking regex must not be able to hold a Worker, so
#: matching runs under this and is abandoned when it passes.
GREP_TIMEOUT_SECONDS: Final[float] = 2.0
#: What a principal must hold before the write tool may be dispatched. Shared
#: with the edit tool: both replace what a name points at, and a permission
#: that let you rewrite a file only if you rewrote all of it would be a
#: distinction nobody could act on.
#:
#: **Shared through this name, which it was not until 2026-08-31.** Four tool
#: specs -- `workspace_write`, `workspace_edit`, `project_write`, `project_edit`
#: -- each spelled the string themselves, so "they share a scope" was four
#: literals that happened to agree and nothing that would notice if one of them
#: stopped. `PROJECT_RUN_SCOPE` next door had it right from the start.
WORKSPACE_WRITE_SCOPE: Final[str] = "workspace:write"


class WorkspaceOverflowError(ValueError):
    """A write would take the workspace past a ceiling.

    Raised rather than returned, and never partially applied: the manifest the
    caller already holds is unchanged, so a refused write leaves no trace to
    reconcile.
    """


class WorkspaceEditMatchError(ValueError):
    """``old_text`` did not occur in the file exactly once.

    Both directions are errors, and neither is recoverable by guessing
    (ADR-030 §2.3). Zero matches means the model believes the file says
    something it does not -- editing anything would be editing a file it has
    not read. More than one means the model believes there is one occurrence
    and there is not; "the first one" is a position it never chose, and an edit
    applied there is silent corruption that leaves no trace to find later.
    """

    def __init__(self, *, name: str, matches: int) -> None:
        self.name = name
        self.matches = matches
        super().__init__(
            f"old_text occurs {matches} times in {name!r}, and an edit needs "
            "exactly one occurrence"
            + (
                "; include more surrounding text to identify which one"
                if matches > 1
                else "; read the file and copy the text to replace from it"
            )
        )


def replace_exactly_once(text: str, old: str, new: str, *, name: str) -> str:
    """``old`` -> ``new`` in ``text``, or refuse.

    Placed in the domain rather than beside the tool handler because the rule
    -- not the plumbing -- is the decision ADR-030 recorded, and because a
    second caller (a future v2 node, an editor over a different store) must not
    get to re-answer it.

    An empty ``old`` is refused by the same count: it "occurs" between every
    pair of characters, so ``str.count`` reports ``len(text) + 1`` and the edit
    is rejected as ambiguous rather than inserting at a position nobody named.
    """

    matches = text.count(old)
    if matches != 1:
        raise WorkspaceEditMatchError(name=name, matches=matches)
    return text.replace(old, new, 1)


class WorkspacePatternError(ValueError):
    """The search pattern is not one this engine can compile."""


class WorkspaceScanTimeoutError(RuntimeError):
    """Matching passed its time budget and was abandoned.

    A real interruption rather than a report written after the fact: the
    pattern is model-authored, so an engine that could only notice the overrun
    once matching returned would notice it never.
    """


@dataclass(frozen=True, slots=True)
class GrepMatch:
    """One line that matched, and where it was."""

    name: str
    line_number: int
    line: str


@dataclass(frozen=True, slots=True)
class GrepOutcome:
    """What a scan found, and what it did not get to.

    The two truncation flags are separate because they mean different things to
    whoever reads the result. ``more_matches`` says the answer is incomplete
    for a reason the caller can fix by searching for something narrower;
    ``unscanned_files`` says some files were never looked at, so a *negative*
    result is not evidence of absence. Collapsing them into one boolean would
    make "I found a lot" indistinguishable from "I stopped early", which is the
    difference between a useful answer and a misleading one.
    """

    matches: tuple[GrepMatch, ...]
    more_matches: bool
    unscanned_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReadWindow:
    """One slice of a text file, and every way it is not the whole file.

    Four separate facts rather than a ``truncated`` boolean, because the caller
    that matters is a model deciding what to do next, and the four call for
    different next moves. ``last_line < total_lines`` means ask again from
    ``next_offset``. ``stopped_at_char_ceiling`` means the window ended because
    of the context budget rather than because of anything about the file, so a
    smaller ``limit`` is not the fix. ``line_cut`` means the last line itself
    came back shortened, and it is the only one of the four that loses bytes:
    continuing from ``next_offset`` skips the tail of that line, which is a
    thing that has to be said out loud rather than left for the model to infer
    from a line number that does not add up. ``withheld_chars`` is how many
    that is -- a number, because "some of it is missing" and "the last 300
    characters are missing" call for different next moves, and because when the
    cut line is also the last line, no offset reaches them at all.
    """

    text: str
    first_line: int
    last_line: int
    total_lines: int
    stopped_at_char_ceiling: bool
    line_cut: bool
    withheld_chars: int = 0

    @property
    def next_offset(self) -> int | None:
        """Where a following read continues, or ``None`` at the end."""

        return self.last_line + 1 if self.last_line < self.total_lines else None

    @property
    def is_whole_file(self) -> bool:
        return self.first_line == 1 and self.next_offset is None and not self.line_cut


def read_window(text: str, *, offset: int = 1, limit: int | None = None) -> ReadWindow:
    """The lines of ``text`` from ``offset``, bounded by ``limit`` and the ceiling.

    Whichever bites first wins, and the result says which. Slicing by line
    alone would let a `limit` of 5000 on a generated file spend the whole
    context budget in one call; slicing by characters alone gives back a window
    whose edges are not lines, and a model cannot ask for "the rest" of that.

    ``offset`` past the end is not clamped. A caller that asked for line 900 of
    a 40-line file has made a mistake worth hearing about, and returning the
    last line instead would answer a question nobody asked.
    """

    lines = text.splitlines(keepends=True)
    total = len(lines)
    start = max(1, offset)
    if start > total:
        return ReadWindow(
            text="",
            first_line=start,
            last_line=start - 1,
            total_lines=total,
            stopped_at_char_ceiling=False,
            line_cut=False,
        )

    taken: list[str] = []
    budget = MAX_INLINE_READ_CHARS
    stopped_at_ceiling = False
    line_cut = False
    withheld = 0
    last = start - 1
    for index in range(start - 1, total):
        if limit is not None and len(taken) >= limit:
            break
        line = lines[index]
        if len(line) <= budget:
            taken.append(line)
            budget -= len(line)
            last = index + 1
            continue
        stopped_at_ceiling = True
        if taken:
            break
        # Nothing taken yet, so this one line decides whether the file is
        # readable at all. Its terminator is allowed over the ceiling: a line
        # whose text fits and whose "\n" does not would otherwise come back
        # cut, reporting one withheld character and costing a whole extra call
        # to fetch a newline.
        body = line.rstrip("\r\n")
        last = index + 1
        if len(body) <= budget:
            taken.append(line)
            break
        # A minified bundle, a single-line JSON document. Handing back nothing
        # would make the file unreadable at any offset; handing back the
        # ceiling's worth of it loses the tail, which `line_cut` announces.
        taken.append(line[:budget])
        withheld = len(line) - budget
        line_cut = True
        break

    return ReadWindow(
        text="".join(taken),
        first_line=start,
        last_line=last,
        total_lines=total,
        stopped_at_char_ceiling=stopped_at_ceiling,
        line_cut=line_cut,
        withheld_chars=withheld,
    )


def describe_read_window(label: str, window: ReadWindow) -> str | None:
    """The one line that precedes a partial read, or ``None`` for a whole file.

    Built here rather than at each handler so the two file languages report the
    same event in the same words. A model that learns "stopped at the ceiling,
    pass offset=N" from the workspace and then meets a different sentence in a
    project directory has to work out whether it is the same situation.
    """

    if window.is_whole_file:
        return None
    if not window.text:
        return (
            f"{label} has {window.total_lines} lines; "
            f"offset {window.first_line} is past the end."
        )
    where = (
        f"{label}: lines {window.first_line}-{window.last_line} of {window.total_lines}"
    )
    if window.line_cut:
        cut = (
            f"{where}, and line {window.last_line} is longer than the "
            f"{MAX_INLINE_READ_CHARS}-character ceiling on its own, so it is "
            f"shown cut: {window.withheld_chars} characters of it are not here"
        )
        if window.next_offset is None:
            # The one place this mechanism cannot reach: the tail of a cut line
            # that is also the last line -- a minified bundle, a one-line JSON
            # document, which are the two cases the cut exists for. `offset`
            # addresses lines and there is no line after this one to ask for.
            # Said plainly and pointed somewhere that works, because the
            # sentence this replaces interpolated `next_offset` unconditionally
            # and told the model to `pass offset=None` -- a value the schema
            # rejects, whose likely sequel is the identical read again and
            # `MAX_IDENTICAL_CALLS` ending the turn on the third.
            return (
                f"{cut}, and no offset reaches them: this is the file's last "
                "line. Search the file rather than reading it."
            )
        return f"{cut}. offset={window.next_offset} continues after that line."
    if window.stopped_at_char_ceiling:
        return (
            f"{where}, stopped at the {MAX_INLINE_READ_CHARS}-character "
            f"ceiling; pass offset={window.next_offset} to continue."
        )
    if window.next_offset is not None:
        return f"{where}; pass offset={window.next_offset} to continue."
    return f"{where}."


def grep_workspace(
    files: Sequence[tuple[str, str]],
    pattern: str,
    *,
    now: Callable[[], float],
    timeout_seconds: float = GREP_TIMEOUT_SECONDS,
) -> GrepOutcome:
    """Lines matching ``pattern`` across ``files``, under every ceiling.

    ``files`` arrives as ``(name, text)`` already decoded and already ordered,
    so this function performs no I/O and the caller keeps the decision about
    which files exist. That is what lets the bounds be tested without a store.

    The time budget covers the **whole scan**, not one match. A per-call
    timeout would multiply by the number of lines, so a pattern slow enough to
    be interesting would still hold the Worker for lines x timeout seconds.
    """

    try:
        compiled = regex.compile(pattern)
    except regex.error as error:
        raise WorkspacePatternError(str(error)) from error

    deadline = now() + timeout_seconds
    matches: list[GrepMatch] = []
    scanned = 0
    unscanned: list[str] = []
    more = False

    for index, (name, text) in enumerate(files):
        encoded_size = len(text.encode("utf-8", errors="replace"))
        if scanned + encoded_size > MAX_GREP_SCANNED_BYTES or more:
            # Every remaining file, named. A model told only "truncated" cannot
            # tell whether the file it cares about was searched.
            unscanned.extend(other for other, _ in files[index:])
            break
        scanned += encoded_size

        for line_number, line in enumerate(text.splitlines(), start=1):
            remaining = deadline - now()
            if remaining <= 0:
                raise WorkspaceScanTimeoutError(
                    f"searching stopped after {timeout_seconds} seconds"
                )
            # Truncated before matching, not after. The ceiling is on what
            # reaches the model *and* on what the engine is handed, because
            # backtracking cost grows with the subject.
            candidate = line[:MAX_GREP_LINE_CHARS]
            try:
                found = compiled.search(candidate, timeout=remaining)
            except TimeoutError as error:
                raise WorkspaceScanTimeoutError(
                    f"searching stopped after {timeout_seconds} seconds"
                ) from error
            if found is None:
                continue
            if len(matches) >= MAX_GREP_MATCHES:
                more = True
                unscanned.extend(other for other, _ in files[index + 1 :])
                break
            matches.append(
                GrepMatch(name=name, line_number=line_number, line=candidate)
            )

    return GrepOutcome(
        matches=tuple(matches),
        more_matches=more,
        unscanned_files=tuple(unscanned),
    )


class WorkspaceManifest(VersionedModel):
    """One version of a Task's working set.

    Immutable, like every other domain object here. :meth:`with_entry` returns
    the next version instead of mutating this one, which is what lets a node
    hold its entry version while producing later ones.
    """

    entries: dict[WorkspaceName, ArtifactRef] = Field(
        default_factory=dict,
        max_length=MAX_WORKSPACE_ENTRIES,
    )

    @property
    def total_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.entries.values())

    def names(self) -> tuple[str, ...]:
        """Every name, sorted.

        Sorted rather than insertion-ordered because a listing is shown to a
        model and recorded in events: two runs that wrote the same files in a
        different order should not read as two different workspaces.
        """

        return tuple(sorted(self.entries))

    def with_entry(self, name: str, ref: ArtifactRef) -> WorkspaceManifest:
        """Return the next version, with ``name`` bound to ``ref``.

        Replacing an existing name is not growth: neither ceiling counts the
        bytes or the slot it is giving up. Without that, a workspace that once
        held a large file could never be written to again even after the file
        was replaced -- the budget would ratchet rather than track.

        **Validated, not copied.** This used to end in ``model_copy``, which
        does not run validators, so a name ``WorkspaceName`` forbids went into
        the manifest, serialized, and committed without complaint -- and every
        subsequent ``load`` of that workspace raised, because reading *does*
        validate. Measured: a work node wrote ``季度总结.docx``, the write
        reported success, and from that moment every list, read, grep and
        write in that Task failed, including writes of perfectly legal names.
        The workspace was unrecoverable and the error pointed at a call that
        had already returned.

        A name is rejected here, where the caller can still act on it.
        """

        replaced = self.entries.get(name)
        if replaced is None and len(self.entries) >= MAX_WORKSPACE_ENTRIES:
            raise WorkspaceOverflowError(
                f"workspace already holds {MAX_WORKSPACE_ENTRIES} entries"
            )

        freed = replaced.size_bytes if replaced is not None else 0
        if self.total_bytes - freed + ref.size_bytes > MAX_WORKSPACE_TOTAL_BYTES:
            raise WorkspaceOverflowError(
                f"workspace would exceed {MAX_WORKSPACE_TOTAL_BYTES} bytes"
            )

        # `model_validate` rather than `model_copy`: the latter skips
        # validators, which is exactly how an unrepresentable name reached
        # durable state. Revalidating the whole map is bounded by
        # MAX_WORKSPACE_ENTRIES and happens once per write.
        return WorkspaceManifest.model_validate(
            {"entries": {**self.entries, name: ref}}
        )


__all__ = [
    "GREP_TIMEOUT_SECONDS",
    "MAX_GREP_LINE_CHARS",
    "MAX_GREP_MATCHES",
    "MAX_GREP_SCANNED_BYTES",
    "MAX_INLINE_READ_CHARS",
    "MAX_WORKSPACE_ENTRIES",
    "MAX_WORKSPACE_TOTAL_BYTES",
    "WORKSPACE_EDIT_TOOL",
    "WORKSPACE_GREP_TOOL",
    "WORKSPACE_LIST_TOOL",
    "WORKSPACE_READ_TOOL",
    "WORKSPACE_WRITE_SCOPE",
    "WORKSPACE_WRITE_TOOL",
    "GrepMatch",
    "GrepOutcome",
    "ReadWindow",
    "WorkspaceEditMatchError",
    "WorkspaceManifest",
    "WorkspaceName",
    "WorkspaceOverflowError",
    "WorkspacePatternError",
    "WorkspaceScanTimeoutError",
    "describe_read_window",
    "grep_workspace",
    "read_window",
    "replace_exactly_once",
]
