"""The approval node, as LangGraph interrupts it.

Only one line of this module is framework-specific, and it is the pause.
Everything the node decides -- which approval it is asking about, and what the
answer was -- comes from :class:`~agent_workbench.workflows.approval.TaskApprovalGate`,
so the interrupt point and the ledger protocol are not two descriptions of the
same thing.

``interrupt()`` is not a return.  On the first pass it raises, and LangGraph
checkpoints the thread with the node still pending; on the resume pass the
**whole handler runs again** and ``interrupt()`` returns the resume payload
instead.  That is why opening the approval has to be idempotent: the node asks
its question twice for every one human decision.

The returned payload is deliberately discarded.  The ledger is asked again,
after the pause, by the id the node itself opened -- so resuming with a forged
payload wakes the node and changes nothing about what it reads.  Discarding a
value is easy to "clean up" later, which is why the discard is written as an
explicit statement with this comment attached rather than as an unused variable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

# langgraph ships no type stubs; narrowed here rather than by relaxing the type
# checker for the whole package, the same way the rest of this adapter does.
from langgraph.types import interrupt  # pyright: ignore[reportMissingTypeStubs]

from agent_workbench.domain.tasks import TaskState
from agent_workbench.workflows.approval import TaskApprovalGate

ApprovalNodeHandler = Callable[[TaskState], Awaitable[Mapping[str, Any]]]


def build_approval_node(gate: TaskApprovalGate) -> ApprovalNodeHandler:
    """Return the handler for the v1 graph's one human interrupt."""

    async def run(state: TaskState) -> Mapping[str, Any]:
        record = await gate.open(state)

        # Raises on the first pass. Returns on the resume pass -- and what it
        # returns is whoever-called-resume's word, so it is not used for
        # anything. The authority is the ledger read below.
        interrupt({"approval_id": record.approval_id})

        decision = await gate.decision(record.approval_id)
        return {"approval_id": record.approval_id, "approval_decision": decision}

    return run


__all__ = ["ApprovalNodeHandler", "build_approval_node"]
