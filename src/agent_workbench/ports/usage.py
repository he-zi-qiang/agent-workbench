"""What this tenant has spent, across all three modes.

**Why a read model and not a counter.** Every number here already exists: the
runtime writes one terminal event per run (``RunCompleted`` / ``RunFailed`` /
``RunCancelled``) carrying that run's whole ``BudgetUsage`` -- tokens *and*
``cost_micro_usd``, priced at write time by the profile's own rates. Adding a
running total somewhere would create a second answer to "how much did this
cost", and the two would disagree the first time a write was replayed or a
migration ran. This port sums the events instead, so there stays one fact
source and it is the log.

**Settled runs only, and that is a claim not an omission.** A run still in
flight has written no terminal event, so it contributes nothing here. The
alternative -- summing ``ModelCompleted.usage`` as calls land -- would include
in-flight work but would have to re-price it at read time, and re-pricing means
a total that changes when the config changes, retroactively. A report that says
"here is what finished" can be checked against the log; one that re-prices
history cannot. The reader returns the in-flight run count so a caller can say
which is which rather than quietly under-reporting.

**Tenancy comes from the joins, not from the events.** ``event_streams`` has no
tenant column, deliberately -- storing a derived one would be a fact nobody
established. So a tenant's usage is whatever hangs off the tables that *do*
know: ``task_runs`` for Task, ``chat_turns`` -> ``conversation_sessions`` for
Chat and Code. A run reachable from neither is not this tenant's and is not
counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from agent_workbench.domain.runs import TokenUsage

#: The three surfaces a person can spend from. Not an open string: a fourth
#: value here would silently create a fourth column in every caller, and the
#: modes are a closed set defined by `conversation_sessions.mode` plus Task.
UsageMode = Literal["chat", "code", "task"]

USAGE_MODES: tuple[UsageMode, ...] = ("chat", "code", "task")

#: The events that carry a run's final account. One per run, exactly -- which
#: is what makes summing them safe. `AgentCompleted` is deliberately absent:
#: it reports a child's usage *to its parent*, and the child writes its own
#: terminal event too, so counting both would double every delegated run.
TERMINAL_EVENT_TYPES: tuple[str, ...] = (
    "RunCompleted",
    "RunFailed",
    "RunCancelled",
)


@dataclass(frozen=True)
class UsageSlice:
    """One bucket's account.

    ``cost_micro_usd`` is what the runs themselves recorded, not something
    recomputed here. Where a profile has no configured prices that figure is
    zero -- and zero-because-unpriced is not zero-because-free, which is why
    ``UsageReport`` carries the list of unpriced profiles rather than leaving a
    reader to infer it from a total that looks like a bargain.
    """

    tokens: TokenUsage = field(default_factory=TokenUsage)
    cost_micro_usd: int = 0
    #: Settled runs in this bucket. The denominator for "average cost per run",
    #: and the honest answer to "is this total built on anything".
    runs: int = 0

    def merged(self, other: UsageSlice) -> UsageSlice:
        return UsageSlice(
            tokens=self.tokens.merged(other.tokens),
            cost_micro_usd=self.cost_micro_usd + other.cost_micro_usd,
            runs=self.runs + other.runs,
        )


@dataclass(frozen=True)
class UsageReport:
    """Everything the usage page asks for, in one round trip."""

    by_mode: dict[UsageMode, UsageSlice]
    #: Keyed by model profile name, as written into ``RunStarted``.
    by_model: dict[str, UsageSlice]
    #: The part of ``by_mode["task"]`` that delegated runs spent. Reported
    #: beside the mode rather than subtracted from it: a sub-agent's tokens are
    #: real spend on this tenant's bill, but they never counted against the
    #: parent run's budget, so the two numbers answer different questions and
    #: must not be added.
    delegated: UsageSlice
    #: Runs that have started and not yet written a terminal event. Not a
    #: usage figure -- a caveat, so a caller can say "plus N still running"
    #: instead of presenting a partial total as a complete one.
    runs_in_flight: int
    #: Profile names seen in this window whose recorded cost was zero across
    #: every run. Almost always "this deployment configured no prices for it";
    #: a genuinely free model would look the same, and both deserve the same
    #: footnote rather than a silent zero.
    unpriced_profiles: tuple[str, ...]
    #: The window actually covered. `since` is None when the caller asked for
    #: everything, and echoing it back is what lets a page title itself
    #: truthfully instead of repeating what it requested.
    since: datetime | None
    until: datetime


@runtime_checkable
class UsageReader(Protocol):
    """Read one tenant's spend over a window."""

    async def report(
        self,
        *,
        tenant_id: str,
        since: datetime | None,
        until: datetime,
    ) -> UsageReport:
        """Aggregate settled runs recorded for ``tenant_id`` within the window.

        ``until`` is exclusive and ``since`` inclusive, so consecutive windows
        tile without double counting the run that landed on the boundary.
        """
        ...


__all__ = [
    "TERMINAL_EVENT_TYPES",
    "USAGE_MODES",
    "UsageMode",
    "UsageReader",
    "UsageReport",
    "UsageSlice",
]
