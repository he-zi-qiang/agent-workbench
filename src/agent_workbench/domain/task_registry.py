"""The Task's product lifecycle.

Deliberately not in :mod:`agent_workbench.domain.tasks`. That module is the
state the graph carries, and it says in its first paragraph that product status
belongs to the Task Registry rather than to ``TaskState``. Putting the status
vocabulary beside the graph's own fields is how the two start being confused.

These are the statuses the architecture baseline names, and nothing else. A
status invented here would be one the coordination design has not reasoned
about -- fencing, cancellation and the stale-lease reaper are all phrased in
terms of this set.
"""

from __future__ import annotations

from typing import Final, Literal

TaskStatus = Literal[
    # Waiting to be claimed. The only status a Worker's claim query matches.
    "queued",
    # Claimed, with a live lease. Every checkpoint write is fenced against it.
    "running",
    # Interrupted at an approval whose decision has not been made. The Worker
    # released its lease and its lock: nothing is executing.
    "waiting_approval",
    # The graph that wrote this Task's execution position cannot be run here.
    # Not terminal, and not resumable either: a human decides what happens.
    "waiting_migration",
    "succeeded",
    "failed",
    "cancelled",
]

#: No Worker may resume these. A late claim on one of them propagates the
#: terminal fact instead of running a graph.
TERMINAL_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {"succeeded", "failed", "cancelled"}
)

#: The statuses cancellation has to work from, per the baseline's own list.
CANCELLABLE_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {"queued", "running", "waiting_approval"}
)

ApprovalDecision = Literal["approved", "rejected"]


__all__ = [
    "CANCELLABLE_STATUSES",
    "TERMINAL_STATUSES",
    "ApprovalDecision",
    "TaskStatus",
]
