"""The tools that make a project's directory reachable (ADR-072, ADR-073).

Siblings of ``adapters/tools/workspace.py``, not replacements. A run gets one
set or the other and never both (ADR-073 §2): a model holding two "write a file"
tools whose descriptions differ only in a word it cannot check will eventually
put a draft meant for the working set into somebody's repository, and that
failure does not raise.

The names are distinct for the reason the sets are exclusive. A tool name is
frozen into the authorization envelope at submission (ADR-025), and that name is
what answers "what was this run allowed to do" for an envelope already signed.
If ``workspace_write`` could mean either *bind a name in a versioned manifest*
or *write a file on this machine's disk*, the envelope would have stopped
answering it.

Every path goes through the ``ProjectFileStore``, which goes through
``ProjectSandbox``. Nothing here interprets a path: the moment a tool started
joining or normalising one, the sandbox would stop being the single place those
rules live.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Final

from pydantic import JsonValue

from agent_workbench.application.project_file_scope import ProjectFileScope
from agent_workbench.domain.errors import (
    ErrorInfo,
    NotFoundError,
    OutputTooLargeError,
)
from agent_workbench.domain.project_files import (
    MAX_RELATIVE_PATH_BYTES,
    PROJECT_RUN_SCOPE,
    PROJECT_RUN_TOOL,
    ProjectPathError,
)
from agent_workbench.domain.tools import ToolResult, ToolSpec

# The matching engine, shared with `adapters/tools/workspace.py` rather than
# written twice. ADR-073 §2 makes the two *tool sets* exclusive, and this does
# not soften that: what is shared is a pure function over `(name, text)` pairs
# that performs no I/O and knows nothing about where a file came from. The
# decision it owns -- what counts as a match, and under which ceilings -- has
# one answer either way, and a second implementation of it would be a place for
# the two to disagree about what "no matches" means.
from agent_workbench.domain.workspace import (
    GREP_TIMEOUT_SECONDS,
    MAX_GREP_MATCHES,
    MAX_GREP_SCANNED_BYTES,
    GrepOutcome,
    WorkspacePatternError,
    WorkspaceScanTimeoutError,
    grep_workspace,
)
from agent_workbench.ports.project_files import (
    MAX_LISTING_ENTRIES,
    ProjectFileEntry,
    ProjectFileStore,
)
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

LIST_TOOL_NAME = "project_list"
READ_TOOL_NAME = "project_read"
WRITE_TOOL_NAME = "project_write"
EDIT_TOOL_NAME = "project_edit"
GREP_TOOL_NAME = "project_grep"

#: What a read may put into the model's context in one go, matching the
#: workspace tools' ceiling rather than inventing a second one. The bytes are
#: still on disk -- this is about the context budget, not about the file.
MAX_INLINE_READ_CHARS: Final[int] = 48_000

#: What one write may accept inline. Same value and same reason as the
#: workspace tool's: a model that needs to produce more than this is producing
#: something it should be building in pieces.
MAX_INLINE_WRITE_CHARS: Final[int] = 96_000

_PATH_SCHEMA: dict[str, JsonValue] = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_RELATIVE_PATH_BYTES,
    "description": (
        "Path relative to the project root, using forward slashes. "
        "Absolute paths and '..' segments are refused."
    ),
}


class ProjectFilesUnavailableError(RuntimeError):
    """A project tool ran outside a turn that entered a store."""


def _store(scope: ProjectFileScope) -> ProjectFileStore:
    """The store this turn entered, or a refusal.

    An unentered scope is not a reason to open one. The only root this function
    could pick is one nobody registered, and writing a model's output into a
    directory the user never chose is the single worst thing this subsystem
    could do.
    """

    store = scope.current()
    if store is None:
        raise ProjectFilesUnavailableError(
            "no project directory is entered for this turn"
        )
    return store


def _refusal(invocation: ToolInvocation, error: Exception) -> ToolResult:
    """Turn a store or sandbox refusal into a result the model can act on.

    The sandbox's own message is passed through. It names *which* rule refused
    -- left the root, followed a symlink, not a file -- and a model that is told
    only "invalid path" retries the same path, while one told "'..' segment" does
    not. The message contains nothing the caller did not send: it is built from
    the path the model itself supplied.
    """

    if isinstance(error, ProjectPathError):
        return ToolResult.failed(
            invocation.call,
            ErrorInfo(code="invalid_tool_input", message=str(error), retryable=False),
        )
    if isinstance(error, NotFoundError):
        return ToolResult.failed(
            invocation.call,
            ErrorInfo(code="not_found", message=str(error), retryable=False),
        )
    return ToolResult.failed(
        invocation.call,
        ErrorInfo(code="output_too_large", message=str(error), retryable=False),
    )


@dataclass(frozen=True, slots=True)
class ProjectListTool:
    """What is in the project directory, without reading any of it."""

    scope: ProjectFileScope

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=LIST_TOOL_NAME,
            description=(
                "List files in this project's directory. Give 'path' to list a "
                "subdirectory, or omit it for the project root. Set 'recursive' "
                "to see every file underneath. Generated directories (.git, "
                "node_modules, __pycache__, .venv) are never listed."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": _PATH_SCHEMA,
                    "recursive": {"type": "boolean"},
                },
            },
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=30,
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        path = str(arguments.get("path") or "")
        recursive = bool(arguments.get("recursive") or False)
        store = _store(self.scope)
        try:
            listing = (
                await store.walk(path)
                if recursive
                else await store.list_directory(path)
            )
        except (ProjectPathError, NotFoundError) as error:
            return _refusal(invocation, error)
        if not listing.entries:
            return ToolResult.succeeded(
                invocation.call, content=f"{path or 'the project root'} is empty."
            )
        lines = [
            f"{entry.path}/"
            if entry.kind == "directory"
            else f"{entry.path}\t{entry.size_bytes} bytes"
            for entry in listing.entries
        ]
        if listing.truncated:
            # Said in the content, not only in the DTO. The model is the caller
            # here, and a truncated listing it believes is complete is how it
            # concludes a file does not exist and writes a second one.
            lines.append(
                f"... listing stopped at {len(listing.entries)} entries; "
                "ask for a subdirectory to see the rest."
            )
        return ToolResult.succeeded(invocation.call, content="\n".join(lines))


@dataclass(frozen=True, slots=True)
class ProjectReadTool:
    """One file out of the project directory."""

    scope: ProjectFileScope

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=READ_TOOL_NAME,
            description=(
                "Read one text file from this project's directory by its path "
                "relative to the project root."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {"path": _PATH_SCHEMA},
            },
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=30,
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        path = str(invocation.call.arguments.get("path", ""))
        store = _store(self.scope)
        try:
            content = await store.read(path)
        except (ProjectPathError, NotFoundError, OutputTooLargeError) as error:
            return _refusal(invocation, error)
        if not content.is_text:
            # Told, not handed. A binary file decoded with replacement is a
            # string of U+FFFD that a model reads as text and then edits, and
            # the edit destroys the file.
            return ToolResult.succeeded(
                invocation.call,
                content=(
                    f"{path} is not a text file ({content.size_bytes} bytes). "
                    "Its contents are not shown."
                ),
            )
        text = content.text or ""
        if len(text) > MAX_INLINE_READ_CHARS:
            return ToolResult.succeeded(
                invocation.call,
                content=(
                    f"{path} is {content.size_bytes} bytes, too long to show in "
                    f"full. First {MAX_INLINE_READ_CHARS} characters:\n\n"
                    + text[:MAX_INLINE_READ_CHARS]
                ),
            )
        return ToolResult.succeeded(invocation.call, content=text)


@dataclass(frozen=True, slots=True)
class ProjectWriteTool:
    """Create or replace one file in the project directory."""

    scope: ProjectFileScope

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=WRITE_TOOL_NAME,
            description=(
                "Write a text file into this project's directory, replacing it "
                "if the path is already taken. Parent directories are created. "
                "The path is relative to the project root; absolute paths and "
                "'..' segments are refused. This writes to the user's real "
                "files."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {
                    "path": _PATH_SCHEMA,
                    "content": {"type": "string", "maxLength": MAX_INLINE_WRITE_CHARS},
                },
            },
            concurrency="exclusive",
            risk="write",
            idempotency="safe",
            timeout_seconds=60,
            # The same scope the flat workspace write requires. A deployment
            # that granted one and not the other would be drawing a distinction
            # between two ways of writing a file that the person granting it
            # never made.
            permission_scopes=("workspace:write",),
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        path = str(arguments.get("path", ""))
        content = str(arguments.get("content", ""))
        store = _store(self.scope)
        try:
            entry = await store.write(path, content)
        except (ProjectPathError, NotFoundError, OutputTooLargeError) as error:
            return _refusal(invocation, error)
        return ToolResult.succeeded(
            invocation.call,
            content=f"Wrote {entry.path} ({entry.size_bytes} bytes).",
        )


@dataclass(frozen=True, slots=True)
class ProjectEditTool:
    """Replace one exact occurrence in one file."""

    scope: ProjectFileScope

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=EDIT_TOOL_NAME,
            description=(
                "Replace an exact snippet in one file of this project's "
                "directory. The snippet must appear exactly once; if it appears "
                "zero times or more than once the edit is refused, so include "
                "enough surrounding lines to make it unique."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "find", "replace"],
                "properties": {
                    "path": _PATH_SCHEMA,
                    "find": {"type": "string", "minLength": 1},
                    "replace": {"type": "string"},
                },
            },
            concurrency="exclusive",
            risk="write",
            idempotency="safe",
            timeout_seconds=60,
            permission_scopes=("workspace:write",),
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        path = str(arguments.get("path", ""))
        find = str(arguments.get("find", ""))
        replace = str(arguments.get("replace", ""))
        store = _store(self.scope)
        try:
            current = await store.read(path)
        except (ProjectPathError, NotFoundError, OutputTooLargeError) as error:
            return _refusal(invocation, error)
        if not current.is_text:
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message=f"{path} is not a text file",
                    retryable=False,
                ),
            )
        text = current.text or ""
        occurrences = text.count(find)
        if occurrences != 1:
            # Exactly once, refused otherwise -- the same rule
            # `replace_exactly_once` enforces for the flat workspace. Zero means
            # the model is editing a file it has misremembered; more than one
            # means it cannot know which occurrence it changed, and "the first"
            # is a guess dressed as a result.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message=(
                        f"the snippet appears {occurrences} times in {path}; "
                        "it must appear exactly once"
                    ),
                    retryable=False,
                ),
            )
        try:
            entry = await store.write(path, text.replace(find, replace, 1))
        except (ProjectPathError, NotFoundError, OutputTooLargeError) as error:
            return _refusal(invocation, error)
        return ToolResult.succeeded(
            invocation.call,
            content=f"Edited {entry.path} ({entry.size_bytes} bytes).",
        )


@dataclass(frozen=True, slots=True)
class _Corpus:
    """What one search got to read, and the three ways it did not.

    Four outcomes rather than "the files, and a truncated flag", because the
    four are different sentences to put in front of a model. "That file is a
    PNG", "that file is 6 MB", and "I ran out of budget three files earlier"
    are all reasons a match was not reported, and only the last one is worth
    spending the next turn re-asking in a narrower form.
    """

    files: tuple[tuple[str, str], ...]
    binary: tuple[str, ...]
    too_large: tuple[str, ...]
    unread: tuple[str, ...]


async def _read_corpus(
    store: ProjectFileStore, candidates: Sequence[ProjectFileEntry]
) -> _Corpus:
    """Read as much of ``candidates`` as the budget allows, in a fixed order.

    Sorted by path before anything is read, and that is a correctness property
    rather than tidiness. When the budget runs out, *which* files went
    unsearched is part of this tool's answer; ``walk`` returns them in whatever
    order ``os.walk`` produced, so two identical searches over an unchanged
    directory could otherwise name different files as unsearched and a model
    comparing the two would be reading a difference that is not there.

    The ceiling is ``MAX_GREP_SCANNED_BYTES``, applied to *reading* rather than
    left to ``grep_workspace``. On the workspace that function guards a manifest
    the session already holds in memory, so its budget bounds matching alone; on
    a real tree every byte it scans has to come off disk first, one offloaded
    read per file, and by the time it could decline a file the read has already
    happened. Bounding the read is the only place this ceiling does any work
    here, which is also why the function's own check never fires on this path.
    """

    files: list[tuple[str, str]] = []
    binary: list[str] = []
    too_large: list[str] = []
    unread: list[str] = []
    budget = MAX_GREP_SCANNED_BYTES

    ordered = sorted(candidates, key=lambda entry: entry.path)
    for index, entry in enumerate(ordered):
        size = entry.size_bytes
        if size is not None and size > budget:
            # Every remaining file, named -- the same rule `grep_workspace`
            # follows when it stops early. A model told only that the search was
            # incomplete cannot tell whether the file it actually cares about
            # was among the ones that got read.
            unread.extend(other.path for other in ordered[index:])
            break
        try:
            content = await store.read(entry.path)
        except OutputTooLargeError:
            too_large.append(entry.path)
            continue
        except NotFoundError:
            # Walked a moment ago, gone now. Named as unread rather than raised:
            # a file deleted mid-search is not a reason to fail the search over
            # everything else, but it is a reason not to claim it was searched.
            unread.append(entry.path)
            continue
        text = content.text
        if not content.is_text or text is None:
            binary.append(entry.path)
            continue
        if "\x00" in text:
            # Valid UTF-8 and still not something to quote back. The store
            # decodes strictly, so a NUL byte survives as U+0000 with `is_text`
            # true; a matched line carrying one reaches the model prompt, the
            # prompt is recorded in a `ModelStarted` event, and PostgreSQL
            # refuses the write outright -- `UntranslatableCharacterError:
            # \u0000 cannot be converted to text` -- taking down a run that
            # had
            # done nothing wrong except search a directory containing a `.mo`
            # file. `adapters/tools/workspace.py` sniffs only the first 8 KiB
            # for this byte because scanning a 64 MB working set end to end to
            # answer a header question is not worth it; here the read budget
            # above has already bounded the text to 8 MiB and it is in memory,
            # so the exact test costs one more pass over bytes that were about
            # to be matched anyway.
            binary.append(entry.path)
            continue
        files.append((entry.path, text))
        budget -= content.size_bytes

    return _Corpus(
        files=tuple(files),
        binary=tuple(binary),
        too_large=tuple(too_large),
        unread=tuple(unread),
    )


def _unsearched(
    *, outcome: GrepOutcome, corpus: _Corpus, walk_truncated: bool
) -> list[str]:
    """Every reason this answer is not exhaustive, one line each.

    Assembled once and appended to *both* renderings, including -- especially --
    the one with nothing to show. A model that reads a bare "No matches."
    concludes the string is not in the project and stops opening files, which is
    the exact failure `CODE_PROJECT_TOOLS` withheld this tool to avoid. It is a
    false negative rather than a missing feature: silent, plausible, and wrong,
    and the model has no way to discover it was told something untrue.
    """

    notes: list[str] = []
    if outcome.more_matches:
        notes.append(f"... stopped at {MAX_GREP_MATCHES} matches; there are more.")
    if walk_truncated:
        notes.append(
            f"... the walk stopped at {MAX_LISTING_ENTRIES} files; give 'path' "
            "to search a subdirectory and reach the rest."
        )
    skipped = (*outcome.unscanned_files, *corpus.unread)
    if skipped:
        notes.append("... not searched: " + ", ".join(skipped))
    if corpus.binary:
        notes.append("... not text, so not searched: " + ", ".join(corpus.binary))
    if corpus.too_large:
        notes.append("... too large to search: " + ", ".join(corpus.too_large))
    return notes


@dataclass(frozen=True, slots=True)
class ProjectGrepTool:
    """Where a pattern occurs across the project's real files."""

    scope: ProjectFileScope
    #: Injected so a test can drive the clock, matching the workspace tool. The
    #: scan's budget is wall-clock over the whole match pass, not per line.
    monotonic: Callable[[], float] = time.monotonic

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=GREP_TOOL_NAME,
            description=(
                "Search this project's files for lines matching a regular "
                "expression and return where they are. Give 'path' to search "
                "one subdirectory instead of the whole project, and 'name_glob' "
                "to restrict it to matching paths such as '*.py' -- the glob is "
                "matched against the whole project-relative path, so '*' "
                "crosses directories. Generated directories (.git, node_modules, "
                "__pycache__, .venv) are never searched. Results are capped, and "
                "the reply names every file that was not searched."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["pattern"],
                "properties": {
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1024,
                        "description": "A regular expression.",
                    },
                    "path": _PATH_SCHEMA,
                    "name_glob": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": (
                            "Shell-style glob over the project-relative path, "
                            "e.g. '*.py'."
                        ),
                    },
                },
            },
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            # 30s, the same as its `project_read` and `project_list` siblings.
            # Measured 2026-08-24 over this repository's own `src` tree, 305
            # files and 2.8 MB: 0.09s for a pattern that hits the 100-match cap
            # early, 0.11s for one that matches nothing and therefore reads
            # every file. Two orders of magnitude of headroom, and that is the
            # point of leaving it at the sibling value rather than tightening
            # it -- the work this bounds is one `walk`, at most
            # `MAX_GREP_SCANNED_BYTES` off disk, and `GREP_TIMEOUT_SECONDS` of
            # matching, and only the middle term can grow. What would actually
            # move this number is a project on a network mount, where the reads
            # dominate and none of the three ceilings above would notice.
            timeout_seconds=30,
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        pattern = str(arguments.get("pattern", ""))
        path = str(arguments.get("path") or "")
        name_glob = arguments.get("name_glob")
        store = _store(self.scope)

        try:
            listing = await store.walk(path)
        except (ProjectPathError, NotFoundError) as error:
            return _refusal(invocation, error)

        candidates = [
            entry
            for entry in listing.entries
            if name_glob is None or fnmatch(entry.path, str(name_glob))
        ]
        if not candidates:
            # Distinguished from "no matches" on purpose. Nothing was searched
            # because nothing was there to search, and a model told "No matches"
            # would take that as evidence about the pattern rather than about
            # its own `path` or `name_glob`.
            where = path or "the project root"
            narrowed = "" if name_glob is None else f" matching {name_glob}"
            return ToolResult.succeeded(
                invocation.call,
                content=f"No files under {where}{narrowed} to search.",
            )

        corpus = await _read_corpus(store, candidates)
        try:
            outcome = grep_workspace(corpus.files, pattern, now=self.monotonic)
        except WorkspacePatternError as error:
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message=f"pattern is not a valid regular expression: {error}",
                    retryable=False,
                ),
            )
        except WorkspaceScanTimeoutError:
            # Not retryable, for the reason its workspace twin gives: the same
            # pattern over the same tree will time out again, and retrying is
            # what a model does with a transient error.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="tool_timeout",
                    message=(
                        f"searching took longer than {GREP_TIMEOUT_SECONDS} "
                        "seconds and was stopped; try a simpler pattern"
                    ),
                    retryable=False,
                ),
            )

        notes = _unsearched(
            outcome=outcome, corpus=corpus, walk_truncated=listing.truncated
        )
        if not outcome.matches:
            return ToolResult.succeeded(
                invocation.call, content="\n".join(["No matches.", *notes])
            )
        lines = [
            f"{match.name}:{match.line_number}: {match.line}"
            for match in outcome.matches
        ]
        return ToolResult.succeeded(
            invocation.call, content="\n".join([*lines, *notes])
        )


#: How long one command may run before it is killed.
#:
#: 120s. Argued from the two ends it sits between rather than rounded: the
#: turn that contains it is 360s in `config.code-local.toml`, and the longest
#: command this repository's own gate runs is `uv run pytest`, measured
#: 2026-08-24 at 71s for 2811 tests. A ceiling under that would make the tool
#: useless for the one command a coding agent most wants to run; a ceiling near
#: the turn's own would let a single hung command consume the turn and leave
#: nothing to report it with. The spec's `timeout_seconds` is set higher so
#: this clock fires first -- a killed command can say what it printed before it
#: died, and `tool_timeout` from the executor cannot.
RUN_TIMEOUT_SECONDS: Final[float] = 120.0

#: How much output is read before the command is killed for producing too much.
#:
#: Reading stops here rather than growing a list until the process exits: a
#: command like `yes` fills memory in seconds, and the wall clock above is
#: 120 of them. Once reading stops the pipe fills and the command blocks, so
#: the kill is not optional -- it is what turns "we stopped listening" into
#: "it stopped talking".
MAX_CAPTURE_BYTES: Final[int] = 1024 * 1024

#: How much of what was captured reaches the model, matching `sandbox_run`'s
#: inline ceiling and its marker rather than inventing a second convention.
MAX_INLINE_OUTPUT_CHARS: Final[int] = 8_000


def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill the command and everything it started.

    The process *group*, not the process. A shell command runs under ``/bin/sh
    -c``, so the thing that actually matters -- the ``pytest``, the ``npm``,
    the dev server -- is a child of what ``process.kill()`` would reach.
    Killing only the shell leaves that child alive and reparented, still
    holding the pipe this call was reading from and whatever port it had bound,
    with nothing left in the system that knows it exists.
    ``start_new_session=True`` at spawn is what makes a group exist to be
    killed.

    ``SIGKILL`` rather than a term-then-kill pair. Both paths that reach here
    have already spent their budget -- the clock ran out, or the output ceiling
    was passed and the pipe is full -- and a grace period is time taken from a
    turn that has none left to give. The cost is a command that cannot clean up
    after itself, which is the cost of every timeout.
    """

    if process.returncode is not None:
        return
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


async def _capture(process: asyncio.subprocess.Process, chunks: list[bytes]) -> bool:
    """Read up to the ceiling into ``chunks``, and say whether there was more.

    The accumulator belongs to the caller rather than to this function, and
    that is the whole reason for the signature. A command that runs past the
    clock is cancelled *inside this loop*, and a version that built the buffer
    locally and returned it would lose every byte it had already read -- which
    is exactly the output worth having. A `pytest` that printed three failures
    and then hung is a far more useful answer than "it did not finish".
    """

    assert process.stdout is not None
    total = 0
    while True:
        chunk = await process.stdout.read(65_536)
        if not chunk:
            return False
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_CAPTURE_BYTES:
            return True


def _rendered(text: str) -> str:
    """The output as far as it fits, marked where it was cut."""

    if len(text) <= MAX_INLINE_OUTPUT_CHARS:
        return text
    return (
        f"[{len(text)} characters; first {MAX_INLINE_OUTPUT_CHARS} shown]\n"
        + text[:MAX_INLINE_OUTPUT_CHARS]
    )


@dataclass(frozen=True, slots=True)
class ProjectRunTool:
    """One command, run in the project's directory, on this machine (ADR-077)."""

    scope: ProjectFileScope
    #: What the command inherits, decided in `bootstrap/child_environment.py`
    #: and handed here already made. Not read from `os.environ` in this module:
    #: `tests/architecture/test_dependency_boundaries.py` allows that in
    #: `bootstrap` and nowhere else, and the rule is right -- a tool whose
    #: behaviour depends on a variable nobody passed it is a tool whose
    #: behaviour cannot be read off the configuration.
    environment: Mapping[str, str]
    #: Injected so a test can drive it down to something a test can wait for.
    timeout_seconds: float = RUN_TIMEOUT_SECONDS

    def binding(self) -> ToolBinding:
        # No `operation_key`, and that is not an oversight. A key would put this
        # in the execution ledger, and `ToolBinding.__post_init__` refuses one
        # paired with `idempotency="safe"`; more to the point, ADR-075's
        # `advertise` guardrail refuses to offer *any* keyed binding to a
        # model, and the Code gateway is built with no ledger at all, so a keyed
        # Code tool stops the API process from assembling. What keeps a command
        # from running twice here is that a Code turn is never replayed
        # (`application/code_session.py`) and that every call stops at a human.
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=PROJECT_RUN_TOOL,
            description=(
                "Run one shell command in this project's directory, on the "
                "machine this server runs on. It is the user's real machine: "
                "there is no sandbox and no undo, and every call stops and asks "
                "them first. Output is stdout and stderr interleaved in the "
                "order they were written, capped, and marked where it was cut; "
                "a non-zero exit code is reported, not treated as a failure. "
                "The command cannot read input -- anything that prompts will "
                f"hang until it is killed at {int(RUN_TIMEOUT_SECONDS)} seconds."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["command"],
                "properties": {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4_000,
                        "description": (
                            "A shell command, run by /bin/sh in the project "
                            "root. Pipes, && and redirection work."
                        ),
                    }
                },
            },
            concurrency="exclusive",
            risk="destructive",
            idempotency="safe",
            # Above `RUN_TIMEOUT_SECONDS` so this is the backstop and not the
            # clock that fires. The one below can report what the command
            # printed before it was killed; this one only cancels the handler.
            timeout_seconds=int(RUN_TIMEOUT_SECONDS) + 30,
            permission_scopes=(PROJECT_RUN_SCOPE,),
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        command = str(invocation.call.arguments.get("command", ""))
        store = _store(self.scope)
        await invocation.progress("running the command")

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=store.working_directory,
            stdout=asyncio.subprocess.PIPE,
            # One stream, in the order the command wrote it. `sandbox_run`
            # keeps the two apart because its caller reads them apart; here the
            # thing being read is a terminal session, and a test runner's
            # failing assertion and the line naming the test it belongs to go
            # to different channels. Separated, they arrive as two lists nobody
            # can re-interleave.
            stderr=asyncio.subprocess.STDOUT,
            # Nothing to read. A command that prompts would otherwise wait on a
            # human who is not there, look identical to a slow one, and be
            # killed by the clock with no output explaining why.
            stdin=asyncio.subprocess.DEVNULL,
            env=dict(self.environment),
            start_new_session=True,
        )
        chunks: list[bytes] = []
        overflowed = False
        timed_out = False
        exit_code: int | None = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                overflowed = await _capture(process, chunks)
                if overflowed:
                    # Before waiting, not after. Reading stopped at the ceiling,
                    # so the pipe is full and the command is blocked writing
                    # into it -- `wait()` here would be waiting for something
                    # that is waiting for us.
                    _terminate(process)
                exit_code = await process.wait()
        except TimeoutError:
            timed_out = True
        finally:
            # Also on cancellation. The executor races this handler against the
            # run's cancellation token and cancels the loser, and a cancelled
            # turn that left a build running is the failure this clause exists
            # for.
            _terminate(process)

        captured = b"".join(chunks)[:MAX_CAPTURE_BYTES]
        output = _rendered(captured.decode("utf-8", errors="replace"))
        if timed_out:
            # The bytes it managed to print, carried on the error rather than
            # dropped. This is why the clock here is lower than the spec's: the
            # executor's `tool_timeout` cancels the handler and has nothing to
            # say, while a command killed by this one has usually already
            # printed the interesting part -- three failing tests and then a
            # hang is a different problem from a hang, and only one of the two
            # answers tells the model which it is.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="tool_timeout",
                    message=(
                        f"the command did not finish within "
                        f"{int(self.timeout_seconds)} seconds and was killed"
                        + (f"; it had printed:\n{output}" if output else "")
                    ),
                    retryable=False,
                ),
            )
        lines = [f"exit code: {exit_code}"]
        if output:
            lines.append(output)
        if overflowed:
            lines.append(
                f"[the command was killed after producing more than "
                f"{MAX_CAPTURE_BYTES} bytes]"
            )
        return ToolResult.succeeded(
            invocation.call,
            # A non-zero exit is a result, not a failure. The traceback, the
            # failing assertion, the compiler's line number -- those are the
            # payload, and a `ToolResult.failed` would hand the model an error
            # code where the answer it asked for is.
            content="\n".join(lines),
        )


__all__ = [
    "EDIT_TOOL_NAME",
    "GREP_TOOL_NAME",
    "LIST_TOOL_NAME",
    "MAX_CAPTURE_BYTES",
    "MAX_INLINE_OUTPUT_CHARS",
    "READ_TOOL_NAME",
    "RUN_TIMEOUT_SECONDS",
    "WRITE_TOOL_NAME",
    "ProjectEditTool",
    "ProjectFilesUnavailableError",
    "ProjectGrepTool",
    "ProjectListTool",
    "ProjectReadTool",
    "ProjectRunTool",
    "ProjectWriteTool",
]
