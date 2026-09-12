"""The runner that executes a command somewhere else (ADR-0115).

The other side of ``ports/commands.py``: a ``CommandRunner`` whose subprocess
lives in a container that holds the project directory and nothing else --
no provider key, no database address, no artifact volume -- reached over the
same loopback MCP tunnel the sandbox broker is reached over (ADR-0107).

The shape is ``adapters/tools/sandbox.py``'s ``WorkspaceSandbox`` with the
file handling taken out, because there is none: the runner container mounts
the same host folder the API mounts, at the same path, so a command's working
directory is a string both sides agree on rather than a set of files to ship.
What crosses the wire is the command, the directory and the clock, and what
comes back is what the local runner would have produced.

Two error classes for the two things that can go wrong before a command runs,
and neither is a command that failed. A command that exited 3 is an outcome
with ``exit_code=3``; the tool reports it and the model reads the traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from agent_workbench.adapters.mcp.client import MCPClientPort
from agent_workbench.domain.errors import ErrorCode, ToolFailedError
from agent_workbench.domain.runner import RUNNER_REMOTE_TOOL
from agent_workbench.ports.commands import CommandOutcome


class RunnerUnavailableError(ToolFailedError):
    """``project_run`` was called before the runner connection was opened.

    Derives from `ToolFailedError` for the reason `SandboxUnavailableError`
    does: `ErrorInfo.from_exception` passes a message through only for an
    `AgentWorkbenchError`, and the model is the reader here -- handed a bare
    class name it has nothing to put in its report but the class name.
    """


class RunnerRefusedError(RuntimeError):
    """The runner answered, and the answer was not a command's outcome.

    A refusal on the wire (`is_error`), a result with no structured body, or a
    body that does not parse. Carries the code the tool result should carry,
    the way `SandboxRefusedError` does, so the tool can turn it into a
    `ToolResult.failed` without branching on which of the three it was.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code


@dataclass(frozen=True, slots=True)
class RemoteCommandRunner:
    """A ``CommandRunner`` that hands the command to ``agent-runner-mcp``."""

    client: MCPClientPort

    async def run(
        self, command: str, *, cwd: str, timeout_seconds: float
    ) -> CommandOutcome:
        remote = await self.client.call_tool(
            RUNNER_REMOTE_TOOL,
            {
                "command": command,
                "cwd": cwd,
                "timeout_seconds": int(timeout_seconds),
            },
        )
        if remote.is_error:
            raise RunnerRefusedError(
                "tool_failed",
                _remote_message(remote.content) or "the runner refused the command",
            )
        body = remote.structured_content
        if not isinstance(body, dict):
            raise RunnerRefusedError(
                "tool_failed", "the runner returned no structured result"
            )
        result = cast(dict[str, Any], body)
        try:
            exit_code = result.get("exit_code")
            return CommandOutcome(
                exit_code=None if exit_code is None else int(cast(int, exit_code)),
                output=str(result.get("output") or ""),
                timed_out=bool(result.get("timed_out", False)),
                overflowed=bool(result.get("overflowed", False)),
            )
        except (TypeError, ValueError) as error:
            raise RunnerRefusedError(
                "tool_failed", "the runner returned a malformed result"
            ) from error


def _remote_message(content: object) -> str:
    """The first text block of a refusal, or ``""``."""

    for block in cast(tuple[Any, ...], content) if isinstance(content, tuple) else ():
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            return text
    return ""


__all__ = [
    "RemoteCommandRunner",
    "RunnerRefusedError",
    "RunnerUnavailableError",
]
