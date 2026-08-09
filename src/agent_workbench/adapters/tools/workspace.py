"""The three tools that make a Task's working set reachable (ADR-028).

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

from dataclasses import dataclass

from pydantic import JsonValue

from agent_workbench.application.workspace import (
    WorkspaceEntryNotFoundError,
    WorkspaceSession,
)
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.tools import ToolResult, ToolSpec
from agent_workbench.domain.workspace import WorkspaceOverflowError
from agent_workbench.ports.tools import ToolBinding, ToolInvocation
from agent_workbench.workflows.workspace_scope import WorkspaceScope

LIST_TOOL_NAME = "workspace_list"
READ_TOOL_NAME = "workspace_read"
WRITE_TOOL_NAME = "workspace_write"

#: What a read may put into the model's context in one go. Above this the bytes
#: are still stored -- the workspace is not lossy -- but the model is told the
#: size instead of being handed it, because a single read should not be able to
#: spend the whole context budget.
MAX_INLINE_READ_CHARS = 48_000

#: What one write may accept inline. Larger content arrives by another route
#: (a download, an MCP result) and is bound with `write_ref`, so this ceiling
#: bounds a model-authored string rather than the workspace itself.
MAX_INLINE_WRITE_CHARS = 200_000

_NAME_SCHEMA: dict[str, JsonValue] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "description": "A flat workspace name. No directories and no path separators.",
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
                "Write a file into this task's workspace, replacing it if the "
                "name is already taken. Names are flat: no directories, no "
                "path separators."
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


__all__ = [
    "LIST_TOOL_NAME",
    "MAX_INLINE_READ_CHARS",
    "MAX_INLINE_WRITE_CHARS",
    "READ_TOOL_NAME",
    "WRITE_TOOL_NAME",
    "WorkspaceListTool",
    "WorkspaceReadTool",
    "WorkspaceUnavailableError",
    "WorkspaceWriteTool",
]
