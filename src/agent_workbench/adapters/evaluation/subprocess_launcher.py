"""Running an evaluation script as a child of this process.

A subprocess and not a thread, for the reason ADR-042 §6 records about the
converter next door: `asyncio.to_thread` shares the interpreter's default
executor with DNS resolution, and a seventy-minute run parked there would be
seventy minutes of everybody else's name lookups. A subprocess needs no thread
at all -- and it also means the embedding runtime's memory lives and dies
outside the API process, which for a model that loads gigabytes is the whole
point.

`sys.executable` runs the API's own interpreter. A deployment without the
`embedding` extra therefore fails within seconds, non-zero, and the tail of its
own stderr is what the console shows -- which is honest, and better than a
config field holding a shell command that would have to be quoted, split and
trusted.
"""

from __future__ import annotations

import asyncio
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from agent_workbench.ports.evaluation_runs import (
    EvaluationBusyError,
    EvaluationRunState,
    EvaluationSuite,
)

#: Suite name to argv. A fixed table, and the request contributes no character
#: to it: the body names a key from a `Literal`, and the server chooses the
#: command. Building argv by formatting a suite name into a string is the one
#: mistake this file exists to make impossible.
COMMANDS: Final[dict[EvaluationSuite, tuple[str, ...]]] = {
    "rag": ("scripts/run_rag_eval.py",),
    "chat": ("scripts/run_chat_eval.py",),
    "triage": ("scripts/run_triage_eval.py",),
}

#: How many output lines to keep. A tail, so that a caller can see the run is
#: still moving; the whole log belongs in a terminal, and streaming it through
#: a poll would make the response grow for as long as the run does.
KEPT_LINES: Final[int] = 200


class SubprocessEvaluationLauncher:
    """One run at a time, in a child process, for as long as this process lives."""

    __slots__ = ("_process", "_project_root", "_state", "_timeout_seconds", "_watcher")

    def __init__(self, *, project_root: Path, timeout_seconds: int) -> None:
        self._project_root = project_root
        self._timeout_seconds = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._state: EvaluationRunState | None = None
        self._watcher: asyncio.Task[None] | None = None

    async def start(self, suite: EvaluationSuite) -> EvaluationRunState:
        if self._state is not None and self._state.status == "running":
            raise EvaluationBusyError(
                "an evaluation is already running; this machine fits one at a time"
            )

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *COMMANDS[suite],
            cwd=self._project_root,
            stdout=asyncio.subprocess.PIPE,
            # Merged, because a runner's progress and its failure both go to
            # stderr and reading them apart would let one starve the other.
            stderr=asyncio.subprocess.STDOUT,
        )
        self._process = process
        self._state = EvaluationRunState(
            suite=suite,
            status="running",
            started_at=datetime.now(UTC),
            finished_at=None,
            exit_code=None,
            recent_output=(),
        )
        self._watcher = asyncio.create_task(self._watch(process, suite))
        return self._state

    def state(self) -> EvaluationRunState | None:
        return self._state

    async def cancel(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        # Killed rather than terminated: the runner spends most of its time
        # inside a model's C extension, where a SIGTERM handler does not get a
        # turn until the current batch finishes -- which can be minutes.
        process.kill()
        await process.wait()

    async def _watch(
        self, process: asyncio.subprocess.Process, suite: EvaluationSuite
    ) -> None:
        """Follow the child to its end, keeping the tail of what it said."""

        lines: deque[str] = deque(maxlen=KEPT_LINES)
        started = (
            self._state.started_at if self._state is not None else datetime.now(UTC)
        )

        async def drain() -> None:
            assert process.stdout is not None
            async for raw in process.stdout:
                lines.append(raw.decode("utf-8", errors="replace").rstrip())
                # Published as it arrives rather than at the end. A tail that
                # only appeared once the run finished would answer "is this
                # moving?" exactly when nobody needs to ask any more.
                self._state = EvaluationRunState(
                    suite=suite,
                    status="running",
                    started_at=started,
                    finished_at=None,
                    exit_code=None,
                    recent_output=tuple(lines),
                )

        try:
            await asyncio.wait_for(drain(), timeout=self._timeout_seconds)
            await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
            seconds = self._timeout_seconds
            lines.append(f"评测超过 {seconds} 秒仍未结束，已经强制停止。")
        except asyncio.CancelledError:
            # The process outlives this task on purpose: cancelling the watcher
            # is not a request to stop the run, and killing it here would make
            # a shutdown that merely stopped watching also destroy the evidence
            # the run had already written.
            raise
        finally:
            code = process.returncode
            self._state = EvaluationRunState(
                suite=suite,
                # A killed run is a failed one. It produced whatever reports its
                # finished arms wrote, and those are on disk -- but the run as
                # asked for did not complete, and saying "succeeded" would make
                # a partial ablation look like a whole one.
                status="succeeded" if code == 0 else "failed",
                started_at=started,
                finished_at=datetime.now(UTC),
                exit_code=code,
                recent_output=tuple(lines),
            )


__all__ = ["COMMANDS", "KEPT_LINES", "SubprocessEvaluationLauncher"]
