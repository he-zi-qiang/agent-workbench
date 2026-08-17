"""The Task-side half of the sandbox: workspace in, workspace out (ADR-029 §3.1).

The sandbox server knows nothing about workspaces, tenants or owners, and that
is what makes it safe to run beside the Worker rather than inside it. Somebody
still has to carry bytes across that gap, and it has to be somebody holding the
Worker's own authority. That is this module: it reads the named files out of the
node's workspace session, hands the server content, and binds whatever comes
back to names in the next workspace version.

So the model never sees base64 and never names a path. It names files it already
knows about, and gets told which files now exist.

The split is the same one ADR-026 drew for the Word renderer, and it is drawn
here for the same reason: an MCP server that received an artifact store would be
a server that could write under any tenant it was told to.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, cast

from pydantic import JsonValue

from agent_workbench.adapters.mcp.client import MCPClientPort
from agent_workbench.application.workspace import (
    WorkspaceEntryNotFoundError,
    WorkspaceSession,
)
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.sandbox import (
    SANDBOX_REMOTE_TOOL,
    SANDBOX_RUN_SCOPE,
    SANDBOX_RUN_TOOL,
)
from agent_workbench.domain.tools import ToolResult, ToolSpec
from agent_workbench.domain.workspace import WorkspaceOverflowError
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

TOOL_NAME = SANDBOX_RUN_TOOL

#: How many workspace files one call may feed in. Matches the server's own
#: input ceiling; a request past it would be refused there anyway, and being
#: refused here means it is refused before any bytes are read.
MAX_INPUT_NAMES = 32

#: What one call's stdout or stderr may put into the model's context. The
#: sandbox itself allows far more, and the excess is not lost -- the byte count
#: is reported alongside the head, which is the shape ``workspace_read`` already
#: uses for the same problem.
MAX_INLINE_STREAM_CHARS = 8_000

#: What one call may add to the working set. The server bounds its own outputs;
#: this bounds how many of them become named workspace entries.
MAX_OUTPUT_FILES = 32

_NAME_SCHEMA: dict[str, JsonValue] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "description": "A flat workspace name. No directories and no path separators.",
}


class SandboxUnavailableError(RuntimeError):
    """A sandbox tool ran outside a node that entered a workspace session."""


@dataclass(frozen=True, slots=True)
class SandboxRunTool:
    """Run one script over named workspace files and keep what it wrote."""

    scope: WorkspaceScope
    client: MCPClientPort

    def binding(self) -> ToolBinding:
        # No operation key. ADR-029 §3.4: a fresh, network-less container with
        # no history produces another equally legal execution on replay, and
        # its only effect lands in this project's own versioned workspace.
        return ToolBinding(spec=self.spec(), handler=self.handle)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL_NAME,
            description=(
                "Run a Python 3 script over files from this task's workspace. "
                "Use it whenever a step is better computed than reasoned "
                "about: parsing, aggregating, converting, or checking a file "
                "you wrote. Name the workspace files the script needs in "
                "`inputs`; they appear in its working directory under those "
                "names, and any file it writes there is saved back into the "
                "workspace. There is no network access and nothing survives "
                "between calls, so it cannot fetch a URL or call an API."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["script"],
                "properties": {
                    "script": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 40_000,
                        "description": "Python 3 source to execute.",
                    },
                    "inputs": {
                        "type": "array",
                        "maxItems": MAX_INPUT_NAMES,
                        "items": _NAME_SCHEMA,
                        "description": (
                            "Workspace file names to place in the working "
                            "directory. Use workspace_list if unsure."
                        ),
                    },
                },
            },
            # ADR-029 §3.5, exactly as declared there. `external` because the
            # content leaves this process for an execution environment outside
            # it -- not because the sandbox can reach anything.
            concurrency="exclusive",
            risk="external",
            idempotency="safe",
            timeout_seconds=300,
            permission_scopes=(SANDBOX_RUN_SCOPE,),
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        script = str(arguments.get("script", ""))
        names = [str(name) for name in cast(list[Any], arguments.get("inputs") or [])]
        session = _session(self.scope)

        try:
            inputs = [
                {
                    "name": name,
                    "content_base64": base64.b64encode(
                        await session.workspace.read(session.version, name)
                    ).decode("ascii"),
                }
                for name in names
            ]
        except WorkspaceEntryNotFoundError as error:
            # Named rather than skipped. A script handed one fewer file than it
            # asked for fails somewhere inside itself, and the traceback that
            # comes back says nothing about the real cause.
            return _failed(invocation, "not_found", str(error))

        invocation.cancellation.raise_if_cancelled()
        arguments_out: dict[str, JsonValue] = {"script": script}
        # Omitted rather than sent empty. `inputs` is optional on the server and
        # declares `minItems: 1`, so `[]` is a schema violation -- a script that
        # only computes would be refused before it ran.
        if inputs:
            arguments_out["inputs"] = cast(JsonValue, inputs)
        remote = await self.client.call_tool(SANDBOX_REMOTE_TOOL, arguments_out)
        if remote.is_error:
            return _failed(
                invocation,
                "tool_failed",
                _remote_message(remote.content) or "the sandbox refused the run",
            )

        payload = remote.structured_content
        if not isinstance(payload, dict):
            return _failed(
                invocation, "tool_failed", "the sandbox returned no structured result"
            )
        body = cast(dict[str, Any], payload)

        try:
            outputs = _outputs(body)
        except (KeyError, TypeError, ValueError, binascii.Error):
            return _failed(
                invocation, "tool_failed", "the sandbox returned a malformed result"
            )
        if len(outputs) > MAX_OUTPUT_FILES:
            return _failed(
                invocation,
                "output_too_large",
                f"the script produced more than {MAX_OUTPUT_FILES} files",
            )

        written: list[str] = []
        for name, content in outputs:
            invocation.cancellation.raise_if_cancelled()
            try:
                session.version = await session.workspace.write(
                    session.version, name, content, media_type=_media_type_for(name)
                )
            except (ValueError, WorkspaceOverflowError) as error:
                # Partial by construction: the versions already committed are
                # real and stay. Saying which ones landed is the difference
                # between a caller that can retry the rest and one that cannot.
                return _failed(
                    invocation,
                    "invalid_tool_input",
                    f"saved {', '.join(written) or 'nothing'} before {name} "
                    f"was refused: {error}",
                )
            written.append(name)

        return ToolResult.succeeded(
            invocation.call,
            content=_summary(body, written),
        )


def _session(scope: WorkspaceScope) -> WorkspaceSession:
    session = scope.current()
    if session is None:
        raise SandboxUnavailableError(
            "no workspace session is entered for this node invocation"
        )
    return session


def _outputs(body: dict[str, Any]) -> list[tuple[str, bytes]]:
    return [
        (
            str(cast(dict[str, Any], item)["name"]),
            base64.b64decode(
                str(cast(dict[str, Any], item)["content_base64"]), validate=True
            ),
        )
        for item in cast(list[Any], body["outputs"])
    ]


def _summary(body: dict[str, Any], written: list[str]) -> str:
    exit_code = body.get("exit_code")
    lines = [f"exit_code: {exit_code}"]
    for channel in ("stdout", "stderr"):
        text = str(body.get(channel) or "")
        if text:
            lines.append(f"{channel}:\n{_bounded(text)}")
    lines.append(
        "saved to the workspace: " + (", ".join(written) if written else "nothing")
    )
    return "\n".join(lines)


def _bounded(text: str) -> str:
    """The head plus the true size, never a silent cut.

    ``workspace_read`` already answers an oversized read this way, and the
    reason carries: a model shown a truncated stream with no marker treats it
    as the whole stream.
    """

    if len(text) <= MAX_INLINE_STREAM_CHARS:
        return text
    return (
        f"[{len(text)} characters; first {MAX_INLINE_STREAM_CHARS} shown]\n"
        + text[:MAX_INLINE_STREAM_CHARS]
    )


def _remote_message(content: object) -> str:
    if not isinstance(content, tuple | list):
        return ""
    parts = [
        str(getattr(block, "text", "")) for block in cast(list[Any], content)
    ]  # fmt: skip
    return " ".join(part for part in parts if part)[:500]


#: Enough to keep a listing readable, guessed from the name. A wrong guess here
#: costs a label, not the bytes.
_SUFFIX_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".py": "text/x-python",
    ".html": "text/html",
    ".png": "image/png",
    ".pdf": "application/pdf",
    # Typed octet-stream an .svg was download-only in the console; typed as
    # the image it is, the `<img>` viewer shows it (rasterised -- scripts in
    # it never run there).
    ".svg": "image/svg+xml",
}


def _media_type_for(name: str) -> str:
    for suffix, media_type in _SUFFIX_MEDIA_TYPES.items():
        if name.endswith(suffix):
            return media_type
    return "application/octet-stream"


def _failed(invocation: ToolInvocation, code: str, message: str) -> ToolResult:
    return ToolResult.failed(
        invocation.call,
        ErrorInfo(code=code, message=message, retryable=False),  # pyright: ignore[reportArgumentType]
    )


__all__ = [
    "MAX_INLINE_STREAM_CHARS",
    "MAX_INPUT_NAMES",
    "MAX_OUTPUT_FILES",
    "TOOL_NAME",
    "SandboxRunTool",
    "SandboxUnavailableError",
]
