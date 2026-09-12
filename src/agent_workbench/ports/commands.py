"""Where a shell command runs, as a seam (ADR-0115).

``project_run`` used to spawn its command itself, and everything about the
tool -- its gate, its receipts, the output ceiling, the sentence it hands the
model -- was tangled with the one line that called ``create_subprocess_shell``.
ADR-0115 adds a second place a command can execute, a container that holds
only the project directory, and the honest way to add a second place is to
name the first one.

The contract is deliberately small. A runner is handed a command, the
directory to run it in and a wall clock, and answers with what happened. It
does not know what a project is, what a receipt is, or that a person approved
the command a moment ago: those are the tool's, and they are the same
whichever runner is behind it.

``output`` is both streams interleaved in the order they were written, and it
is already *bounded* by the runner -- a runner must never return more than
``MAX_CAPTURE_BYTES`` (``adapters/filesystem/commands.py``), because a
transport that has to carry it is the thing the ceiling protects. Rendering
it for the model, with the marker that says where it was cut, is the tool's
job and happens once, on this side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """What one command did, as far as the runner could tell."""

    #: ``None`` only when the command was killed before it could exit -- the
    #: clock ran out, or it produced more than the ceiling.
    exit_code: int | None
    #: stdout and stderr interleaved, capped by the runner.
    output: str
    #: The wall clock fired and the process group was killed.
    timed_out: bool
    #: The output ceiling was passed and the process group was killed.
    overflowed: bool


@runtime_checkable
class CommandRunner(Protocol):
    """One shell command, in one directory, under one clock."""

    async def run(
        self, command: str, *, cwd: str, timeout_seconds: float
    ) -> CommandOutcome: ...


__all__ = ["CommandOutcome", "CommandRunner"]
