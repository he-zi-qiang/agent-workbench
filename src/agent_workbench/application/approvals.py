"""Answering an approval, and asking about one.

Reading an approval is an authorization boundary, and it is the same one a Task
read is: an approval belongs to an owner inside a tenant, so a query by id must
answer identically for "no such approval" and "not yours". Answering differently
is itself the disclosure -- the difference confirms the id exists -- which is why
both raise ``NotFoundError`` and neither reflects the id back.

Deciding authorizes first and mutates second, in that order and never the other
way round. A decision that reached the ledger before the ownership check would
have requeued somebody else's Task; that the ledger would then have refused a
*second* attempt is not a defence, because the first one already moved it.

Who decided is taken from the authenticated principal, never from the request.
A caller that could name the decider could record a colleague's approval, and
the ledger's audit trail is exactly the thing that must not be forgeable.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.task_registry import ApprovalDecision
from agent_workbench.ports.approvals import ApprovalRecord, ApprovalStore


@dataclass(frozen=True, slots=True)
class ApprovalService:
    """Read and decide the caller's own approvals."""

    approvals: ApprovalStore

    async def get(
        self, principal: PrincipalContext, approval_id: Identifier
    ) -> ApprovalRecord:
        """One approval, if it is this caller's.

        Raises ``NotFoundError`` when it does not exist *and* when it belongs to
        somebody else, because those two answers have to be the same one.
        """

        record = await self.approvals.get(approval_id)
        if record is None or not _belongs_to(record, principal):
            # Do not reflect the probed id. A guessed id that exists but is
            # owned by somebody else and an id that does not exist must have the
            # same status *and* the same public detail.
            raise NotFoundError("approval not found")
        return record

    async def decide(
        self,
        principal: PrincipalContext,
        approval_id: Identifier,
        *,
        decision: ApprovalDecision,
        decision_version: int,
    ) -> ApprovalRecord:
        """Record this caller's answer, and let the ledger requeue the Task.

        The ownership check runs first and on its own, so a cross-owner decision
        is refused before anything is written -- and refused the same way a read
        is, with the same 404, so the endpoint cannot be used to discover which
        approval ids exist.
        """

        await self.get(principal, approval_id)
        return await self.approvals.decide(
            approval_id,
            decision=decision,
            decision_version=decision_version,
            decided_by=principal.principal_id,
        )


def _belongs_to(record: ApprovalRecord, principal: PrincipalContext) -> bool:
    # Both, not either. A tenant match alone would expose one tenant's approvals
    # to every principal in it, and an owner match alone would let an id collide
    # across tenants into somebody else's decision.
    return (
        record.tenant_id == principal.tenant_id
        and record.owner_id == principal.principal_id
    )


__all__ = ["ApprovalService"]
