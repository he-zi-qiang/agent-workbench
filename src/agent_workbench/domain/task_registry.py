"""The Task's product lifecycle.

Deliberately not in :mod:`agent_workbench.domain.tasks`. That module is the
state the graph carries, and it says in its first paragraph that product status
belongs to the Task Registry rather than to ``TaskState``. Putting the status
vocabulary beside the graph's own fields is how the two start being confused.

These are the statuses the implementation plan's state machine names, and
nothing else. A status invented here would be one the coordination design has
not reasoned about -- fencing, cancellation and the stale-lease reaper are all
phrased in terms of this set. The set is duplicated once, as a database check
constraint, and a test asserts the two agree rather than trusting that whoever
adds the ninth status remembers both.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
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
    # Retried until the attempt budget ran out. Terminal for a Worker: it is
    # what stops a poison task from being picked up forever.
    "dead_letter",
]

#: No Worker may resume these. A late claim on one of them propagates the
#: terminal fact instead of running a graph.
TERMINAL_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {"succeeded", "failed", "cancelled", "dead_letter"}
)

#: The statuses cancellation has to work from, per the baseline's own list.
CANCELLABLE_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {"queued", "running", "waiting_approval"}
)

#: The states that must record why they are what they are. A Task nobody has
#: to act on carries no explanation, so a stale one cannot outlive the
#: transition that made it wrong.
EXPLAINED_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {"waiting_migration", "failed", "cancelled", "dead_letter"}
)

#: Every legal move, as data. The repository derives its conditional update
#: from this rather than restating each rule in SQL, because a transition table
#: written twice is one that disagrees with itself the first time an edge moves.
#:
#: Two properties are worth reading off it. Terminal statuses have no outgoing
#: edge at all, which is what "a late approval cannot reopen a cancelled Task"
#: means concretely. And ``waiting_migration`` also has none: nothing in the
#: plan says who resolves a migration or how, and an edge invented here would
#: be a procedure nobody has designed. It arrives with that procedure.
#:
#: Some edges have no method on the registry yet. ``running -> queued`` is the
#: stale-lease reaper's (WP08), ``running -> dead_letter`` is the attempt
#: budget's (WP08), and ``waiting_approval -> queued`` is the approval
#: decision's (WP10). They are here because the documents state them; the
#: registry's methods stay limited to what a single Worker performs.
ALLOWED_TRANSITIONS: Final[Mapping[TaskStatus, frozenset[TaskStatus]]] = (
    MappingProxyType(
        {
            "queued": frozenset({"running", "cancelled"}),
            "running": frozenset(
                {
                    "succeeded",
                    "failed",
                    "waiting_approval",
                    "waiting_migration",
                    "cancelled",
                    "queued",
                    "dead_letter",
                }
            ),
            "waiting_approval": frozenset({"queued", "cancelled"}),
            "waiting_migration": frozenset(),
            "succeeded": frozenset(),
            "failed": frozenset(),
            "cancelled": frozenset(),
            "dead_letter": frozenset(),
        }
    )
)


def sources_for(target: TaskStatus) -> frozenset[TaskStatus]:
    """Which statuses may become ``target``.

    This is the inverse of :data:`ALLOWED_TRANSITIONS`, and it is what a
    conditional update's ``WHERE status IN (...)`` is built from -- so the SQL
    cannot legalise an edge the table does not contain.
    """

    return frozenset(
        source for source, targets in ALLOWED_TRANSITIONS.items() if target in targets
    )


ApprovalDecision = Literal["approved", "rejected"]


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CANCELLABLE_STATUSES",
    "EXPLAINED_STATUSES",
    "TERMINAL_STATUSES",
    "ApprovalDecision",
    "TaskStatus",
    "sources_for",
]
