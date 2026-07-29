"""What a Worker does with a Task it has just claimed.

The Task Registry holds the product lifecycle and the LangGraph checkpoint
holds the execution position. They are two facts in two places and this project
does not pretend they commit together, so picking a Task up means deciding what
those two facts *jointly* mean. Architecture baseline section 9.5 enumerates
that decision; this module is that enumeration, as a total function.

It is pure on purpose. The Worker that will call it has to hold an advisory
lock, a lease and a guard connection while it runs a graph, which makes every
one of these branches expensive to reach in a test. Written as a decision over
values, all of them are reachable in microseconds, and the Worker is left with
only the part that genuinely needs I/O.

Two branches -- an approval interrupt with and without a decision -- describe a
graph M3a cannot yet produce: ``approval`` is a side-effect-free placeholder
until WP10. They are here rather than deferred because the alternative is a
function that silently answers "resume" for an interrupted graph, and because
the input has to carry the approval anyway for the other branches to be sound.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.task_registry import (
    TERMINAL_STATUSES,
    ApprovalDecision,
    TaskStatus,
)
from agent_workbench.ports.task_workflow import CheckpointPosition, GraphVersion

ReconciliationAction = Literal[
    # The Registry already reached a terminal fact. Do not run a graph; make
    # the terminal state visible and let go.
    "propagate_terminal",
    # The graph that wrote this position cannot be run here. Never fall back to
    # another version: a node name that moved would silently resume somewhere
    # that means something else now.
    "wait_for_migration",
    # No checkpoint. The first invocation, built from the Task's own input.
    "start",
    # The graph finished but the Registry never learned it. Read the final
    # output and settle the Task idempotently.
    "settle_succeeded",
    # Interrupted at an approval nobody has decided. Release the lease and the
    # lock rather than hold execution resources open across a human decision.
    "wait_for_approval",
    # Interrupted at an approval that now has a decision. Resume the same
    # thread with it; the node re-reads the authoritative decision itself.
    "resume_with_approval",
    # Ordinary unfinished work on the same thread, with no input resubmitted.
    "resume",
]

#: The status each action moves the Registry to, or ``None`` where the action
#: leaves it alone. This is the half of the state machine the decision owns;
#: ``settle_succeeded`` is written by the Worker after it reads the graph's
#: output, so it appears here as the status that write must reach.
RESULTING_STATUS: Final[Mapping[ReconciliationAction, TaskStatus | None]] = {
    "propagate_terminal": None,
    "wait_for_migration": "waiting_migration",
    "start": None,
    "settle_succeeded": "succeeded",
    "wait_for_approval": "waiting_approval",
    "resume_with_approval": None,
    "resume": None,
}

#: Actions after which the Worker is still executing a graph. The rest release
#: the claim, and two of them (migration, approval) release it without the Task
#: being finished -- which is the point: neither is waiting on compute.
EXECUTING_ACTIONS: Final[frozenset[ReconciliationAction]] = frozenset(
    {"start", "resume", "resume_with_approval"}
)


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """One decision, with the sentence that justifies it.

    ``detail`` is for the event and the log. It is the reason a human reading
    a ``waiting_migration`` Task needs, and it is not a second encoding of the
    action: nothing branches on it.
    """

    action: ReconciliationAction
    detail: str
    approval_id: Identifier | None = None

    @property
    def resulting_status(self) -> TaskStatus | None:
        return RESULTING_STATUS[self.action]

    @property
    def keeps_executing(self) -> bool:
        return self.action in EXECUTING_ACTIONS


def reconcile(
    *,
    status: TaskStatus,
    graph_version: GraphVersion,
    position: CheckpointPosition | None,
    buildable_versions: Collection[GraphVersion],
    approval_decision: ApprovalDecision | None = None,
) -> Reconciliation:
    """Decide what to do with a claimed Task, from the two facts about it.

    ``graph_version`` is the Registry's -- what this Task was submitted to run.
    ``position.graph_version`` is the checkpoint's -- what actually wrote its
    execution position. They are separate inputs because they can disagree, and
    that disagreement is the whole of the migration case.
    """

    if status in TERMINAL_STATUSES:
        # First, and before anything reads the checkpoint. A cancelled Task
        # whose checkpoint still has pending nodes is not a Task to resume; it
        # is a Task somebody stopped.
        return Reconciliation(
            action="propagate_terminal",
            detail=f"the registry is already {status}",
        )

    if status == "waiting_migration":
        return Reconciliation(
            action="wait_for_migration",
            detail="the registry is already waiting for a migration decision",
        )

    if graph_version not in buildable_versions:
        # Unregistered here. A Worker deployed without this graph must not
        # substitute the newest one it happens to have.
        return Reconciliation(
            action="wait_for_migration",
            detail=f"graph version {graph_version} is not registered in this process",
        )

    if position is None:
        return Reconciliation(
            action="start",
            detail="no checkpoint exists for this task's thread",
        )

    if position.graph_version is None:
        return Reconciliation(
            action="wait_for_migration",
            detail="the checkpoint does not record which graph wrote it",
        )

    if position.graph_version != graph_version:
        return Reconciliation(
            action="wait_for_migration",
            detail=(
                f"the checkpoint was written by {position.graph_version}, "
                f"and the task is registered as {graph_version}"
            ),
        )

    # These two are mutually exclusive rather than merely ordered: the position
    # refuses to be constructed both finished and awaiting an approval, so
    # neither branch can hide the other.
    if position.finished:
        return Reconciliation(
            action="settle_succeeded",
            detail="the graph finished before the registry learned of it",
        )

    if position.awaiting_approval_id is not None:
        if approval_decision is None:
            return Reconciliation(
                action="wait_for_approval",
                detail="the graph is interrupted at an approval with no decision",
                approval_id=position.awaiting_approval_id,
            )
        return Reconciliation(
            action="resume_with_approval",
            detail=f"the approval was {approval_decision}",
            approval_id=position.awaiting_approval_id,
        )

    return Reconciliation(
        action="resume",
        detail=f"the checkpoint still has {len(position.pending_nodes)} node(s) to run",
    )


__all__ = [
    "EXECUTING_ACTIONS",
    "RESULTING_STATUS",
    "CheckpointPosition",
    "Reconciliation",
    "ReconciliationAction",
    "reconcile",
]
