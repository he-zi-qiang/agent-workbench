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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic import JsonValue

from agent_workbench.adapters.mcp.client import MCPClientPort, ProgressSink
from agent_workbench.adapters.tools.media_guess import media_type_for
from agent_workbench.application.workspace import (
    WorkspaceEntryNotFoundError,
    WorkspaceSession,
)
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.domain.errors import ErrorInfo, ToolFailedError
from agent_workbench.domain.sandbox import (
    SANDBOX_REMOTE_TOOL,
    SANDBOX_RUN_SCOPE,
    SANDBOX_RUN_TOOL,
)
from agent_workbench.domain.tools import ToolResult, ToolSpec
from agent_workbench.domain.workspace import WorkspaceOverflowError
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.tools import (
    ToolBinding,
    ToolInvocation,
    ToolProgressReporter,
    discard_progress,
)

TOOL_NAME = SANDBOX_RUN_TOOL

#: How many workspace files one call may feed in. Matches the server's own
#: input ceiling; a request past it would be refused there anyway, and being
#: refused here means it is refused before any bytes are read.
MAX_INPUT_NAMES = 32

#: How many bytes those files may come to. The server's own
#: ``MAX_TOTAL_INPUT_BYTES``, restated here for the same reason as the count
#: above -- and used by the caller that chooses the file list rather than
#: receiving it: the model names its inputs and wears the refusal, while the
#: console feeds a whole working set in and has to decide what fits.
MAX_INPUT_BYTES = 16 * 1024 * 1024

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


class SandboxUnavailableError(ToolFailedError):
    """A sandbox tool ran outside a node that entered a workspace session.

    Derives from `ToolFailedError` rather than `RuntimeError` so that the
    sentence survives. `ErrorInfo.from_exception` passes a message through only
    for `AgentWorkbenchError`; everything else becomes `unhandled <ClassName>`,
    on the reasoning that a third-party message is untrusted content of unknown
    provenance. That reasoning does not cover a string written on the line
    below, and the model is the reader here: handed the class name, it has
    nothing to put in its report but the class name.
    """


class SandboxRefusedError(RuntimeError):
    """One run that did not happen, or happened and could not be kept.

    Carries the code the tool result would have carried, because the two
    callers of :class:`WorkspaceSandbox` need it in two different shapes: a
    ``ToolResult.failed`` for the model, an HTTP status for a person. Raising
    rather than returning a union keeps the success path of both callers free
    of branching on a discriminant.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SandboxOutcome:
    """What one sandboxed run did, before anybody decides how to say it.

    ``written`` names the files the workspace *accepted*, never the ones the
    script claimed to produce (ADR-063): it is appended to only after a version
    commits, so a partial save reports exactly the part that landed.

    The streams are the server's, unbounded here on purpose. The sandbox
    already refuses a run whose stdout or stderr passed its own ceiling, so
    what arrives is bounded; each caller then applies the bound that suits it
    -- the model's context is not the same budget as a browser's viewport, and
    a single ceiling here would be the wrong one for one of them.
    """

    exit_code: int
    stdout: str
    stderr: str
    written: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceSandbox:
    """Workspace in, script, workspace out -- the part with no caller in it.

    Split out of :class:`SandboxRunTool` when the console grew a 运行 button
    (ADR-065). The two callers differ only in who asked and how the answer is
    phrased: a model naming files through a tool schema, and a person clicking
    on a ``.py`` they can see. What must not differ is what happens in between
    -- which names are read, that outputs are bound to workspace versions one
    at a time under compare-and-set, and that a partial save says so. A second
    copy of that would be a second set of rules for the same working set.
    """

    client: MCPClientPort

    async def run(
        self,
        session: WorkspaceSession,
        *,
        script: str,
        inputs: Sequence[str],
        cancellation: CancellationToken,
        progress: ToolProgressReporter = discard_progress,
    ) -> SandboxOutcome:
        """Run ``script`` with ``inputs`` beside it, keeping what it writes.

        ``progress`` names the three phases as they are entered (ADR-068). It
        defaults to the discarding reporter because this method has two callers
        and only one of them has somewhere to report: the tool handler is
        invoked by the executor and carries a channel, while the console's 运行
        button is an HTTP request holding its own response open. Reporting into
        nothing is what the caller without a channel is entitled to do.

        The phases are named rather than counted, and there is no ``percent``.
        Which of the three is running is a fact; "the script is 40% done" is
        not one this process can know -- the script is a container the sandbox
        server owns, and it reports nothing until it exits.
        """

        await progress(f"staging {len(inputs)} input file(s)" if inputs else "staging")
        try:
            payload = [
                {
                    "name": name,
                    "content_base64": base64.b64encode(
                        await session.workspace.read(session.version, name)
                    ).decode("ascii"),
                }
                for name in inputs
            ]
        except WorkspaceEntryNotFoundError as error:
            # Named rather than skipped. A script handed one fewer file than it
            # asked for fails somewhere inside itself, and the traceback that
            # comes back says nothing about the real cause.
            raise SandboxRefusedError("not_found", str(error)) from error

        cancellation.raise_if_cancelled()
        arguments: dict[str, JsonValue] = {"script": script}
        # Omitted rather than sent empty. `inputs` is optional on the server and
        # declares `minItems: 1`, so `[]` is a schema violation -- a script that
        # only computes would be refused before it ran.
        if payload:
            arguments["inputs"] = cast(JsonValue, payload)
        # The one that matters. Everything either side of this line is bounded
        # by a workspace read or a workspace write; this await is a container
        # starting, running arbitrary Python and stopping, and it is the whole
        # of the 300 seconds `sandbox_run` declares.
        #
        # It is no longer silent. `on_progress` receives the script's own
        # output line by line as it is printed (ADR-069), so what a reader
        # watches is the script talking rather than a clock ticking beside a
        # phase name. The phase is still reported first, because a script that
        # prints nothing for a minute is a real and common case and the row
        # must say something before its first line arrives.
        await progress("executing in the sandbox")
        remote = await self.client.call_tool(
            SANDBOX_REMOTE_TOOL,
            arguments,
            # `None` when nobody is listening, and that is not a micro
            # optimisation: passing a callback is what makes the SDK send a
            # progress token, which is what makes the server frame and transmit
            # a notification per slice. A caller with nowhere to report to --
            # the console's 运行 button, which holds its own HTTP response open
            # -- would otherwise pay the whole cost of a stream it discards.
            on_progress=None if progress is discard_progress else _forwarding(progress),
        )
        if remote.is_error:
            raise SandboxRefusedError(
                "tool_failed",
                _remote_message(remote.content) or "the sandbox refused the run",
            )

        body = remote.structured_content
        if not isinstance(body, dict):
            raise SandboxRefusedError(
                "tool_failed", "the sandbox returned no structured result"
            )
        result = cast(dict[str, Any], body)

        try:
            outputs = _outputs(result)
        except (KeyError, TypeError, ValueError, binascii.Error) as error:
            raise SandboxRefusedError(
                "tool_failed", "the sandbox returned a malformed result"
            ) from error
        if len(outputs) > MAX_OUTPUT_FILES:
            raise SandboxRefusedError(
                "output_too_large",
                f"the script produced more than {MAX_OUTPUT_FILES} files",
            )

        written: list[str] = []
        if outputs:
            await progress(f"saving {len(outputs)} output file(s)")
        for name, content in outputs:
            cancellation.raise_if_cancelled()
            try:
                session.version = await session.workspace.write(
                    session.version,
                    name,
                    content,
                    media_type=media_type_for(name, content),
                )
            except (ValueError, WorkspaceOverflowError) as error:
                # Partial by construction: the versions already committed are
                # real and stay. Saying which ones landed is the difference
                # between a caller that can retry the rest and one that cannot.
                raise SandboxRefusedError(
                    "invalid_tool_input",
                    f"saved {', '.join(written) or 'nothing'} before {name} "
                    f"was refused: {error}",
                ) from error
            written.append(name)

        return SandboxOutcome(
            exit_code=int(cast(int, result.get("exit_code", 0))),
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            written=tuple(written),
        )


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
        try:
            outcome = await WorkspaceSandbox(client=self.client).run(
                _session(self.scope),
                script=str(arguments.get("script", "")),
                inputs=[
                    str(name) for name in cast(list[Any], arguments.get("inputs") or [])
                ],
                cancellation=invocation.cancellation,
                # The channel the executor bound to this call. Passed rather
                # than rebuilt, so the phases below land on the same
                # `tool_call_id` as this call's ToolStarted and ToolCompleted.
                progress=invocation.progress,
            )
        except SandboxRefusedError as refusal:
            return _failed(invocation, refusal.code, str(refusal))

        return ToolResult.succeeded(
            invocation.call,
            content=_summary(outcome),
            # `written` is appended to only after the store accepted a version,
            # so it names the files that actually landed and never the ones the
            # script claimed (ADR-063). One script can produce several files,
            # which is why this is the tool that made the field a tuple rather
            # than a single name.
            workspace_writes=outcome.written,
        )


def _forwarding(report: ToolProgressReporter) -> ProgressSink:
    """Turn the MCP progress triple back into one line of tool progress.

    The `progress` count and the `total` are dropped, deliberately. Both are
    real -- the count is characters streamed so far -- but neither is a
    completion fraction: the sandbox does not know how much a script will
    print, so `total` is always `None` and a percentage computed from the pair
    would be an invention. `ToolProgress.percent` therefore stays empty, which
    is the same decision ADR-068 §2.3 made for the heartbeat.

    A notification with no message carries nothing to show and is dropped
    rather than turned into an empty line.
    """

    # Parameter names matter here and are not free to improve: the SDK calls
    # this with keywords, so `progress` has to be called `progress` even though
    # the reporter it forwards to is the more interesting thing in scope --
    # which is why that one is bound as `report`.
    async def forward(
        progress: float, total: float | None, message: str | None
    ) -> None:
        del progress, total
        if message is None:
            return
        # Trailing whitespace stripped, leading whitespace kept. `print` ends
        # every line with a newline that would otherwise ride into a
        # single-line row; indentation at the *front* of a line is the script
        # saying something about its own output and is left alone.
        text = message.rstrip()
        if text:
            await report(text)

    return forward


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


def _summary(outcome: SandboxOutcome) -> str:
    lines = [f"exit_code: {outcome.exit_code}"]
    for channel, text in (("stdout", outcome.stdout), ("stderr", outcome.stderr)):
        if text:
            lines.append(f"{channel}:\n{_bounded(text)}")
    lines.append(
        "saved to the workspace: "
        + (", ".join(outcome.written) if outcome.written else "nothing")
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


def _failed(invocation: ToolInvocation, code: str, message: str) -> ToolResult:
    return ToolResult.failed(
        invocation.call,
        ErrorInfo(code=code, message=message, retryable=False),  # pyright: ignore[reportArgumentType]
    )


__all__ = [
    "MAX_INLINE_STREAM_CHARS",
    "MAX_INPUT_BYTES",
    "MAX_INPUT_NAMES",
    "MAX_OUTPUT_FILES",
    "TOOL_NAME",
    "SandboxOutcome",
    "SandboxRefusedError",
    "SandboxRunTool",
    "SandboxUnavailableError",
    "WorkspaceSandbox",
]
