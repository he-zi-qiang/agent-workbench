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

from dataclasses import dataclass
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
    ProjectPathError,
)
from agent_workbench.domain.tools import ToolResult, ToolSpec
from agent_workbench.ports.project_files import ProjectFileStore
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

LIST_TOOL_NAME = "project_list"
READ_TOOL_NAME = "project_read"
WRITE_TOOL_NAME = "project_write"
EDIT_TOOL_NAME = "project_edit"

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


__all__ = [
    "EDIT_TOOL_NAME",
    "LIST_TOOL_NAME",
    "READ_TOOL_NAME",
    "WRITE_TOOL_NAME",
    "ProjectEditTool",
    "ProjectFilesUnavailableError",
    "ProjectListTool",
    "ProjectReadTool",
    "ProjectWriteTool",
]
