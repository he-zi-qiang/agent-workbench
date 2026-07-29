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
from typing import Annotated, Protocol, runtime_checkable

from pydantic import StringConstraints

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel
from agent_workbench.domain.task_registry import TaskStatus
from agent_workbench.ports.task_workflow import GraphVersion

#: Why a Task stopped where it did, for a human. Bounded because it reaches
#: events and API responses, and unbounded free text there is a way to put a
#: provider's exception body somewhere it was never meant to travel.
TaskStatusDetail = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1024),
]


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
    submission_dedup_key: Identifier


class TaskRun(DomainModel):
    """One Task's product lifecycle, as the Registry holds it."""

    task_id: Identifier
    tenant_id: Identifier
    owner_id: Identifier
    thread_id: Identifier
    graph_version: GraphVersion
    input_ref: Identifier
    submission_dedup_key: Identifier
    status: TaskStatus
    status_detail: TaskStatusDetail | None = None
    created_at: datetime
    updated_at: datetime


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

    async def start_next(self) -> TaskRun | None:
        """Move the oldest queued Task to ``running`` and return it.

        ``None`` when there is nothing queued. Ordering is oldest-first and
        nothing more: priority and concurrent claiming arrive with multiple
        Workers, and a single Worker needs neither.
        """
        ...

    async def mark_succeeded(self, task_id: Identifier) -> TaskRun: ...

    async def mark_failed(self, task_id: Identifier, *, reason: str) -> TaskRun: ...

    async def park_for_migration(self, task_id: Identifier, *, reason: str) -> TaskRun:
        """Record that the Task's graph cannot be run as it stands.

        Terminal for a Worker but not for the Task: nothing here decides what
        happens next, because nothing in the plan yet says who performs a
        migration.
        """
        ...

    async def await_approval(self, task_id: Identifier) -> TaskRun:
        """Release the Task while a human decides.

        The Worker stops after this, so the Task must not be left in a status
        that claims something is executing.
        """
        ...

    async def cancel(self, task_id: Identifier, *, reason: str) -> TaskRun: ...


__all__ = [
    "TaskRegistry",
    "TaskRun",
    "TaskSubmission",
    "TaskSubmissionConflictError",
    "TaskTransitionRejectedError",
]
