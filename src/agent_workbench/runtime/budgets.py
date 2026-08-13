"""Deadline arithmetic: the innermost bound wins, and it says which one it was.

Three limits can stop a single model call -- the model profile's own timeout,
the runtime's envelope for any model call, and whatever is left of the run's
deadline. The effective limit is their minimum, applied as one bound rather
than as three independent timers, because unrelated timers are how a run ends
up waiting on the longest of them.

The profile timeout is deliberately absent here. It belongs to the model
adapter, which is the only component that knows which concrete model a profile
maps to; the runtime nests its own bound outside the adapter's, so the shorter
one still fires first.

Which bound expired changes what the run should report. Running out of run
deadline is a budget outcome and the run is over; a model call that overran the
envelope is a provider problem and may be worth retrying. Callers need to tell
those apart, so the computation carries its source.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.runs import StopReason

DeadlineSource = Literal["none", "model_envelope", "run_deadline"]


def remaining_run_seconds(
    run_deadline: datetime | None,
    *,
    now: datetime,
) -> float | None:
    """Seconds left before the run's deadline, or ``None`` if it has none."""

    if run_deadline is None:
        return None
    return (run_deadline - now).total_seconds()


@dataclass(frozen=True, slots=True)
class ModelCallDeadline:
    """How long one model call may take, and what decided that."""

    seconds: float | None
    source: DeadlineSource

    @property
    def expired(self) -> bool:
        return self.seconds is not None and self.seconds <= 0

    def stop_reason(self) -> StopReason:
        return "deadline" if self.source == "run_deadline" else "error"

    def to_error(self) -> ErrorInfo:
        if self.source == "run_deadline":
            return ErrorInfo(
                code="budget_exceeded",
                message="the run reached its deadline during a model call",
            )
        return ErrorInfo(
            code="provider_error",
            message=(f"the model call exceeded the runtime's {self.seconds}s envelope"),
            retryable=True,
        )


def effective_model_deadline(
    *,
    envelope_seconds: float | None,
    run_deadline: datetime | None,
    now: datetime,
) -> ModelCallDeadline:
    """Return the shortest applicable bound for one model call."""

    candidates: list[tuple[float, DeadlineSource]] = []
    if envelope_seconds is not None:
        candidates.append((envelope_seconds, "model_envelope"))

    remaining = remaining_run_seconds(run_deadline, now=now)
    if remaining is not None:
        candidates.append((remaining, "run_deadline"))

    if not candidates:
        return ModelCallDeadline(seconds=None, source="none")

    # Ties go to the run deadline: it is the bound whose expiry ends the run
    # rather than just this call, and reporting the stricter consequence of two
    # equal limits is the safer default.
    seconds, source = min(
        candidates, key=lambda item: (item[0], item[1] != "run_deadline")
    )
    return ModelCallDeadline(seconds=seconds, source=source)


def effective_tool_timeout(
    spec_timeout_seconds: int,
    *,
    run_budget_seconds: float | None,
    deployment_ceiling_seconds: float | None = None,
) -> float:
    """Bound one tool call by its own timeout and by the run's remaining time.

    An outer deadline has to constrain inner work; a tool allowed an hour
    inside a run with ten seconds left would outlive the run that authorized
    it.

    ``deployment_ceiling_seconds`` is the operator's own bound on any single
    tool call (``runtime.tool_timeout_seconds``), and is normally unset. It can
    only ever shorten a call: a deployment may refuse to wait as long as a tool
    asks, but may not grant one more time than the tool believes it needs, so
    raising it is not a way to fix a tool whose declared timeout is too small.
    """

    limits = [
        float(limit)
        for limit in (
            spec_timeout_seconds,
            run_budget_seconds,
            deployment_ceiling_seconds,
        )
        if limit is not None
    ]
    return min(limits)


__all__ = [
    "DeadlineSource",
    "ModelCallDeadline",
    "effective_model_deadline",
    "effective_tool_timeout",
    "remaining_run_seconds",
]
