"""Starting an offline evaluation, and watching the one that is running.

Deliberately **not** modelled as a Task. `TaskState` is a set of graph channels,
and the Task subsystem carries budgets, checkpoints, lanes, leases and approvals
because a *graph* has to be recoverable across a worker's death. An evaluation
is a script that reads a fixed corpus and writes JSON. Wrapping it in a
single-node graph would inherit a recovery contract we would then have to lie
about -- there is nothing to resume, because the runner is not idempotent from
the middle.

Deliberately **not** a durable run table either. A row that told the truth about
a dead process needs a lease and a reaper (the machinery `docs/known-gaps.md`
F-01 already declined for Code), and it would buy nothing the report files do
not already give: `scripts/run_rag_eval.py` writes one report per arm as it
finishes, so a run killed halfway leaves real, complete evidence for the arms
that completed. In this repository the report file *is* the unit of evidence; a
run row would be a second, weaker one that could disagree with it.

So a run lives in the process that started it and dies with that process. That
is the same trade Code takes, for the same reason, and it is stated here rather
than discovered later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

#: The three runners this repository has. A `Literal` rather than a string,
#: because the value selects a fixed argv tuple on the server: a suite name that
#: reached a command line would be an injection surface with a very short path
#: from an HTTP body.
EvaluationSuite = Literal["rag", "chat", "triage"]

EvaluationStatus = Literal["running", "succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class EvaluationRunState:
    """What the one live -- or last -- run in this process is doing.

    ``recent_output`` is a tail, not a log. A caller watching a 70-minute run
    needs to see that it is still moving; the whole output belongs in the
    terminal that would have run the script, and shipping it through an HTTP
    poll would make the response grow without bound.
    """

    suite: EvaluationSuite
    status: EvaluationStatus
    started_at: datetime
    finished_at: datetime | None
    exit_code: int | None
    recent_output: tuple[str, ...]


class EvaluationBusyError(RuntimeError):
    """A run is already going, and this machine fits exactly one."""


class EvaluationDisabledError(RuntimeError):
    """This deployment does not start runs; its message says what to type."""


class EvaluationLauncher(Protocol):
    """Starts one runner and reports on it. One at a time, per process."""

    async def start(self, suite: EvaluationSuite) -> EvaluationRunState:
        """Begin a run, or raise :class:`EvaluationBusyError`.

        Returns as soon as the process is spawned -- not when it finishes.
        Anything else would hold an HTTP request open for over an hour.
        """
        ...

    def state(self) -> EvaluationRunState | None:
        """The current or most recent run, or ``None`` since this process began.

        ``None`` is not "nothing has ever been run": reports from earlier
        processes are still on disk. It means only that *this* process has not
        started one.
        """
        ...

    async def cancel(self) -> None:
        """Stop the running process, if there is one. A no-op if there is not."""
        ...


__all__ = [
    "EvaluationBusyError",
    "EvaluationDisabledError",
    "EvaluationLauncher",
    "EvaluationRunState",
    "EvaluationStatus",
    "EvaluationSuite",
]
