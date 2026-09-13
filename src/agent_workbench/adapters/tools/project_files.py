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

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Final

from pydantic import JsonValue

from agent_workbench.adapters.filesystem.commands import (
    MAX_CAPTURE_BYTES,
    MAX_INLINE_OUTPUT_CHARS,
    RUN_TIMEOUT_SECONDS,
    LocalCommandRunner,
    render_output,
)
from agent_workbench.adapters.tools.reading import (
    LIMIT_SCHEMA,
    OFFSET_SCHEMA,
    windowed_result,
)
from agent_workbench.adapters.tools.runner import RunnerRefusedError
from agent_workbench.application.file_read_receipts import ReadReceipts
from agent_workbench.application.project_file_scope import ProjectFileScope
from agent_workbench.domain.errors import (
    AgentWorkbenchError,
    ErrorInfo,
    NotFoundError,
    OutputTooLargeError,
    ToolFailedError,
)
from agent_workbench.domain.project_files import (
    MAX_RELATIVE_PATH_BYTES,
    PROJECT_RUN_SCOPE,
    PROJECT_RUN_TOOL,
    ProjectFileChangedError,
    ProjectFileExistsError,
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
    WORKSPACE_WRITE_SCOPE,
    GrepOutcome,
    WorkspacePatternError,
    WorkspaceScanTimeoutError,
    grep_workspace,
)
from agent_workbench.ports.commands import CommandRunner
from agent_workbench.ports.project_files import (
    MAX_LISTING_ENTRIES,
    ProjectFileEntry,
    ProjectFileStore,
    ProjectFileVersion,
    ProjectListing,
)
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

LIST_TOOL_NAME = "project_list"
READ_TOOL_NAME = "project_read"
WRITE_TOOL_NAME = "project_write"
EDIT_TOOL_NAME = "project_edit"
GREP_TOOL_NAME = "project_grep"
DELETE_TOOL_NAME = "project_delete"
MOVE_TOOL_NAME = "project_move"

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


class ProjectFilesUnavailableError(ToolFailedError):
    """A project tool ran outside a turn that entered a store.

    Derives from `ToolFailedError` rather than `RuntimeError` so that the
    sentence survives. `ErrorInfo.from_exception` passes a message through only
    for `AgentWorkbenchError`; everything else becomes `unhandled <ClassName>`,
    on the reasoning that a third-party message is untrusted content of unknown
    provenance. That reasoning does not cover a string written on the line
    below, and the model is the reader here: handed the class name, it has
    nothing to put in its report but the class name.
    """


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


def _record_written(
    receipts: ReadReceipts, entry: ProjectFileEntry, *, covers_whole_file: bool
) -> None:
    """Note the file as this turn now sees it, having just written it.

    Without this, the second write to a file the model wrote itself is refused
    by its own gate: the receipt still describes the file as it was read, and
    the write it just made moved both the size and the mtime. The values come
    from the entry the store returned rather than from a read-back, which is
    what `ProjectFileStore.write` returns an entry *for*.

    ``covers_whole_file`` is a parameter and not a constant because the two
    write paths differ, and getting that wrong opened the gate completely.
    `project_write` was handed the entire content, so the model has seen the
    whole file by construction. `project_edit` was handed a snippet: the store
    read the file, not the model. Minting a whole-file receipt there let two
    calls launder an overwrite -- edit one unique string in a 30 KB file nobody
    read, then `project_write` over all of it, with the gate's approval. So an
    edit *carries forward* whatever coverage the model already had and never
    manufactures more (see `_coverage_after_edit`).
    """

    receipts.record(
        entry.path,
        # A file always has a size. The `None` arm of `ProjectFileEntry.
        # size_bytes` is the directory case, and `write` cannot produce one.
        size_bytes=entry.size_bytes if entry.size_bytes is not None else 0,
        modified_at=entry.modified_at,
        covers_whole_file=covers_whole_file,
    )


def _stale(invocation: ToolInvocation, message: str) -> ToolResult:
    """Refuse a write because the model's picture of the file is not current.

    `invalid_tool_input`, the same code `project_edit` already answers when the
    snippet it was told to replace is not there -- one event, one code. What
    makes this refusal unusual is that retrying *is* the answer, after a read,
    and discipline 3 of the coding prompt says the opposite about refusals in
    general ("retrying the same call with the same arguments cannot succeed").
    That is true here too -- the same call *would* fail again -- so every
    message this carries has to name the read that makes the retry different.
    """

    return ToolResult.failed(
        invocation.call,
        ErrorInfo(code="invalid_tool_input", message=message, retryable=False),
    )


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
    #: Where this tool leaves the record `ProjectWriteTool` reads (ADR-0078).
    #: A required field with no default, because a tool holding a private
    #: ledger nobody consults would record every read into nothing and let
    #: every overwrite through, while the transcript showed a gate.
    receipts: ReadReceipts

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=READ_TOOL_NAME,
            description=(
                "Read one text file from this project's directory by its path "
                "relative to the project root. Omit 'offset' and 'limit' unless "
                "you want one specific region: without them a read brings back "
                "everything up to a large ceiling, and only a file past that "
                "ceiling comes back as a window. The reply says which lines it "
                "gave you, why it stopped, and which offset continues."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": _PATH_SCHEMA,
                    "offset": OFFSET_SCHEMA,
                    "limit": LIMIT_SCHEMA,
                },
            },
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=30,
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        path = str(arguments.get("path", ""))
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
        # What the model had already seen of this file, before this read. A
        # narrower read of an unchanged file must not *lower* the coverage a
        # wider one earned: re-reading a region before rewriting it is the most
        # ordinary check a coding agent makes, and downgrading the receipt for
        # it refuses a model that is strictly better informed than the one that
        # was allowed through. Guarded on the file not having moved -- if size
        # or mtime differ, the earlier read describes a file that is gone and
        # this window's coverage is the true one.
        carried = self.receipts.seen(path)
        already_whole = (
            carried is not None
            and carried.covers_whole_file
            and carried.size_bytes == content.size_bytes
            and carried.modified_at == content.modified_at
        )
        text = content.text or ""
        if not text:
            # Said, not returned empty. An empty tool message reads to a model
            # as a call that was ignored, so it sends the same one again --
            # and `MAX_IDENTICAL_CALLS` ends the turn on the third. `project_
            # list` and `project_grep` both already had this branch; read did
            # not, and a zero-byte `__init__.py` is not an unusual thing to
            # open in a Python project.
            #
            # A receipt, and a whole-file one: there is nothing in this file
            # the model has not seen. Withholding one would fire the write gate
            # on the single case where the model is provably up to date.
            self.receipts.record(
                path,
                size_bytes=content.size_bytes,
                modified_at=content.modified_at,
                covers_whole_file=True,
            )
            return ToolResult.succeeded(invocation.call, content=f"{path} is empty.")
        return windowed_result(
            invocation,
            label=path,
            text=text,
            arguments=arguments,
            note_read=lambda whole: self.receipts.record(
                path,
                size_bytes=content.size_bytes,
                modified_at=content.modified_at,
                covers_whole_file=whole or already_whole,
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectWriteTool:
    """Create or replace one file in the project directory."""

    scope: ProjectFileScope
    #: The record `ProjectReadTool` leaves (ADR-0078). Required, not defaulted:
    #: a private ledger would make this gate pass every time while looking in
    #: the transcript exactly like a gate that checked.
    receipts: ReadReceipts

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=WRITE_TOOL_NAME,
            description=(
                "Write a text file into this project's directory, replacing it "
                "if the path is already taken. With 'append' true the content "
                "is added to the end of the file instead (the file is created "
                "if it is not there) -- use that to write a large file in "
                "pieces, a few hundred lines per call: a whole file sent in "
                "one call is cut off at the model's output ceiling, and then "
                "nothing is written. Parent directories are created. The path "
                "is relative to the project root; absolute paths and '..' "
                "segments are refused. This writes to the user's real files."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {
                    "path": _PATH_SCHEMA,
                    "content": {"type": "string", "maxLength": MAX_INLINE_WRITE_CHARS},
                    "append": {"type": "boolean"},
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
            permission_scopes=(WORKSPACE_WRITE_SCOPE,),
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        path = str(arguments.get("path", ""))
        content = str(arguments.get("content", ""))
        append = bool(arguments.get("append") or False)
        store = _store(self.scope)
        try:
            replacing = await store.exists(path)
        except ProjectPathError as error:
            return _refusal(invocation, error)
        if replacing and append:
            return await self._append(invocation, store, path, content)
        # Only a replacement is gated. Creating a file destroys nothing, and
        # requiring a read of a path that is not there would refuse the most
        # ordinary thing a coding agent does -- with a sentence ("read it
        # first") that names an impossible action.
        precondition: ProjectFileVersion | None = None
        if replacing:
            receipt = self.receipts.seen(path)
            if receipt is None:
                return _stale(
                    invocation,
                    f"{path} already exists and you have not read it in this "
                    "turn. This call would replace all of it. Read it first, "
                    "then write.",
                )
            if not receipt.covers_whole_file:
                # "Read the rest, the read told you which offset continues" is
                # what this said first, and it is the one move that never
                # works: coverage comes from a window that starts at line 1 and
                # reaches the end, so following the offset replaces a head
                # receipt with a tail receipt and the same refusal comes back.
                # Above `MAX_INLINE_READ_CHARS` no single read can be whole at
                # all -- a file that long cannot be replaced wholesale by this
                # tool, which is the gate working rather than failing, so the
                # sentence names the tool that does not need a full read.
                return _stale(
                    invocation,
                    f"you have seen only part of {path}, and this call would "
                    "replace all of it. Read it again with no offset and no "
                    "limit to see it whole; if it is too long to arrive in one "
                    "read, use project_edit, which changes only the snippet "
                    "you name and needs no full read.",
                )
            precondition = ProjectFileVersion(
                size_bytes=receipt.size_bytes, modified_at=receipt.modified_at
            )
        try:
            entry = await store.write(
                path,
                content,
                if_unchanged=precondition,
                # The `exists` above is a hop older than this write, and the
                # user's editor saves into windows like that -- the same
                # argument that put `if_unchanged` inside the store rather than
                # here. Without this, a file that appeared in between is
                # replaced unconditionally, having never been read.
                create_only=not replacing,
            )
        except ProjectFileExistsError as error:
            return _stale(
                invocation,
                f"{error} It was not there when this call started, so nothing "
                "in this turn has seen it. Read it before writing it.",
            )
        except ProjectFileChangedError as error:
            return _stale(invocation, f"{error} {self._who_changed_it()}")
        except (ProjectPathError, NotFoundError, OutputTooLargeError) as error:
            return _refusal(invocation, error)
        # The model supplied every byte of this file, so it has seen all of it.
        _record_written(self.receipts, entry, covers_whole_file=True)
        return ToolResult.succeeded(
            invocation.call,
            content=f"Wrote {entry.path} ({entry.size_bytes} bytes).",
            # The sentence above is for the model; this is for everything else
            # (ADR-086). `entry.path`, not the argument the model supplied:
            # the store normalises what it was given, and a console that
            # refetched the argument's spelling would ask for a file under a
            # name the directory does not use.
            project_writes=(entry.path,),
        )

    async def _append(
        self,
        invocation: ToolInvocation,
        store: ProjectFileStore,
        path: str,
        content: str,
    ) -> ToolResult:
        """Add to the end of a file that is there (ADR-0118).

        The piece-wise write. A model whose output ceiling is smaller than
        the file it is asked for -- 8192 tokens against a 42 KB page, three
        runs in a row on 2026-09-13 -- has no way to land that file through
        a tool that takes the whole body at once, and `project_edit` is the
        wrong shape for it: an edit names a snippet to replace, and "the end
        of the file" is not a snippet the model can name without reading the
        file back after every piece.

        Not gated on a receipt, on the reasoning `project_edit` uses: this
        tool reads the file itself, one statement earlier, so it is never
        working from a stale copy, and the write is fenced by `if_unchanged`
        from that read. What the receipt does decide is the *coverage* the
        model is left with afterwards, and it is carried rather than minted:
        a file the model created this turn and has only ever appended to is
        one it has seen whole, so a later `project_write` over it is allowed;
        a file it never read and appended to once is not.
        """

        carried = self.receipts.seen(path)
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
        try:
            entry = await store.write(
                path,
                (current.text or "") + content,
                if_unchanged=ProjectFileVersion(
                    size_bytes=current.size_bytes,
                    modified_at=current.modified_at,
                ),
            )
        except ProjectFileChangedError as error:
            return _stale(invocation, f"{error} {self._who_changed_it()}")
        except (ProjectPathError, NotFoundError, OutputTooLargeError) as error:
            return _refusal(invocation, error)
        _record_written(
            self.receipts,
            entry,
            covers_whole_file=carried is not None and carried.covers_whole_file,
        )
        return ToolResult.succeeded(
            invocation.call,
            content=f"Appended to {entry.path} ({entry.size_bytes} bytes now).",
            project_writes=(entry.path,),
        )

    def _who_changed_it(self) -> str:
        """The half of the refusal that says whose change it probably was.

        Two sentences rather than one, because they call for different next
        moves. A formatter this turn started with `project_run` moving an mtime
        is the model's own doing and wants a re-read; an edit from outside the
        turn is somebody else at work on the same file, and a model told *that*
        should say so in its report rather than quietly writing again.

        It is a guess, and it is worded as one. Nothing here can attribute a
        change: the console's own `PUT /v1/projects/{id}/file` and the user's
        editor both land on the same disk without passing a tool.
        """

        if self.receipts.commands_ran():
            return (
                "A command you ran in this turn may have done it. Read it "
                "again before writing."
            )
        return "Something outside this turn changed it, so read it again."


@dataclass(frozen=True, slots=True)
class ProjectEditTool:
    """Replace one exact occurrence in one file."""

    scope: ProjectFileScope
    #: Not consulted before editing -- this tool reads the file itself, one
    #: statement earlier, so it can never be working from a stale copy. It is
    #: written to afterwards, so a `project_write` later in the same turn is
    #: not refused by a receipt this edit has just made obsolete.
    receipts: ReadReceipts

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
            permission_scopes=(WORKSPACE_WRITE_SCOPE,),
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        path = str(arguments.get("path", ""))
        find = str(arguments.get("find", ""))
        replace = str(arguments.get("replace", ""))
        store = _store(self.scope)
        # Read before the edit, because the edit is about to invalidate it. A
        # file the model never read has no receipt here and gets none after,
        # which is what keeps the next `project_write` refused with the
        # accurate sentence rather than with "you have seen only part of it".
        carried = self.receipts.seen(path)
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
            entry = await store.write(
                path,
                text.replace(find, replace, 1),
                # From this tool's own read, three statements up. That read is
                # what makes an edit safe where a write needs a receipt -- but
                # it leaves a window of its own, and the user's editor saves
                # into windows like that. `read` and `write` are two separate
                # hops onto the executor; this closes the gap between them.
                if_unchanged=ProjectFileVersion(
                    size_bytes=current.size_bytes,
                    modified_at=current.modified_at,
                ),
            )
        except ProjectFileChangedError as error:
            return _stale(invocation, str(error))
        except (ProjectPathError, NotFoundError, OutputTooLargeError) as error:
            return _refusal(invocation, error)
        # Refreshed, not minted. The edit moved the size and the mtime, so a
        # receipt the model had earned still has to be brought up to date or
        # its own next write is refused -- but the coverage it carries is the
        # coverage the model already had, because what the model supplied here
        # was a snippet and what it has seen has not grown.
        if carried is not None:
            _record_written(
                self.receipts, entry, covers_whole_file=carried.covers_whole_file
            )
        return ToolResult.succeeded(
            invocation.call,
            content=f"Edited {entry.path} ({entry.size_bytes} bytes).",
            # An edit is a write for every reader of this field: the bytes
            # under that path are new. What kind of write it was is already on
            # `tool_name`, which is where a caller that cares should read it.
            project_writes=(entry.path,),
        )


@dataclass(frozen=True, slots=True)
class ProjectDeleteTool:
    """Remove one file from the project directory (ADR-0116).

    The gap this closes was reported in one sentence -- 「文件夹中的文件不能
    进行删除和改名」-- and it was true on every path. Claude Code deletes
    with `rm` through Bash; here the shell is `project_run`, which the Windows
    native launcher does not offer at all and which a model holding five
    file-shaped tools does not reach for to remove a file. So the file
    language gets the two verbs it was missing, spelled the way its others
    are: one path, relative to the root, through the same store and the same
    sandbox.

    `destructive`, like `project_run`, because there is no undo and the
    store already refused to make this recursive. Not gated on a read
    receipt: a receipt is a claim about having *seen* the bytes, and
    deleting a file is not a statement about its contents -- `rm` has never
    asked, and a build artefact nobody read is the ordinary thing to remove.
    What it is gated on is a person, in every turn that is not unattended.
    """

    scope: ProjectFileScope
    #: Consulted for nothing and written to for nothing: a deleted file has no
    #: receipt worth keeping, and a later write to the same path is a
    #: creation, which the write gate does not ask about. Held so the three
    #: file-changing tools have one constructor shape.
    receipts: ReadReceipts

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=DELETE_TOOL_NAME,
            description=(
                "Delete one file from this project's directory. Files only: a "
                "directory is refused rather than removed with everything in "
                "it. There is no undo -- this removes the user's real file -- "
                "so say why before you propose it."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {"path": _PATH_SCHEMA},
            },
            concurrency="exclusive",
            risk="destructive",
            idempotency="safe",
            timeout_seconds=60,
            permission_scopes=(WORKSPACE_WRITE_SCOPE,),
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        path = str(invocation.call.arguments.get("path", ""))
        store = _store(self.scope)
        try:
            removed = await store.delete(path)
        except (ProjectPathError, NotFoundError) as error:
            return _refusal(invocation, error)
        if not removed:
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="not_found",
                    message=f"nothing is at {path}, so there is nothing to delete",
                    retryable=False,
                ),
            )
        return ToolResult.succeeded(
            invocation.call,
            content=f"Deleted {path}.",
            # The path is named as a write for every reader of the field
            # (ADR-086): the bytes under it are gone, which is a change to
            # the directory the console shows, and "which paths did this
            # step change" is the question the field answers.
            project_writes=(path,),
        )


@dataclass(frozen=True, slots=True)
class ProjectMoveTool:
    """Rename one file, or move it to another directory (ADR-0116).

    `write`, not `destructive`: nothing is lost by a move, the bytes are the
    same bytes under a new name, and the store refuses to land on an existing
    file so a move can never overwrite one. The read receipt travels with the
    file -- a model that read `a.py`, moved it to `b.py` and then writes
    `b.py` has seen every byte of what it is replacing.
    """

    scope: ProjectFileScope
    receipts: ReadReceipts

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=MOVE_TOOL_NAME,
            description=(
                "Rename one file in this project's directory, or move it to "
                "another directory under the root; parent directories of the "
                "new path are created. The new path must be free -- a move "
                "never replaces a file that is there. Files only."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "new_path"],
                "properties": {
                    "path": _PATH_SCHEMA,
                    "new_path": {
                        **_PATH_SCHEMA,
                        "description": (
                            "Where the file goes, relative to the project root. "
                            "Refused if something is already there."
                        ),
                    },
                },
            },
            concurrency="exclusive",
            risk="write",
            idempotency="safe",
            timeout_seconds=60,
            permission_scopes=(WORKSPACE_WRITE_SCOPE,),
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        path = str(arguments.get("path", ""))
        new_path = str(arguments.get("new_path", ""))
        store = _store(self.scope)
        carried = self.receipts.seen(path)
        try:
            entry = await store.move(path, new_path)
        except ProjectFileExistsError as error:
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="invalid_tool_input", message=str(error), retryable=False
                ),
            )
        except (ProjectPathError, NotFoundError) as error:
            return _refusal(invocation, error)
        if carried is not None:
            # The same file, so the same receipt, under the name it now has.
            # Refreshed from the entry the store returned rather than copied:
            # a rename keeps the mtime on every filesystem this runs on, and
            # reading it back off the entry is what makes that a fact here
            # rather than an assumption.
            _record_written(
                self.receipts, entry, covers_whole_file=carried.covers_whole_file
            )
        return ToolResult.succeeded(
            invocation.call,
            content=f"Moved {path} to {entry.path}.",
            # Both ends. The old path lost its bytes and the new one gained
            # them, and a console refreshing what a step changed needs to
            # hear about each.
            project_writes=(path, entry.path),
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


async def _single_file_listing(
    store: ProjectFileStore, path: str
) -> ProjectListing | None:
    """``path`` as a listing of one file, or ``None`` when it is not one.

    Asked of the parent directory rather than by reading the file, and that is
    the whole reason this is not two lines inside the handler: the corpus
    reader downstream needs `size_bytes` to spend its budget, and learning it
    by reading the file would read every searched file twice -- once to find
    out how big it is, once to search it.

    ``None`` for everything that is not a plain file in its parent: a missing
    path, a directory (which `walk` would have handled), a parent that is
    itself not a directory. The caller then reports the original refusal, so a
    genuinely wrong path still says so.
    """

    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    try:
        listing = await store.list_directory(parent)
    except (ProjectPathError, NotFoundError):
        return None
    for entry in listing.entries:
        if entry.path == path and entry.kind == "file":
            return ProjectListing(path=path, entries=(entry,))
    return None


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
                "one subdirectory -- or one file -- instead of the whole "
                "project, and 'name_glob' "
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
        except ProjectPathError as error:
            return _refusal(invocation, error)
        except NotFoundError as error:
            # `path` may name a file rather than a directory, and searching one
            # file is the most ordinary thing a model does after writing it.
            # Before this, `walk` answered `not a directory: 'mario.html'` --
            # a sentence that reads like the file is missing, about a file the
            # model had written four calls earlier.
            #
            # Measured 2026-09-12 on Windows: a turn tried it twice, got that
            # sentence twice, and fell back to walking the file by hand in
            # 40-line `project_read` windows -- twenty-eight of them, which is
            # most of a 60-step budget. Two of those steps were this refusal;
            # the other twenty-six were what it did instead.
            single = await _single_file_listing(store, path)
            if single is None:
                return _refusal(invocation, error)
            listing = single

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


@dataclass(frozen=True, slots=True)
class ProjectRunTool:
    """One command, run in the project's directory (ADR-077, ADR-0115).

    *Where* it runs is the runner's business, and there are two: the process
    holding this session, which on the native launcher is the user's own
    machine, and a container that mounts the project directory and holds
    nothing else (ADR-0115). The tool is the same in front of either -- the
    same `destructive` risk, the same stop at a person, the same receipts, the
    same sentence to the model -- because the thing the gate protects is the
    user's files, and those are the same files in both places.
    """

    scope: ProjectFileScope
    #: Told that a command ran, and nothing more (ADR-0078). A command's
    #: effects are not knowable from here -- `black .` rewrites files nobody
    #: named -- so this cannot invalidate the receipts it actually invalidated.
    #: What it buys is one sentence: when a later write finds a file moved, the
    #: model is told its own command may have done it and to re-read, rather
    #: than that somebody else is editing the file, which would have it stop
    #: and report instead.
    receipts: ReadReceipts
    #: What a *local* command inherits, decided in
    #: `bootstrap/child_environment.py` and handed here already made. Not read
    #: from `os.environ` in this module:
    #: `tests/architecture/test_dependency_boundaries.py` allows that in
    #: `bootstrap` and nowhere else, and the rule is right -- a tool whose
    #: behaviour depends on a variable nobody passed it is a tool whose
    #: behaviour cannot be read off the configuration. Unused when `runner` is
    #: given: the remote process has an environment of its own, and nothing
    #: of this one's should travel.
    environment: Mapping[str, str]
    #: Injected so a test can drive it down to something a test can wait for.
    timeout_seconds: float = RUN_TIMEOUT_SECONDS
    #: Where the command executes, when not here (ADR-0115). `None` -- the
    #: default every caller written before this existed gets -- means a
    #: `LocalCommandRunner` over `environment`, which is exactly what this tool
    #: did inline until the second place existed. A deployment that runs its
    #: coding sessions in a container passes the runner that reaches the
    #: container beside it (`apps/api/dependencies.py`, `RunnerSlot`).
    runner: CommandRunner | None = None

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
            # "The process this session's commands run in" rather than "the
            # user's real machine", which this said until ADR-0115. Both places
            # a command can run share every property the sentence has to
            # carry -- the user's real files, no undo, a person who sees the
            # command first -- and differ in the one it must not overclaim:
            # whose toolchain is there. The prompt (`_HAS_SHELL`) says which.
            description=(
                "Run one shell command in this project's directory, in the "
                "process this session's commands run in. The files are the "
                "user's real files: there is no sandbox around them and no "
                "undo, and every call stops and asks the user first. Output is "
                "stdout and stderr interleaved in the order they were written, "
                "capped, and marked where it was cut; a non-zero exit code is "
                "reported, not treated as a failure. The command cannot read "
                "input -- anything that prompts will hang until it is killed "
                f"at {int(RUN_TIMEOUT_SECONDS)} seconds."
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
        # Noted before the command runs, not after. A command killed by the
        # clock, or one that failed halfway, has already had whatever effect it
        # had on the directory; recording only successes would leave exactly
        # the interrupted cases silently claiming nothing moved.
        self.receipts.note_command_ran()
        await invocation.progress("running the command")

        runner: CommandRunner = (
            self.runner
            if self.runner is not None
            else LocalCommandRunner(environment=self.environment)
        )
        try:
            outcome = await runner.run(
                command,
                cwd=str(store.working_directory),
                timeout_seconds=self.timeout_seconds,
            )
        except RunnerRefusedError as error:
            # The runner answered and the answer was not an outcome: a refusal
            # on the wire, a result that did not parse. Its code is the tool
            # result's, the way `sandbox_run` carries `SandboxRefusedError`'s.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(code=error.code, message=str(error), retryable=False),
            )
        except AgentWorkbenchError as error:
            # `RunnerUnavailableError` -- the connection was never opened --
            # and anything else this project's own errors describe. Carried
            # as its own sentence rather than the class name, for the reason
            # `SandboxUnavailableError` gives.
            return ToolResult.failed(invocation.call, error.to_error_info())

        # Rendered once, here, whichever runner produced it. The runners cap
        # at `MAX_CAPTURE_BYTES`; this is the ceiling the model sees, with the
        # marker that says where it was cut.
        output = render_output(outcome.output)
        if outcome.timed_out:
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
        lines = [f"exit code: {outcome.exit_code}"]
        if output:
            lines.append(output)
        if outcome.overflowed:
            lines.append(
                f"[the command was killed after producing more than "
                f"{MAX_CAPTURE_BYTES} bytes]"
            )
        return ToolResult.succeeded(
            invocation.call,
            content="\n".join(lines),
        )


__all__ = [
    "DELETE_TOOL_NAME",
    "EDIT_TOOL_NAME",
    "GREP_TOOL_NAME",
    "LIST_TOOL_NAME",
    "MAX_CAPTURE_BYTES",
    "MAX_INLINE_OUTPUT_CHARS",
    "MOVE_TOOL_NAME",
    "READ_TOOL_NAME",
    "RUN_TIMEOUT_SECONDS",
    "WRITE_TOOL_NAME",
    "ProjectDeleteTool",
    "ProjectEditTool",
    "ProjectFilesUnavailableError",
    "ProjectGrepTool",
    "ProjectListTool",
    "ProjectMoveTool",
    "ProjectReadTool",
    "ProjectRunTool",
    "ProjectWriteTool",
]
