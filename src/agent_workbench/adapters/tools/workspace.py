"""The tools that make a Task's working set reachable (ADR-028, ADR-030).

A handler returns a ``ToolResult`` and nothing else -- it has no way to put a
new workspace version into graph state. So the version lives on a session the
node owns: the node creates one pinned to its entry version, hands it to the
tools, and reads back whatever it advanced to. That is the shape
``RetrievalJournal`` already uses for the same reason, and it keeps the state
update where it belongs, in the node.

These are native tools rather than an MCP server, and that is a departure from
ADR-025/026/027 with a specific cause: they touch the artifact store and the
Task's state, which is Worker-internal authority. ADR-026's rule is that an MCP
server receives no path and no owner; handing one the artifact store would
invert it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch

from pydantic import JsonValue

from agent_workbench.application.workspace import (
    WorkspaceEntryNotFoundError,
    WorkspaceSession,
)
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.tools import ToolResult, ToolSpec
from agent_workbench.domain.workspace import (
    GREP_TIMEOUT_SECONDS,
    MAX_GREP_MATCHES,
    WorkspaceEditMatchError,
    WorkspaceOverflowError,
    WorkspacePatternError,
    WorkspaceScanTimeoutError,
    grep_workspace,
    replace_exactly_once,
)
from agent_workbench.ports.tools import ToolBinding, ToolInvocation
from agent_workbench.workflows.workspace_scope import WorkspaceScope

LIST_TOOL_NAME = "workspace_list"
READ_TOOL_NAME = "workspace_read"
WRITE_TOOL_NAME = "workspace_write"
EDIT_TOOL_NAME = "workspace_edit"
GREP_TOOL_NAME = "workspace_grep"

#: What a read may put into the model's context in one go. Above this the bytes
#: are still stored -- the workspace is not lossy -- but the model is told the
#: size instead of being handed it, because a single read should not be able to
#: spend the whole context budget.
MAX_INLINE_READ_CHARS = 48_000

#: What one write may accept inline. Larger content arrives by another route
#: (a download, an MCP result) and is bound with `write_ref`, so this ceiling
#: bounds a model-authored string rather than the workspace itself.
MAX_INLINE_WRITE_CHARS = 200_000

#: The tool-facing half of ``WorkspaceName``, kept the same shape on purpose.
#:
#: It used to state only the length, so a model proposing ``季度总结.docx`` was
#: told nothing until the write came back -- and before the manifest was fixed
#: to validate, not even then. Declaring the pattern lets the model see the
#: rule with the tool, and lets the schema check refuse a name at the argument
#: boundary rather than three layers in.
#:
#: The description says it in words as well. A model reads the prose more
#: reliably than the regex, and a name is one of the few arguments where
#: guessing wrong costs a whole step.
_NAME_SCHEMA: dict[str, JsonValue] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$",
    "description": (
        "A flat workspace name. No directories and no path separators. "
        "ASCII letters, digits, dot, underscore and hyphen only, starting "
        "with a letter or digit -- a name with other characters, including "
        "any non-English text, is refused. Write 'quarterly-summary.docx', "
        "not '季度总结.docx'."
    ),
}


class WorkspaceUnavailableError(RuntimeError):
    """A workspace tool ran outside a node that entered a session."""


def _session(scope: WorkspaceScope) -> WorkspaceSession:
    """The session this node entered, or a refusal.

    An unentered scope is not a reason to create one: a workspace no node
    committed is one no checkpoint names, so everything written into it would
    be discarded at the end of the run without anything saying so.
    """

    session = scope.current()
    if session is None:
        raise WorkspaceUnavailableError(
            "no workspace session is entered for this node invocation"
        )
    return session


@dataclass(frozen=True, slots=True)
class WorkspaceListTool:
    """What is in the working set, without reading any of it."""

    scope: WorkspaceScope

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=LIST_TOOL_NAME,
            description=(
                "List the files in this task's workspace with their sizes and "
                "types. Names are flat; there are no directories."
            ),
            input_schema={"type": "object", "additionalProperties": False},
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=30,
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        session = _session(self.scope)
        listing = await session.workspace.list(session.version)
        if not listing:
            return ToolResult.succeeded(
                invocation.call, content="The workspace is empty."
            )
        lines = "\n".join(
            f"{item.name}\t{item.size_bytes} bytes\t{item.media_type}"
            for item in listing
        )
        return ToolResult.succeeded(invocation.call, content=lines)


@dataclass(frozen=True, slots=True)
class WorkspaceReadTool:
    """One file out of the working set."""

    scope: WorkspaceScope

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=READ_TOOL_NAME,
            description=(
                "Read one file from this task's workspace by name. Use "
                "workspace_list first if you do not know the name."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {"name": _NAME_SCHEMA},
            },
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=30,
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        name = str(invocation.call.arguments.get("name", ""))
        session = _session(self.scope)
        try:
            content = await session.workspace.read(session.version, name)
        except WorkspaceEntryNotFoundError:
            # Named rather than empty: a model that received "" would build on
            # something that was never there, and the mistake would surface much
            # later as content with no source.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="not_found",
                    message=f"no workspace file named {name!r}",
                    retryable=False,
                ),
            )
        text = content.decode("utf-8", errors="replace")
        if len(text) > MAX_INLINE_READ_CHARS:
            return ToolResult.succeeded(
                invocation.call,
                content=(
                    f"{name} holds {len(content)} bytes, which is too large to "
                    f"show in full. First {MAX_INLINE_READ_CHARS} characters:\n"
                    + text[:MAX_INLINE_READ_CHARS]
                ),
            )
        return ToolResult.succeeded(invocation.call, content=text)


@dataclass(frozen=True, slots=True)
class WorkspaceWriteTool:
    """Bind a name to new bytes, producing the next workspace version."""

    scope: WorkspaceScope

    def binding(self) -> ToolBinding:
        # No operation key. The effect lands in this project's own versioned
        # store, so a replay produces another version rather than a second
        # effect somewhere nothing can take it back.
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=WRITE_TOOL_NAME,
            description=(
                "Write a text file into this task's workspace, replacing it if "
                "the name is already taken. Names are flat: no directories, no "
                "path separators. The content is written as text, so this "
                "cannot produce Word, Excel, PowerPoint, PDF or other binary "
                "documents, and declaring one of those media types is refused."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "content"],
                "properties": {
                    "name": _NAME_SCHEMA,
                    "content": {
                        "type": "string",
                        "maxLength": MAX_INLINE_WRITE_CHARS,
                    },
                    "media_type": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 128,
                    },
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
        name = str(arguments.get("name", ""))
        content = str(arguments.get("content", ""))
        media_type = str(arguments.get("media_type") or _media_type_for(name))
        session = _session(self.scope)
        if _unwritable_media_type(media_type):
            # Before the write, with the name-validator refusals below: a write
            # this tool cannot honour must leave the session's version where it
            # was. Only the declared type can reach here -- the guess from the
            # name is text either way.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message=(
                        f"{WRITE_TOOL_NAME} stores the text it is given, so it "
                        f"cannot write a {media_type} file: that format is a "
                        "binary package, not text. Write the content as text "
                        "(a .md or .txt name), or, to produce a real document, "
                        "use a document-rendering tool if this deployment "
                        "offers one."
                    ),
                    retryable=False,
                ),
            )
        try:
            session.version = await session.workspace.write(
                session.version,
                name,
                content.encode("utf-8"),
                media_type=media_type,
            )
        except (ValueError, WorkspaceOverflowError) as error:
            # Includes the name validator: a refused write must leave the
            # session's version where it was, so a node that then succeeds
            # commits only what actually landed.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message=str(error),
                    retryable=False,
                ),
            )
        return ToolResult.succeeded(
            invocation.call,
            content=f"Wrote {len(content)} characters to {name}.",
        )


@dataclass(frozen=True, slots=True)
class WorkspaceEditTool:
    """Replace one exact passage inside a file, leaving the rest untouched.

    ``workspace_write`` can already produce any content, so this adds no
    authority -- what it adds is the ability to change a large file at all
    (ADR-030 §2.3). Editing one line through whole-file write means restating
    the file verbatim, which on a two-thousand-line file is not an expensive
    step but an impossible one, and a single mis-restated character is silent
    corruption.
    """

    scope: WorkspaceScope

    def binding(self) -> ToolBinding:
        # No operation key, for the same reason as the write tool: the effect
        # lands in this project's own versioned store, so a replay produces
        # another version rather than a second outside effect.
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=EDIT_TOOL_NAME,
            description=(
                "Replace an exact passage in a workspace file. old_text must "
                "appear exactly once in the file: if it appears zero times or "
                "more than once the edit is refused and the file is left "
                "unchanged. Include enough surrounding text to make the "
                "passage unique."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "old_text", "new_text"],
                "properties": {
                    "name": _NAME_SCHEMA,
                    "old_text": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_INLINE_WRITE_CHARS,
                        "description": (
                            "The exact text to replace, copied from the file."
                        ),
                    },
                    "new_text": {
                        "type": "string",
                        "maxLength": MAX_INLINE_WRITE_CHARS,
                        "description": (
                            "What to put in its place. May be empty to delete "
                            "the passage."
                        ),
                    },
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
        name = str(arguments.get("name", ""))
        old_text = str(arguments.get("old_text", ""))
        new_text = str(arguments.get("new_text", ""))
        session = _session(self.scope)

        try:
            current = await session.workspace.read(session.version, name)
        except WorkspaceEntryNotFoundError:
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="not_found",
                    message=f"no workspace file named {name!r}",
                    retryable=False,
                ),
            )

        try:
            edited = replace_exactly_once(
                current.decode("utf-8", errors="replace"),
                old_text,
                new_text,
                name=name,
            )
        except WorkspaceEditMatchError as error:
            # Nothing has been written at this point, and that is the property
            # worth stating: a refused edit leaves both the bytes and the
            # session's version exactly where they were, so the model can read
            # the file and try again against what is actually there.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message=str(error),
                    retryable=False,
                ),
            )

        try:
            session.version = await session.workspace.write(
                session.version,
                name,
                edited.encode("utf-8"),
                media_type=_media_type_for(name),
            )
        except (ValueError, WorkspaceOverflowError) as error:
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message=str(error),
                    retryable=False,
                ),
            )
        return ToolResult.succeeded(
            invocation.call,
            content=(
                f"Replaced one passage in {name}. "
                f"The file is now {len(edited)} characters."
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceGrepTool:
    """Which files mention something, without reading them all in (ADR-030 2.4).

    ``workspace_list`` gives names only. An agent working over dozens of files
    otherwise has to read each one into context to find the one that matters,
    and those are exactly the expensive steps the budget work was about.
    """

    scope: WorkspaceScope
    #: Injected so a test can drive the clock. The scan's time budget is
    #: wall-clock over the whole call, not per line.
    monotonic: Callable[[], float] = time.monotonic

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=GREP_TOOL_NAME,
            description=(
                "Search the workspace for lines matching a regular expression "
                "and return where they are. Optionally restrict it to names "
                "matching a glob such as '*.md'. Results are capped, and the "
                "reply says what was left unsearched."
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
                    "name_glob": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": (
                            "Shell-style glob over workspace names, e.g. '*.md'."
                        ),
                    },
                },
            },
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=30,
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        pattern = str(arguments.get("pattern", ""))
        name_glob = arguments.get("name_glob")
        session = _session(self.scope)

        listing = await session.workspace.list(session.version)
        names = [
            item.name
            for item in listing
            if name_glob is None or fnmatch(item.name, str(name_glob))
        ]
        files = [
            (
                name,
                (await session.workspace.read(session.version, name)).decode(
                    "utf-8", errors="replace"
                ),
            )
            for name in names
        ]

        try:
            outcome = grep_workspace(files, pattern, now=self.monotonic)
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
            # Structured, and deliberately not retryable: the same pattern over
            # the same workspace will time out again. Retrying is what the model
            # does with a transient error, and it would burn the budget the
            # timeout exists to protect.
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

        if not outcome.matches:
            unscanned = (
                ""
                if not outcome.unscanned_files
                else " (not all files were searched: "
                + ", ".join(outcome.unscanned_files)
                + ")"
            )
            return ToolResult.succeeded(
                invocation.call, content=f"No matches.{unscanned}"
            )

        lines = [
            f"{match.name}:{match.line_number}: {match.line}"
            for match in outcome.matches
        ]
        if outcome.more_matches:
            # Said out loud, because a capped list that looks complete is worse
            # than a short one: the model would conclude it has seen every site
            # and stop looking.
            lines.append(
                f"... stopped at {MAX_GREP_MATCHES} matches. Not searched: "
                + ", ".join(outcome.unscanned_files)
            )
        elif outcome.unscanned_files:
            lines.append("... not searched: " + ", ".join(outcome.unscanned_files))
        return ToolResult.succeeded(invocation.call, content="\n".join(lines))


#: Types this tool cannot produce, whatever the model calls its content.
#:
#: ``content`` is a JSON string and the handler writes its UTF-8 bytes, so what
#: lands is text. Every type here is a binary package -- a zip of parts for the
#: Office and OpenDocument formats, an object graph with a byte-offset table for
#: PDF -- and no string encodes one by accident. So a write naming one of these
#: is not a file in that format; it is a text file wearing its label.
#:
#: The label then travels further than the mistake. The console offers the
#: layout preview on media type alone, so a text file typed .docx lights up a
#: 版面 button that can only answer 422, and the reader is told their document
#: is broken when what is broken is its type. Refused at the argument, where the
#: model can still write the text under a name that is true.
_UNWRITABLE_MEDIA_TYPES = frozenset(
    {
        "application/msword",
        "application/pdf",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    }
)


#: Enough to keep a listing readable. Guessed from the name because the model
#: usually omits it, and a wrong guess here costs a label, not the bytes.
_SUFFIX_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".py": "text/x-python",
    ".html": "text/html",
}


def _media_type_for(name: str) -> str:
    for suffix, media_type in _SUFFIX_MEDIA_TYPES.items():
        if name.endswith(suffix):
            return media_type
    return "text/plain"


def _unwritable_media_type(media_type: str) -> bool:
    # Parameters and case are the wire's, not the model's: `application/pdf`
    # and `Application/PDF; charset=utf-8` name the same format, and a check
    # that saw two would refuse one and let the other through.
    return media_type.split(";", 1)[0].strip().lower() in _UNWRITABLE_MEDIA_TYPES


__all__ = [
    "EDIT_TOOL_NAME",
    "GREP_TOOL_NAME",
    "LIST_TOOL_NAME",
    "MAX_INLINE_READ_CHARS",
    "MAX_INLINE_WRITE_CHARS",
    "READ_TOOL_NAME",
    "WRITE_TOOL_NAME",
    "WorkspaceEditTool",
    "WorkspaceGrepTool",
    "WorkspaceListTool",
    "WorkspaceReadTool",
    "WorkspaceUnavailableError",
    "WorkspaceWriteTool",
]
