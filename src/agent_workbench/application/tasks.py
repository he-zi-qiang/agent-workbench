"""Submitting a Task, and asking about one.

Two things live here that the Registry deliberately does not do.

The Registry takes a ``thread_id`` and a ``graph_version`` because they are
facts about a row. Choosing them is a decision, and leaving it to callers means
every interface has to invent an id nothing enforces the uniqueness of, and to
name a graph version that a deployment -- not a request -- is entitled to
choose. Both are minted here, once.

And reading a Task is an authorization boundary. A Task belongs to an owner
inside a tenant, so a query by id must answer the same way for "no such Task"
and "not yours". Answering differently is itself the disclosure: the difference
confirms the id exists. That is why this returns ``NotFoundError`` for both and
never a "forbidden".
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.events import EventEnvelope
from agent_workbench.domain.identifiers import Identifier, new_id
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.schema import DomainModel, JsonObject
from agent_workbench.ports.event_log import EventCursor, EventLogPort
from agent_workbench.ports.task_registry import TaskRegistry, TaskRun, TaskSubmission
from agent_workbench.ports.task_workflow import GraphVersion
from agent_workbench.workflows.research_graph import GRAPH_VERSION_V1

TASK_THREAD_PREFIX: Final[str] = "thr"

#: A timeline read is a client-supplied request. Unbounded, it is a way to ask
#: the server to hold a whole Task's history in memory on demand.
DEFAULT_TIMELINE_LIMIT: Final[int] = 200
MAX_TIMELINE_LIMIT: Final[int] = 500


def task_stream_id(task: TaskRun) -> Identifier:
    """The one stream a Task's events belong to.

    It is the workflow thread, and it is derived in exactly one place so the
    writer and the reader cannot disagree about where a Task's events went. A
    Task and a thread are one-to-one -- the Registry's unique constraint says
    so in both directions -- which is what lets a single ``(stream, sequence)``
    cursor mean "everything about this Task up to here".
    """

    return task.thread_id


class TaskTimeline(DomainModel):
    """One slice of a Task's events, and where to continue from.

    ``cursor`` is absent only for an empty slice. It is what a client sends
    back to resume, so it is the *last delivered* position rather than the end
    of the stream: a slice that stopped at the limit and one that reached the
    end resume identically.
    """

    task_id: Identifier
    events: tuple[EventEnvelope, ...]
    cursor: EventCursor | None = None


@dataclass(frozen=True, slots=True)
class SubmittedSemantics:
    """What a Task means, and under which rules it was granted.

    Assembled once, at submission, from settings and policy -- never from the
    request. A caller that could name its own semantics could pin a Task to a
    model, an index or a permission ceiling nobody deployed.

    The snapshot and the policy identity are separate on purpose. The snapshot
    is restored on resume; policy is re-evaluated every time, and these two
    fields only record which rules the caller was granted under, so the
    effective authorization stays "submitted envelope intersected with current
    policy" rather than "whatever was allowed then".
    """

    run_semantics_snapshot: JsonObject
    run_semantics_revision: str
    policy_revision: str
    policy_fingerprint: str
    authorization_envelope: AuthorizationEnvelope


@dataclass(frozen=True, slots=True)
class TaskService:
    """Open Tasks for a caller, and answer questions about their own."""

    registry: TaskRegistry
    # How a submission's semantics are produced. A callable rather than a
    # value, because the Qdrant alias a knowledge-using Task resolves is read
    # per submission (WP07-04), not once at startup.
    semantics: Callable[[], SubmittedSemantics]
    # Reading the timeline needs the log; opening and reading a Task do not.
    # Optional so a deployment that only submits does not have to wire one, and
    # so the M3a in-memory log and the WP07 durable one are the same swap.
    events: EventLogPort | None = None
    # Which graph a newly submitted Task runs. A deployment decision rather
    # than a request parameter: a caller that could name a version could pin
    # itself to one nobody deploys any more, or to one that means something
    # else now.
    graph_version: GraphVersion = GRAPH_VERSION_V1

    async def submit(
        self,
        principal: PrincipalContext,
        *,
        input_ref: Identifier,
        submission_dedup_key: Identifier,
        input_fingerprint: str | None = None,
    ) -> TaskRun:
        """Open a Task, or return the one this caller's key already opened.

        The thread id is minted here and never supplied. Two submissions of one
        key must reach the same Task, and a caller-supplied thread would let a
        retry hand the Registry a *different* thread for the same key -- which
        the unique constraint would then refuse, turning an idempotent retry
        into an error.
        """

        decided = self.semantics()
        return await self.registry.submit(
            TaskSubmission(
                tenant_id=principal.tenant_id,
                owner_id=principal.principal_id,
                thread_id=new_id(TASK_THREAD_PREFIX),
                graph_version=self.graph_version,
                input_ref=input_ref,
                input_fingerprint=(
                    input_fingerprint
                    if input_fingerprint is not None
                    else _reference_fingerprint(input_ref)
                ),
                submission_dedup_key=submission_dedup_key,
                run_semantics_snapshot=decided.run_semantics_snapshot,
                run_semantics_revision=decided.run_semantics_revision,
                submitted_policy_revision=decided.policy_revision,
                submitted_policy_fingerprint=decided.policy_fingerprint,
                submitted_authorization_envelope=decided.authorization_envelope,
                submitted_principal_scopes=principal.scopes,
            )
        )

    async def get(self, principal: PrincipalContext, task_id: Identifier) -> TaskRun:
        """One Task, if it is this caller's.

        Raises ``NotFoundError`` when it does not exist *and* when it belongs
        to somebody else, because those two answers have to be the same one.
        """

        task = await self.registry.get(task_id)
        if task is None or not _belongs_to(task, principal):
            # Do not reflect the probed id. A guessed id that exists but is
            # owned by somebody else and an id that does not exist must have
            # the same status *and* the same public detail.
            raise NotFoundError("task not found")
        return task

    async def cancel(
        self,
        principal: PrincipalContext,
        task_id: Identifier,
        *,
        reason: str,
    ) -> TaskRun:
        """Cancel one caller-owned Task through the Registry transition gate.

        Authorization happens before the transition, rather than by letting a
        caller hand an opaque id directly to the Registry.  Otherwise a
        cross-owner cancellation could mutate a row the caller was not allowed
        even to learn existed.
        """

        await self.get(principal, task_id)
        return await self.registry.cancel(task_id, reason=reason)

    async def timeline(
        self,
        principal: PrincipalContext,
        task_id: Identifier,
        *,
        after: EventCursor | None = None,
        limit: int = DEFAULT_TIMELINE_LIMIT,
    ) -> TaskTimeline:
        """This Task's events, in order, from ``after`` onwards.

        The authorization check is the same one ``get`` performs, and it runs
        *first*: a timeline read that answered differently for another owner's
        Task would leak exactly what the Task read refuses to.

        A cursor from another stream is refused rather than ignored. Ignoring
        it would silently serve this Task's history to a client that asked to
        continue a different one, and it is a client-supplied value.
        """

        if self.events is None:
            raise TimelineUnavailableError(
                "this task service was assembled without an event log"
            )
        if limit < 1:
            raise ValueError("limit must be positive")

        task = await self.get(principal, task_id)
        stream_id = task_stream_id(task)
        if after is not None and after.stream_id != stream_id:
            raise NotFoundError("task not found")

        recorded = await self.events.read(
            stream_id,
            after_sequence=None if after is None else after.sequence,
            limit=min(limit, MAX_TIMELINE_LIMIT),
        )
        return TaskTimeline(
            task_id=task.task_id,
            events=recorded,
            cursor=_cursor_after(stream_id, recorded, after),
        )


class TimelineUnavailableError(RuntimeError):
    """The service can open and read Tasks, but was given no event log."""


def _cursor_after(
    stream_id: Identifier,
    recorded: tuple[EventEnvelope, ...],
    previous: EventCursor | None,
) -> EventCursor | None:
    last = next(
        (event.sequence for event in reversed(recorded) if event.sequence is not None),
        None,
    )
    if last is None:
        # Nothing delivered, so the caller's position has not moved. Returning
        # the end of the stream instead would skip anything that arrives
        # between this read and the next one.
        return previous
    return EventCursor(stream_id=stream_id, sequence=last)


def _belongs_to(task: TaskRun, principal: PrincipalContext) -> bool:
    # Both, not either. A tenant match alone would expose one tenant's Tasks to
    # every principal in it, and an owner match alone would let an id collide
    # across tenants into somebody else's Task.
    return (
        task.tenant_id == principal.tenant_id
        and task.owner_id == principal.principal_id
    )


def _reference_fingerprint(input_ref: Identifier) -> str:
    """Give direct callers a stable fallback until they supply content bytes.

    ``TaskInputService`` always passes the canonical content digest. This
    fallback preserves idempotency for existing internal callers that only
    have an immutable input reference; it is intentionally not used by the
    public TaskInput path.
    """

    return hashlib.sha256(input_ref.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_TIMELINE_LIMIT",
    "MAX_TIMELINE_LIMIT",
    "TASK_THREAD_PREFIX",
    "SubmittedSemantics",
    "TaskService",
    "TaskTimeline",
    "TimelineUnavailableError",
    "task_stream_id",
]
