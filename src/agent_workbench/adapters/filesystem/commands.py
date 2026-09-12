"""The runner that executes a command in this process's own world (ADR-077).

Lifted out of ``adapters/tools/project_files.py`` when ADR-0115 gave
``project_run`` a second place to execute. Nothing here changed on the way:
the process group, the ``SIGKILL``, the bounded read that keeps what it had
already read, and the three ceilings are the ones ADR-077 argued for. What
moved is only that they are now behind ``ports/commands.py``, so the tool
that gates a command and the server that executes one in a container
(``apps/runner_mcp``) share this code rather than each keeping a copy that
drifts.

POSIX only, and deliberately so: ``os.killpg`` and ``start_new_session`` are
what make "kill the command and everything it started" a true sentence, and
the two places this runs -- a developer's macOS or Linux machine, and a Linux
container -- both have them. On Windows the native launcher does not offer
``project_run`` at all (``docs/windows-quickstart.md``), and the container
path is Linux by construction.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from agent_workbench.ports.commands import CommandOutcome

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


def terminate_group(process: asyncio.subprocess.Process) -> None:
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


async def capture_bounded(
    process: asyncio.subprocess.Process, chunks: list[bytes]
) -> bool:
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


def render_output(text: str) -> str:
    """The output as far as it fits, marked where it was cut."""

    if len(text) <= MAX_INLINE_OUTPUT_CHARS:
        return text
    return (
        f"[{len(text)} characters; first {MAX_INLINE_OUTPUT_CHARS} shown]\n"
        + text[:MAX_INLINE_OUTPUT_CHARS]
    )


@dataclass(frozen=True, slots=True)
class LocalCommandRunner:
    """``/bin/sh -c`` in this process, with the environment it was handed.

    ``environment`` is the whole environment the command sees -- on the native
    path `bootstrap/child_environment.py`'s scrubbed copy of this process's
    own, in the runner container that container's, which never held a secret
    to scrub. It is a constructor argument rather than read here, for the
    reason the tool that used to do this gave: the one place that decides what
    a child may see should be the one that knows what the parent holds.
    """

    environment: Mapping[str, str]

    async def run(
        self, command: str, *, cwd: str, timeout_seconds: float
    ) -> CommandOutcome:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=dict(self.environment),
            start_new_session=True,
        )
        chunks: list[bytes] = []
        overflowed = False
        timed_out = False
        exit_code: int | None = None
        try:
            async with asyncio.timeout(timeout_seconds):
                overflowed = await capture_bounded(process, chunks)
                if overflowed:
                    terminate_group(process)
                exit_code = await process.wait()
        except TimeoutError:
            timed_out = True
        finally:
            terminate_group(process)
        captured = b"".join(chunks)[:MAX_CAPTURE_BYTES]
        return CommandOutcome(
            exit_code=None if timed_out else exit_code,
            output=captured.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            overflowed=overflowed,
        )


__all__ = [
    "MAX_CAPTURE_BYTES",
    "MAX_INLINE_OUTPUT_CHARS",
    "RUN_TIMEOUT_SECONDS",
    "LocalCommandRunner",
    "capture_bounded",
    "render_output",
    "terminate_group",
]
