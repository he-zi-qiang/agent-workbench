"""What crosses the wire to the runner, and what it refuses before running.

Three fields in, four out, and the one rule that matters is about ``cwd``: it
must be an absolute path *under the root this server was started with*. The
server is what decides that, not the caller, because the caller is the API
process and the root is the runner container's own mount -- two processes,
one path they agree on by deployment (``compose.yaml`` mounts the same host
folder at ``/projects`` on both sides), and the only place that agreement can
be checked is here, against the directory that actually exists.

The ceilings are the local tool's (``adapters/filesystem/commands.py``): the
same 4,000-character command, the same wall clock. A runner that admitted a
longer command or a longer clock than the tool in front of it would be a way
around the tool, and there is no reason for one to exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from agent_workbench.adapters.filesystem.commands import RUN_TIMEOUT_SECONDS

MAX_COMMAND_CHARS: Final[int] = 4_000
MAX_CWD_CHARS: Final[int] = 1_024

RUN_COMMAND_INPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["command", "cwd"],
    "properties": {
        "command": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_COMMAND_CHARS,
            "description": "A shell command, run by /bin/sh in `cwd`.",
        },
        "cwd": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CWD_CHARS,
            "description": (
                "Absolute path of the directory to run in. It must lie under "
                "the projects root this server was started with."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": int(RUN_TIMEOUT_SECONDS),
            "description": (
                f"Seconds before the command is killed; at most "
                f"{int(RUN_TIMEOUT_SECONDS)}, which is also the default."
            ),
        },
    },
}

RUN_COMMAND_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["exit_code", "output", "timed_out", "overflowed"],
    "properties": {
        "exit_code": {"type": ["integer", "null"]},
        "output": {"type": "string"},
        "timed_out": {"type": "boolean"},
        "overflowed": {"type": "boolean"},
    },
}


class RunnerInputError(ValueError):
    """The request cannot be run, and the sentence says which field."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    command: str
    cwd: Path
    timeout_seconds: float


def parse_run_request(payload: dict[str, Any], *, projects_root: Path) -> RunRequest:
    """Judge a request against the schema *and* against the root.

    Type and length checks first, so a caller that sent the wrong shape is
    told that rather than "outside the root". Then containment, on the
    resolved path: ``..`` in text is refused by the resolve, a symlink that
    points out of the root is refused by the resolve, and a directory that is
    not there is refused because a command with no working directory has
    nowhere to start.
    """

    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        raise RunnerInputError("command must be a non-empty string")
    if len(command) > MAX_COMMAND_CHARS:
        raise RunnerInputError(f"command is longer than {MAX_COMMAND_CHARS} characters")

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise RunnerInputError("cwd must be a non-empty string")
    if len(cwd) > MAX_CWD_CHARS:
        raise RunnerInputError(f"cwd is longer than {MAX_CWD_CHARS} characters")
    candidate = Path(cwd)
    if not candidate.is_absolute():
        raise RunnerInputError("cwd must be an absolute path")
    root = projects_root.resolve()
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise RunnerInputError(f"cwd is outside the projects root {root}")
    if not resolved.is_dir():
        raise RunnerInputError(f"cwd does not exist or is not a directory: {cwd}")

    timeout = payload.get("timeout_seconds", int(RUN_TIMEOUT_SECONDS))
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise RunnerInputError("timeout_seconds must be an integer")
    if not 1 <= timeout <= int(RUN_TIMEOUT_SECONDS):
        raise RunnerInputError(
            f"timeout_seconds must be between 1 and {int(RUN_TIMEOUT_SECONDS)}"
        )

    return RunRequest(command=command, cwd=resolved, timeout_seconds=float(timeout))


__all__ = [
    "MAX_COMMAND_CHARS",
    "MAX_CWD_CHARS",
    "RUN_COMMAND_INPUT_SCHEMA",
    "RUN_COMMAND_OUTPUT_SCHEMA",
    "RunRequest",
    "RunnerInputError",
    "parse_run_request",
]
