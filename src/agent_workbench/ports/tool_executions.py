"""Framework-neutral boundary for the external side-effect ledger.

An external effect cannot be undone, and it cannot be made transactional with
this database. What can be made transactional is the *record* of intending it,
so the protocol is two writes around one dispatch:

1. record the intent, keyed by a stable business ``operation_key``;
2. call the outside world;
3. record what happened -- or, if that is not knowable, say so.

The third case is the one this ledger exists for. A process that dies between
the dispatch and the result leaves a row that says an effect was intended and
does not say whether it landed, and no amount of retrying can turn that into
knowledge. It becomes ``needs_reconciliation``: a state a human resolves. This
is deliberately not "retry until it works", because a retry of an effect that
already happened is a second effect, and it is not "assume it failed", because
that is the same thing with a story attached.

Two rules make the key trustworthy.

The key is a *business* key, not a ``tool_call_id``. A model that re-proposes a
call after a retry mints a new call id for the same intent, so keying on it
would make every retry a fresh operation -- which is precisely the duplicate the
ledger is here to prevent. Deriving a stable key is the tool's job; enforcing
what follows from it is this ledger's.

And one key means one request. Replaying an operation with the same canonical
arguments returns the stored row; replaying it with *different* arguments is a
different operation wearing the same name, and is refused rather than recorded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from agent_workbench.domain.artifacts import Sha256
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel, ShortText
from agent_workbench.domain.tools import ProposedToolName

#: What a caller may supply as a stable business key. Bounded and printable for
#: the same reasons an identifier is: it reaches logs, rows and error messages.
OperationKey = Identifier

ToolExecutionStatus = Literal[
    # The intent is recorded and the effect has not been reported. A row left
    # here by a dead process is not "failed" -- nobody knows what it is.
    "intended",
    "succeeded",
    # Reported as failed *by the dispatch itself*. Distinct from the state
    # below: this one is knowledge.
    "failed",
    # The window closed without an answer. A human decides what happened.
    "needs_reconciliation",
]

#: Statuses no further attempt may move. A succeeded operation must never be
#: dispatched again, and one awaiting a human must not be resolved by a Worker
#: that happens to claim the Task next.
SETTLED_STATUSES: frozenset[ToolExecutionStatus] = frozenset(
    {"succeeded", "failed", "needs_reconciliation"}
)


class ToolExecutionIntent(DomainModel):
    """What is about to be attempted, and under whose authority."""

    task_id: Identifier
    operation_key: OperationKey
    tool_name: ProposedToolName
    # The digest of the canonical arguments, not the arguments. A ledger row is
    # read by operators; tool arguments carry user text and retrieved passages,
    # and neither belongs in an audit table.
    canonical_request_hash: Sha256
    # Which claim is attempting this. The ledger refuses a write that does not
    # match the Task's live lease, so a Worker that lost ownership cannot
    # dispatch under it -- and cannot report a result for one either.
    lease_epoch: int = Field(ge=1)
    agent_run_id: Identifier
    # The provider-minted call id. Recorded for tracing and deliberately *not*
    # part of any key: a retried model turn mints a new one for the same intent.
    tool_call_id: Identifier
    # The effective policy this was authorized under -- revision and canonical
    # fingerprint together, so a rule set that kept its label but changed its
    # rules is still distinguishable after the fact.
    policy_identity: ShortText


class ToolExecutionRecord(DomainModel):
    """One operation, in whatever state it reached."""

    execution_id: Identifier
    task_id: Identifier
    operation_key: OperationKey
    tool_name: ProposedToolName
    canonical_request_hash: Sha256
    status: ToolExecutionStatus
    lease_epoch: int = Field(ge=1)
    agent_run_id: Identifier
    tool_call_id: Identifier
    policy_identity: ShortText
    # Present once the operation left ``intended``. Bounded: it is a reason for
    # a human, not a provider's response body.
    outcome_detail: ShortText | None = None
    intended_at: datetime
    settled_at: datetime | None = None

    @property
    def settled(self) -> bool:
        return self.status in SETTLED_STATUSES

    @property
    def may_dispatch(self) -> bool:
        """Whether the caller holding this record may call the outside world.

        Only an ``intended`` row may. This is a property rather than a check at
        the call site so that "the ledger already knows about this operation"
        cannot be read as permission by a caller that only looked at whether
        ``record_intent`` raised.

        It answers for the caller that just recorded the intent, and that is why
        an implementation may not return an ``intended`` row belonging to an
        older lease: see :meth:`ToolExecutionLedger.record_intent`. Such a row
        says an effect was attempted by somebody who is gone, and reading it as
        permission is the duplicate this ledger exists to prevent.
        """

        return self.status == "intended"


class ToolOperationConflictError(RuntimeError):
    """One operation key, two different requests.

    Returning the stored row for a repeated key is idempotency. Returning it for
    *different* arguments would be reporting that an effect nobody asked for had
    already been performed.
    """

    def __init__(
        self,
        *,
        task_id: str,
        operation_key: str,
        recorded_hash: str,
        attempted_hash: str,
    ) -> None:
        self.task_id = task_id
        self.operation_key = operation_key
        self.recorded_hash = recorded_hash
        self.attempted_hash = attempted_hash
        super().__init__(
            f"operation {operation_key} on task {task_id} was recorded with a "
            "different canonical request"
        )


class ToolExecutionNotWritableError(RuntimeError):
    """The ledger refused a write from a claim that is not the live one.

    Carries what was found, because "no rows matched" cannot distinguish a
    Worker whose lease expired from an operation another attempt already
    settled -- and those call for different responses.
    """

    def __init__(
        self,
        *,
        operation_key: str,
        found_status: str | None,
        found_lease_epoch: int | None,
        attempted_lease_epoch: int,
    ) -> None:
        self.operation_key = operation_key
        self.found_status = found_status
        self.found_lease_epoch = found_lease_epoch
        self.attempted_lease_epoch = attempted_lease_epoch
        super().__init__(
            f"operation {operation_key} cannot be written at epoch "
            f"{attempted_lease_epoch}: it is "
            f"{found_status or 'absent'} at epoch {found_lease_epoch}"
        )


@runtime_checkable
class ToolExecutionLedger(Protocol):
    """Record what is about to happen outside, and what did."""

    async def record_intent(self, intent: ToolExecutionIntent) -> ToolExecutionRecord:
        """Claim the operation, or return what a previous attempt recorded.

        The returned record answers the only question the caller has:
        :attr:`ToolExecutionRecord.may_dispatch`. A row that is already settled
        comes back settled, which is how a retry after a successful dispatch
        declines to perform the effect a second time.

        An ``intended`` row left by an *earlier* lease is settled here as
        ``needs_reconciliation`` and returned in that state. It is the one
        transition an implementation performs unasked, and the reason is that
        nothing else can: the Worker that recorded it is fenced out of the
        ledger, so nobody remains who could report what its dispatch did. The
        rule is a requirement rather than an implementation's discretion --
        without it the Worker that claims the Task next reads "an effect was
        intended" as "no effect has happened yet" and performs it again.

        Raises :class:`ToolOperationConflictError` when the key is reused for a
        different canonical request, and
        :class:`ToolExecutionNotWritableError` when the attempting lease is not
        the Task's live one.
        """
        ...

    async def record_result(
        self,
        *,
        task_id: Identifier,
        operation_key: OperationKey,
        lease_epoch: int,
        succeeded: bool,
        detail: str | None = None,
    ) -> ToolExecutionRecord:
        """Report what the dispatch did, under the lease that dispatched it."""
        ...

    async def mark_for_reconciliation(
        self,
        *,
        task_id: Identifier,
        operation_key: OperationKey,
        lease_epoch: int,
        detail: str,
    ) -> ToolExecutionRecord:
        """Record that the outcome is unknown, and that a human must resolve it.

        ``detail`` is required here and optional elsewhere: an operation parked
        for a person is one where the reason is the entire content of the row.
        """
        ...

    async def get(
        self, *, task_id: Identifier, operation_key: OperationKey
    ) -> ToolExecutionRecord | None: ...


__all__ = [
    "SETTLED_STATUSES",
    "OperationKey",
    "ToolExecutionIntent",
    "ToolExecutionLedger",
    "ToolExecutionNotWritableError",
    "ToolExecutionRecord",
    "ToolExecutionStatus",
    "ToolOperationConflictError",
]
