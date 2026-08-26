"""Which runs a stream holds, and which of them started which.

The tree is **rebuilt from the events**, not stored. Everything it needs is
already durable and already ordered: ``AgentDelegated`` names a child on its
parent's run, ``RunStarted`` opens a run, ``RunCompleted``/``RunFailed``/
``RunCancelled`` close one, and ``AgentCompleted`` carries what the child spent.
A second, persisted copy of the same shape would be a thing to keep in step with
the first, and the first is the one with the transaction boundary.

Two properties are worth stating before the code, because both are about runs
this function must **not** drop:

**A run that never finished is shown as running, not omitted.** A crashed Worker
leaves exactly that: a ``RunStarted`` with nothing after it. Omitting it would
make a crash look like work that was never attempted, which is the most
misleading possible reading of a half-executed Task.

**A child announced by a parent counts even if its own events are missing.**
``AgentDelegated`` is emitted before the child's first event, so between those
two writes the child exists and has said nothing. It appears as a node whose
status is unknown rather than as a gap in the parent's list.

The ordering is the event stream's own. Children are listed in the order their
delegations were announced, which is the order a reader watched them start --
not sorted by id, and not sorted by finish time, because both would reorder a
fan-out relative to the transcript it is being read beside.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from agent_workbench.domain.events import (
    AgentCompleted,
    AgentDelegated,
    EventEnvelope,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
)
from agent_workbench.domain.runs import BudgetUsage

#: What a node in the tree can be known to be.
#:
#: ``running`` and ``unknown`` are different, and collapsing them would hide the
#: distinction that matters after a crash: ``running`` means this run said it
#: started, ``unknown`` means a parent named it and nothing from it has arrived.
RunNodeStatus = Literal["running", "completed", "failed", "cancelled", "unknown"]


@dataclass(frozen=True, slots=True)
class RunNode:
    """One run, and the runs it started."""

    run_id: str
    parent_run_id: str | None
    #: Which sub-agent definition this run was started as, when a parent said
    #: so. ``None`` for a root run, which nobody delegated.
    definition_name: str | None
    status: RunNodeStatus
    #: What this run itself spent. Taken from its *own* terminal event where it
    #: has one, and from the parent's ``AgentCompleted`` otherwise -- the two
    #: agree, and the second exists for the case where a page holds the parent
    #: and not the child.
    usage: BudgetUsage
    #: The position of the event that opened this run, or of the delegation
    #: that named it. What the reader would scroll to.
    sequence: int | None
    children: tuple[RunNode, ...] = ()

    def flatten(self) -> tuple[RunNode, ...]:
        """This node and every descendant, parents before children."""

        return (self, *(node for child in self.children for node in child.flatten()))


@dataclass(frozen=True, slots=True)
class RunTree:
    """Every run a page of one stream showed, arranged by who started whom."""

    stream_id: str
    roots: tuple[RunNode, ...] = ()

    def nodes(self) -> tuple[RunNode, ...]:
        return tuple(node for root in self.roots for node in root.flatten())

    def get(self, run_id: str) -> RunNode | None:
        for node in self.nodes():
            if node.run_id == run_id:
                return node
        return None


@dataclass(slots=True)
class _Accumulator:
    """One run's facts as they arrive, before the tree is shaped."""

    run_id: str
    parent_run_id: str | None = None
    definition_name: str | None = None
    status: RunNodeStatus = "unknown"
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    sequence: int | None = None
    children: list[str] = field(default_factory=list["str"])


def build_run_tree(
    stream_id: str,
    envelopes: Iterable[EventEnvelope],
) -> RunTree:
    """Rebuild the run tree a page of events describes.

    Tolerant by construction, because the input is a *page*: a caller may hold
    the middle of a stream, the events of a run whose parent is not in view, or
    a parent whose children have not written yet. Every one of those produces a
    node rather than an exception -- a reader looking at a partial timeline is
    the normal case, not a corrupt one.
    """

    runs: dict[str, _Accumulator] = {}
    order: list[str] = []

    def accumulator(run_id: str) -> _Accumulator:
        held = runs.get(run_id)
        if held is None:
            held = _Accumulator(run_id=run_id)
            runs[run_id] = held
            order.append(run_id)
        return held

    for envelope in envelopes:
        payload = envelope.payload
        own = accumulator(envelope.run_id)
        if isinstance(payload, AgentDelegated):
            # The parent speaks first. Both halves are recorded here because
            # this is the only event that knows the relationship: the child's
            # own events carry its run id and nothing about who sent it.
            child = accumulator(payload.child_agent_run_id)
            child.parent_run_id = envelope.run_id
            child.definition_name = payload.profile_name
            if child.sequence is None:
                child.sequence = envelope.sequence
            if payload.child_agent_run_id not in own.children:
                own.children.append(payload.child_agent_run_id)
        elif isinstance(payload, AgentCompleted):
            child = accumulator(payload.child_agent_run_id)
            # Only where the child did not report for itself. Its own terminal
            # event is the first-hand account; this one exists for the page
            # that holds the parent and not the child.
            if child.status == "unknown":
                child.status = _STATUS_FOR_RUN_STATUS[payload.status]
            if child.usage == BudgetUsage():
                child.usage = payload.usage
        elif isinstance(payload, RunStarted):
            own.status = "running"
            if own.sequence is None:
                own.sequence = envelope.sequence
        elif isinstance(payload, RunCompleted):
            own.status = "completed"
            own.usage = payload.usage
        elif isinstance(payload, RunFailed):
            own.status = "failed"
            own.usage = payload.usage
        elif isinstance(payload, RunCancelled):
            own.status = "cancelled"
            own.usage = payload.usage

    return RunTree(stream_id=stream_id, roots=_shape(runs, order))


#: ``AgentCompleted`` reports a ``RunStatus``, which has three values; a node
#: has five. Written as a mapping rather than a cast so that a status added to
#: the domain has to be considered here rather than silently becoming
#: ``unknown``.
_STATUS_FOR_RUN_STATUS: dict[str, RunNodeStatus] = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _shape(
    runs: dict[str, _Accumulator],
    order: Sequence[str],
) -> tuple[RunNode, ...]:
    """Turn the flat accumulators into roots, without recursing forever.

    A run whose parent is not in this page is a root *of this page*. That is
    the honest answer for a caller holding the middle of a stream, and it is
    also what stops a delegated run from disappearing when its parent's
    ``AgentDelegated`` fell off the top.
    """

    def node(run_id: str, seen: frozenset[str]) -> RunNode:
        held = runs[run_id]
        # `seen` guards against a cycle, which no correct producer can create
        # -- `TraceContext` refuses a self-parent -- but a page assembled from
        # a corrupted store could, and an infinite recursion in a read model is
        # a much worse symptom than a truncated branch.
        children = tuple(
            node(child_id, seen | {run_id})
            for child_id in held.children
            if child_id in runs and child_id not in seen
        )
        return RunNode(
            run_id=held.run_id,
            parent_run_id=held.parent_run_id,
            definition_name=held.definition_name,
            status=held.status,
            usage=held.usage,
            sequence=held.sequence,
            children=children,
        )

    return tuple(
        node(run_id, frozenset())
        for run_id in order
        if runs[run_id].parent_run_id is None or runs[run_id].parent_run_id not in runs
    )


__all__ = ["RunNode", "RunNodeStatus", "RunTree", "build_run_tree"]
