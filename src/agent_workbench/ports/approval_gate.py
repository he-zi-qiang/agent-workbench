"""Asking a human, inside the loop, while the call waits.

This is not the Task approval boundary and must not be confused with it. A Task
approval is a pause across processes: the graph interrupts, the Worker releases
its lease and returns, a human decides hours later through the API, and some
worker -- not necessarily the original one -- resumes from the checkpoint.
Nothing is waiting in memory, and that is what makes it survivable.

A gate here is the opposite shape, and it is only safe where the opposite shape
is true. The coroutine that asked stays parked on the answer, so the answer has
to arrive in the same process and the same event loop. Where a run is executed
somewhere the deciding human cannot reach -- the Task worker, whose decisions
are recorded by the API process -- there is no gate to supply, and the gateway's
answer to an approval requirement remains a refusal. That is why the gateway
takes one of these optionally: absent is not "not built yet", it is "this
deployment cannot honestly wait".

The wait also costs whatever the run is holding while it waits. In the Task
worker that would be an execution lease and a pinned advisory-lock connection,
which is the second reason that process supplies no gate.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_workbench.domain.events import ApprovalDecidedBy, ApprovalDecision
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.tools import PermissionScope, ProposedToolName, ToolRisk


@runtime_checkable
class InteractiveApprovalGate(Protocol):
    """Somewhere a held call can be answered while it is still held."""

    async def request(
        self,
        *,
        approval_id: Identifier,
        tool_call_id: Identifier,
        tool_name: ProposedToolName,
        argument_digest: str,
        risk: ToolRisk | None,
        required_scopes: tuple[PermissionScope, ...],
        timeout_seconds: float,
    ) -> tuple[ApprovalDecision, ApprovalDecidedBy]:
        """Obtain a decision for one call, or say that none arrived.

        ``argument_digest`` is not passed so this can log it. It is passed
        because ``approve_for_session`` is otherwise unsafe to implement: the
        policy engine derives an approval requirement from the tool's declared
        risk and never reads the arguments, so a standing rule keyed by tool
        name alone would let one approved invocation stand for every later
        call of that tool. A rule remembered by this gate must be keyed by the
        arguments as well -- the digest is that identity, and it is the same
        one ``ToolProposed`` published for this call.

        ``timeout_seconds`` is the bound the caller has already committed to:
        it is the smaller of the caller's own approval allowance and whatever
        the run has left. An implementation should return
        ``("deny", "timeout")`` when it elapses, because only the gate knows
        that nobody answered rather than that something broke. The caller
        enforces the same bound regardless, so a gate that ignores it cannot
        hold a run open -- it only loses the chance to say why.

        Two obligations that are easy to miss:

        - This may be cancelled. The caller races it against the run's
          cancellation and cancels the loser, so an implementation must drop
          whatever pending state it registered when ``CancelledError``
          arrives; otherwise a cancelled run leaves a question on somebody's
          screen that no longer has an asker.
        - Raising is allowed and is treated as a refusal. Only the exception's
          type name crosses back, the same way a misbehaving policy engine is
          handled, because a message from deployment-supplied code has been
          known to carry a DSN.
        """
        ...


__all__ = ["InteractiveApprovalGate"]
