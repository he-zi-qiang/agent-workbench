"""Starting an offline evaluation, and reading the reports that exist.

Two things that look like one. Reading is always available and always answers
the same for every principal in the deployment -- these are files in the git
tree, not one principal's data. Starting is gated, because it needs an
embedding runtime and a Qdrant that being able to serve HTTP does not imply.

The scores are passed through as an open mapping and never renamed. The three
suites do not share a metric set, and normalising them into one shape is
precisely what ADR-039 forbids: a metric name is a promise about how a number
was computed, so the API reports what the runner wrote or reports nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from agent_workbench.ports.evaluation_runs import (
    EvaluationDisabledError,
    EvaluationLauncher,
    EvaluationRunState,
    EvaluationSuite,
)

#: How to start a run by hand, per suite. Taken from each script's own
#: docstring, and returned by the API so that a deployment which cannot launch
#: one still tells its reader exactly what to type. A message that only said
#: "not enabled" would leave them to find this themselves.
HOW_TO_RUN: dict[EvaluationSuite, str] = {
    "rag": (
        "AGENT_WORKBENCH_TEST_QDRANT_URL=http://localhost:6333 "
        "uv run --extra embedding python scripts/run_rag_eval.py"
    ),
    "chat": "uv run --extra embedding python scripts/run_chat_eval.py",
    "triage": "uv run python scripts/run_triage_eval.py",
}

#: Where each suite's runner writes, relative to `reports_root`. Spelled out
#: rather than globbed from the tree: a directory that appeared for another
#: reason would otherwise be served as though it held evaluation evidence.
REPORT_DIRECTORIES: dict[EvaluationSuite, str] = {
    "rag": "rag/reports",
    "chat": "chat/reports",
    "triage": "triage/reports",
}


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """One report file, whole.

    ``payload`` is the runner's own JSON object, unmodified. Picking fields out
    of it was the first shape, and it was wrong for the same reason renaming a
    metric would be: the console needs `gold_digest` and `question_count` to say
    which question set a number answered, and a layer that chose `scores` alone
    had already decided -- for every future reader and every future suite --
    which parts of a measurement matter.
    """

    suite: EvaluationSuite
    name: str
    payload: dict[str, Any]


@dataclass(slots=True)
class EvaluationService:
    """Reads reports off disk, and starts at most one runner."""

    launcher: EvaluationLauncher
    reports_root: Path
    runs_enabled: bool

    def reports(self) -> tuple[EvaluationReport, ...]:
        """Every report on disk, whichever process wrote it and whenever.

        Reads the tree on each call rather than caching. A run finishing is the
        only thing that changes the answer, and it changes it in a directory
        this process does not own -- a cache would have to be invalidated by
        something it cannot observe.
        """

        found: list[EvaluationReport] = []
        for suite, relative in REPORT_DIRECTORIES.items():
            directory = self.reports_root / relative
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                # No name-based filter. The `-outcomes.json` dumps the runner
                # writes under a flag carry no `scores` key, so the check below
                # already excludes them -- and a second rule keyed on the
                # filename would look like it was guarding something while
                # actually guarding nothing, which is worse than either.
                payload = _report_in(path)
                if payload is None:
                    continue
                found.append(
                    EvaluationReport(suite=suite, name=path.stem, payload=payload)
                )
        return tuple(found)

    async def start(self, suite: EvaluationSuite) -> EvaluationRunState:
        if not self.runs_enabled:
            raise EvaluationDisabledError(
                "这个部署不从界面发起评测。手动运行：" + HOW_TO_RUN[suite]
            )
        return await self.launcher.start(suite)

    def current(self) -> EvaluationRunState | None:
        return self.launcher.state()

    async def cancel(self) -> None:
        await self.launcher.cancel()


def _report_in(path: Path) -> dict[str, Any] | None:
    """A report object, or ``None`` if this file is not one.

    A `scores` mapping is what makes a file a report here. That is the one
    structural claim this layer makes about a runner's output, and it is the
    minimum needed to tell a score file from the per-question dump beside it --
    which carries no such key, so no rule has to know what `-outcomes` means.

    Unreadable and unrecognised files are skipped rather than raised on. This
    directory is written by scripts and read by a person's editor; one stray
    file must not take the whole page down, and a page that showed the other
    reports is more useful than a 500 that shows none.
    """

    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    payload = cast("dict[str, Any]", parsed)
    return payload if isinstance(payload.get("scores"), dict) else None


__all__ = [
    "HOW_TO_RUN",
    "REPORT_DIRECTORIES",
    "EvaluationReport",
    "EvaluationService",
]
