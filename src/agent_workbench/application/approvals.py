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
from typing import Final

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.pagination import ListCursor
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.schema import DomainModel
from agent_workbench.domain.task_registry import ApprovalDecision
from agent_workbench.ports.approvals import (
    ApprovalRecord,
    ApprovalStatus,
    ApprovalStore,
)

#: A person's queue, not a report. Bounded here rather than at the route so a
#: CLI cannot ask for more than an HTTP client can.
DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200


class ApprovalPage(DomainModel):
    """One page of a caller's approvals, and where to continue from."""

    approvals: tuple[ApprovalRecord, ...]
    cursor: ListCursor | None = None


@dataclass(frozen=True, slots=True)
class ApprovalService:
    """Read and decide the caller's own approvals."""

    approvals: ApprovalStore

    async def list(
        self,
        principal: PrincipalContext,
        *,
        statuses: tuple[ApprovalStatus, ...] = (),
        limit: int = DEFAULT_PAGE_LIMIT,
        after: ListCursor | None = None,
    ) -> ApprovalPage:
        """This caller's own approvals, newest first.

        This is the discovery path that did not exist. Before it, the only way
        to find a pending approval was to read a Task's timeline and pull the
        id out of an event -- so a person could not answer a question they had
        no way to learn was being asked.

        No id is probed, so there is no 404 to be careful about: a caller lists
        as itself and another owner's queue is simply not in the result.
        """

        if limit < 1:
            raise ValueError("limit must be positive")
        bounded = min(limit, MAX_PAGE_LIMIT)
        records = await self.approvals.list_for_owner(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            statuses=statuses,
            limit=bounded,
            after=after,
        )
        return ApprovalPage(approvals=records, cursor=_page_cursor(records, bounded))

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


def _page_cursor(records: tuple[ApprovalRecord, ...], limit: int) -> ListCursor | None:
    """Where to continue, or nothing when the page did not fill."""

    if not records or len(records) < limit:
        return None
    last = records[-1]
    return ListCursor(created_at=last.created_at, last_id=last.approval_id)


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "ApprovalPage",
    "ApprovalService",
]
