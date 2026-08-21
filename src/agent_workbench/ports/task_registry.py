"""Framework-neutral boundary for the Task Registry.

Every method is named after the thing that happened, not after the status it
writes. That is the implementation plan's rule -- transitions go through the
repository as conditional updates, and the layers above never hand it a status
string -- and it is what makes an illegal move impossible to express rather
than merely rejected.

An illegal move raises. It does not return ``None`` and it does not silently
do nothing: a Worker that settles a Task it no longer owns has to find out, and
"the update matched no rows" is exactly the signal a cancelled or reclaimed
Task produces.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal, Protocol, runtime_checkable

from pydantic import Field, StringConstraints, field_validator

from agent_workbench.domain.artifacts import Sha256
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.pagination import ListCursor
from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.schema import DomainModel, JsonObject, ShortText
from agent_workbench.domain.task_intent import TaskIntent
from agent_workbench.domain.task_registry import TaskStatus
from agent_workbench.domain.tools import PermissionScope
from agent_workbench.ports.task_workflow import GraphVersion

#: Why a Task stopped where it did, for a human. Bounded because it reaches
#: events and API responses, and unbounded free text there is a way to put a
#: provider's exception body somewhere it was never meant to travel.
TaskStatusDetail = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1024),
]

#: Enough of the objective to tell one Task from another in a list. The
#: authoritative objective stays in the input artifact and is not duplicated
#: here: this is a label, and a caller that needs the real text reads the
#: artifact. Bounded well below the objective's own 4096 so a list page cannot
#: become a way to pull every Task's full text in one unauthenticated-size
#: response.
OBJECTIVE_PREVIEW_LIMIT: Final[int] = 200
ObjectivePreview = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=OBJECTIVE_PREVIEW_LIMIT
    ),
]


def objective_preview(objective: str) -> ObjectivePreview | None:
    """Cut an objective down to its list label.

    Returns ``None`` for an objective that is empty once stripped, because a
    preview column that stores "" would make "no preview recorded" and "the
    objective was blank" the same value. Truncation is marked, so nobody reads
    a cut label as the whole objective.
    """

    collapsed = " ".join(objective.split())
    if collapsed == "":
        return None
    if len(collapsed) <= OBJECTIVE_PREVIEW_LIMIT:
        return collapsed
    return collapsed[: OBJECTIVE_PREVIEW_LIMIT - 1] + "…"


class IndexReservation(DomainModel):
    """One concrete Qdrant index, reserved for one Task.

    The alias that selected it is deliberately absent. An alias routes *new*
    requests and can move while a Task is mid-run, so a Task that stored one
    would silently change which corpus it was answering from. What it stores is
    the generation, and the Registry's foreign key to it is the reservation:
    while the Task exists, the generation cannot be deleted.
    """

    collection_name: Identifier
    index_version: ShortText
    generation_id: str


class IndexGenerationNotReservableError(RuntimeError):
    """The resolved generation stopped taking reservations before the commit.

    Not a failure to report to the caller: the alias moved or the generation
    began draining between resolution and commit, so the correct response is to
    resolve again and retry. The transaction is already rolled back, so nothing
    dangling was written.
    """

    def __init__(self, *, generation_id: str, found_status: str | None) -> None:
        self.generation_id = generation_id
        self.found_status = found_status
        found = found_status if found_status is not None else "no such generation"
        super().__init__(
            f"index generation {generation_id} cannot be reserved: it is {found}"
        )


class TaskSubmission(DomainModel):
    """What a caller has to supply to open a Task.

    ``submission_dedup_key`` is the caller's own idempotency key. Submitting it
    twice returns the first Task rather than opening a second, which is why it
    is required rather than optional: a submission with no key is one that
    cannot be retried safely.
    """

    tenant_id: Identifier
    owner_id: Identifier
    thread_id: Identifier
    graph_version: GraphVersion
    input_ref: Identifier
    # Content identity is separate from the generated artifact id. A retry can
    # upload equal bytes again and receive another artifact id, yet must still
    # resolve to the Task opened by the first submission.
    input_fingerprint: Sha256
    submission_dedup_key: Identifier
    # A label for lists, not the objective. Optional because a Task can be
    # submitted by a caller that has only an input reference -- the CLI resume
    # path does exactly that -- and a list row without a label is legible,
    # while refusing the submission for lacking one is not. Deliberately
    # excluded from _SUBMISSION_IDENTITY: it is derived from the input, so a
    # retry that agrees on input_fingerprint cannot disagree here meaningfully.
    objective_preview: ObjectivePreview | None = None
    # What the Task means, decided once. Required rather than defaulted: a
    # submission that could omit its semantics would produce a Task whose
    # resume has nothing to restore, and the omission would only be noticed
    # during recovery.
    run_semantics_snapshot: JsonObject
    run_semantics_revision: ShortText
    submitted_policy_revision: ShortText
    submitted_policy_fingerprint: ShortText
    submitted_authorization_envelope: AuthorizationEnvelope
    submitted_principal_scopes: tuple[PermissionScope, ...] = ()
    # Absent for a Task that touches no knowledge base. Present ones are
    # resolved before the transaction opens and reserved inside it.
    index_reservation: IndexReservation | None = None
    # Who decided this submission's shape (ADR-036). Provenance, not identity:
    # excluded from the Registry's column mapping and from
    # ``_SUBMISSION_IDENTITY`` -- a retry that re-ran triage and got different
    # words must still be the same submission -- and recorded only on the
    # ``TaskSubmitted`` event.
    intent: TaskIntent | None = None

    @field_validator("submitted_principal_scopes")
    @classmethod
    def normalize_submitted_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class TaskRun(DomainModel):
    """One Task's product lifecycle, as the Registry holds it."""

    task_id: Identifier
    tenant_id: Identifier
    owner_id: Identifier
    thread_id: Identifier
    graph_version: GraphVersion
    input_ref: Identifier
    input_fingerprint: Sha256
    submission_dedup_key: Identifier
    # Absent on Tasks submitted before this column existed, and on submissions
    # that carried only an input reference. Readers show the id in that case
    # rather than inventing a label.
    objective_preview: ObjectivePreview | None = None
    #: Which project this Task was submitted for, or none (ADR-071). A label,
    #: not an authorization fact: filing a Task under a project changes nothing
    #: about who may read it, and ``None`` is the normal state -- no migration
    #: filed anything anywhere.
    project_id: Identifier | None = None
    run_semantics_snapshot: JsonObject
    run_semantics_revision: ShortText
    submitted_policy_revision: ShortText
    submitted_policy_fingerprint: ShortText
    submitted_authorization_envelope: AuthorizationEnvelope
    submitted_principal_scopes: tuple[PermissionScope, ...] = ()
    # The reservation as stored: three columns rather than the submission's
    # nested value, because that is what the row holds and what a resume reads.
    # All three or none -- the database enforces the same.
    resolved_qdrant_collection: Identifier | None = None
    resolved_qdrant_index_version: ShortText | None = None
    resolved_qdrant_index_generation_id: str | None = None
    status: TaskStatus
    # Why this Task is on the queue again, when it is not new work. Set with the
    # approval that caused it, so a Worker can tell a decision from a retry
    # without inspecting the graph.
    resume_kind: Literal["approval"] | None = None
    resume_approval_id: Identifier | None = None
    status_detail: TaskStatusDetail | None = None
    lease_owner: Identifier | None = None
    lease_epoch: int = Field(default=0, ge=0)
    lease_until: datetime | None = None
    heartbeat_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    #: How many agent invocations this Task has paid for, across every retry
    #: and every reclaim (ADR-040). Distinct from ``attempt_count``, which
    #: counts claims: one claim runs many agent nodes, and a Task reclaimed
    #: after a crash keeps what it already spent.
    #:
    #: Present here from the migration that adds the column, not from the
    #: change that starts enforcing it. That is not scope creep -- this model
    #: forbids extra fields, so a column the Registry can read is a column this
    #: model must name or every read of the table fails validation. Nothing
    #: reads the number yet.
    agent_invocation_count: int = Field(default=0, ge=0)
    available_at: datetime
    created_at: datetime
    updated_at: datetime

    @field_validator("submitted_principal_scopes")
    @classmethod
    def normalize_submitted_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class ExecutionLease(DomainModel):
    """The fenced ownership token for one active Task execution.

    E1 fences Registry lifecycle writes only.  It deliberately does not fence
    the LangGraph checkpointer yet; that requires the E2 saver work.
    """

    task_id: Identifier
    worker_id: Identifier
    epoch: int = Field(ge=1)


class TaskClaim(DomainModel):
    """A running Task and the lease required to mutate it."""

    task: TaskRun
    lease: ExecutionLease


class TaskTransitionRejectedError(RuntimeError):
    """The Task was not in a status from which this move is legal.

    Carries the status actually found, because "the update matched no rows" on
    its own does not distinguish a Task somebody cancelled from one another
    Worker already settled.
    """

    def __init__(
        self,
        *,
        task_id: str,
        found_status: TaskStatus | None,
        attempted: TaskStatus,
    ) -> None:
        self.task_id = task_id
        self.found_status = found_status
        self.attempted = attempted
        found = found_status if found_status is not None else "no such task"
        super().__init__(f"task {task_id} cannot move to {attempted}: it is {found}")


class TaskSubmissionConflictError(RuntimeError):
    """One submission key, two different submissions.

    Returning the first Task for a repeated key is idempotency. Returning it
    for a *different* submission would be answering a question nobody asked.
    """

    def __init__(self, *, owner_id: str, submission_dedup_key: str) -> None:
        self.owner_id = owner_id
        self.submission_dedup_key = submission_dedup_key
        super().__init__(
            f"submission key {submission_dedup_key} already identifies a "
            f"different task for owner {owner_id}"
        )


class AgentInvocationBudgetExhaustedError(RuntimeError):
    """This Task has already paid for every agent invocation it was allowed.

    Terminal, and deliberately not a retry: the next claim reads the same full
    counter and would refuse again, so ``dead_letter`` is what this means --
    "trying again will not help" rather than "this attempt did not work".

    Separate from :class:`AgentInvocationCeilingMissingError` because the two
    have opposite dispositions. This one is a Task that misbehaved; that one is
    a deployment that cannot say what it allows.
    """

    def __init__(self, *, task_id: str, spent: int, ceiling: int) -> None:
        self.task_id = task_id
        self.spent = spent
        self.ceiling = ceiling
        super().__init__(
            f"task {task_id} has spent {spent} of {ceiling} allowed agent "
            f"invocations; refusing to start another"
        )


class AgentInvocationCeilingMissingError(RuntimeError):
    """The Task's own semantics snapshot does not say what it may spend.

    A defect in the deployment that submitted it, not a poison Task -- so it
    fails rather than dead-letters. Dead-lettering it would turn one
    configuration accident into a batch of Tasks nobody can revive.
    """

    def __init__(self, *, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(
            f"task {task_id} carries no "
            f"multi_agent.max_agent_invocation_attempts_per_task in its run "
            f"semantics snapshot, so how much it may spend is unknown"
        )


class StaleExecutionError(RuntimeError):
    """A Worker no longer owns a Task's current, unexpired lease."""

    def __init__(self, lease: ExecutionLease) -> None:
        self.lease = lease
        super().__init__(
            f"execution lease is stale for task {lease.task_id} at epoch {lease.epoch}"
        )


@runtime_checkable
class TaskRegistry(Protocol):
    """Open Tasks, hand out the next one, and record where each one stopped."""

    async def submit(self, submission: TaskSubmission) -> TaskRun:
        """Open a Task, or return the one this submission key already opened.

        Repeating a key with the same submission returns the same Task.
        Repeating it with a different one raises
        ``TaskSubmissionConflictError``.
        """
        ...

    async def get(self, task_id: Identifier) -> TaskRun | None: ...

    async def list_for_owner(
        self,
        *,
        tenant_id: Identifier,
        owner_id: Identifier,
        statuses: tuple[TaskStatus, ...] = (),
        limit: int,
        after: ListCursor | None = None,
    ) -> tuple[TaskRun, ...]:
        """This owner's Tasks, newest first, bounded and resumable.

        Scoped by tenant *and* owner in the query rather than filtered after
        it. A repository that returned more than the caller may see and left
        the narrowing to a service would make every future caller of this
        method a place the narrowing can be forgotten.

        An empty ``statuses`` means every status. It is a filter, never a
        grant: a caller cannot reach another owner's rows by naming one.
        """
        ...

    async def claim_next(
        self, worker_id: Identifier, *, lease_seconds: int
    ) -> TaskClaim | None:
        """Claim one eligible Task with a short PostgreSQL transaction."""
        ...

    async def heartbeat(
        self, lease: ExecutionLease, *, lease_seconds: int
    ) -> TaskRun: ...

    async def reserve_agent_invocation(self, lease: ExecutionLease) -> int:
        """Charge this Task for one agent invocation and return the new total.

        Called *before* the invocation runs, not after. A loop that crashes
        during every invocation would never reach an after-the-fact write, so
        the counter it is supposed to stop would never move -- and that loop is
        precisely what the ceiling exists for. The cost is written down rather
        than argued away: a crash between the charge and the call over-counts
        by one, which makes the real ceiling slightly smaller than configured
        rather than slightly larger.

        The write is fenced on the same predicate every other Registry write
        uses, so a Worker that has lost its claim raises ``StaleExecutionError``
        instead of spending a Task it no longer owns.

        Nothing in this release refuses on the returned number. It is recorded
        and reported so the count is visible before it is ever enforced --
        a ceiling whose first observable effect is a terminal Task is
        indistinguishable, to whoever is on call, from a bug.
        """
        ...

    async def reclaim_expired(
        self,
        *,
        limit: int,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> tuple[TaskRun, ...]: ...

    async def mark_succeeded(
        self, lease: ExecutionLease, *, detail: str | None = None
    ) -> TaskRun:
        """Settle a Task as succeeded, optionally with a caveat on the row.

        ``detail`` is not a second failure channel: it exists for the success
        that owes its reader a qualification -- ADR-060's "the reviewer still
        saw unresolved issues" -- and stays ``None`` for every success with
        nothing to confess.
        """
        ...

    async def mark_failed(self, lease: ExecutionLease, *, reason: str) -> TaskRun: ...

    async def mark_dead_lettered(
        self, lease: ExecutionLease, *, reason: str
    ) -> TaskRun:
        """Retire a Task that trying again cannot help.

        Separate from ``mark_failed`` because the two say different things to
        whoever finds the Task later: ``failed`` invites another attempt,
        ``dead_letter`` says the next one would end the same way. ``reason``
        must be legible enough to tell this writer's decision apart from the
        reaper's, which is the other producer of this status.
        """
        ...

    async def park_for_migration(
        self, lease: ExecutionLease, *, reason: str
    ) -> TaskRun:
        """Record that the Task's graph cannot be run as it stands.

        Terminal for a Worker but not for the Task: nothing here decides what
        happens next, because nothing in the plan yet says who performs a
        migration.
        """
        ...

    async def await_approval(self, lease: ExecutionLease) -> TaskRun:
        """Release the Task while a human decides.

        The Worker stops after this, so the Task must not be left in a status
        that claims something is executing.
        """
        ...

    async def release_for_retry(
        self, lease: ExecutionLease, *, delay_seconds: int
    ) -> TaskRun:
        """Release a live execution lease for a later, fenced retry."""
        ...

    async def cancel(self, task_id: Identifier, *, reason: str) -> TaskRun: ...

    async def delete(self, task_id: Identifier) -> None:
        """Remove one settled Task and everything that was only its.

        Terminal states only. A Task that is still queued, running or waiting
        on an approval is refused with
        :class:`TaskTransitionRejectedError` -- cancelling is how a caller
        reaches a state this will accept, and deleting is deliberately not a
        second way to stop something. Reusing cancellation's own path is what
        keeps the lease and epoch rules in one place instead of two.

        The whole stream or none of it (ADR-056): the Task's row, its
        approvals, its tool executions, its checkpoints and its event stream go
        together, in one transaction. Artifacts do not -- an input document or
        an exported report may be referenced from elsewhere, and deciding when
        the last reference is gone is a different question than this one.
        """
        ...


__all__ = [
    "OBJECTIVE_PREVIEW_LIMIT",
    "ExecutionLease",
    "IndexGenerationNotReservableError",
    "IndexReservation",
    "ObjectivePreview",
    "StaleExecutionError",
    "TaskClaim",
    "TaskRegistry",
    "TaskRun",
    "TaskSubmission",
    "TaskSubmissionConflictError",
    "TaskTransitionRejectedError",
    "objective_preview",
]
