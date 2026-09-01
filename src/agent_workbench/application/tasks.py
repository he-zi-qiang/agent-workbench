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

The timeline read has one more property worth naming: a single stored row this
process cannot decode used to make a whole Task's history unreachable. Where
the log offers an isolating replay it is used, and the positions that were
skipped are returned alongside the events -- an empty ``skipped_sequences`` is
the claim that the slice is complete, so a caller that shows a timeline can
only present a partial one as whole by ignoring a field it was handed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

from agent_workbench.application.run_tree import RunNode, build_run_tree
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.events import EventEnvelope
from agent_workbench.domain.identifiers import Identifier, new_workflow_thread_id
from agent_workbench.domain.pagination import ListCursor
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.schema import DomainModel, JsonObject
from agent_workbench.domain.task_intent import TaskIntent
from agent_workbench.domain.task_registry import TaskStatus
from agent_workbench.ports.event_log import EventCursor, EventLogPort
from agent_workbench.ports.task_registry import TaskRegistry, TaskRun, TaskSubmission
from agent_workbench.ports.task_workflow import GraphVersion
from agent_workbench.workflows.general_graph import GRAPH_VERSION_V2
from agent_workbench.workflows.research_graph import GRAPH_VERSION_V1

#: What a submitter may ask for. Deliberately a shape rather than a version
#: string (ADR-031 §2.3): a caller naming ``graph_version`` directly could pin
#: itself to a version nobody deploys any more, or to one that means something
#: else now. Since ADR-036 the shape may be *proposed* by triage before
#: submission -- but what arrives here is always an explicit shape or nothing,
#: never "let the server guess", so the freezing and idempotency below are
#: untouched by who chose it.
#:
#: These names outlive the graphs behind them. Bumping v1 to v1.1 changes the
#: mapping below and nothing a client sends.
TaskGraphChoice = Literal["research", "general"]

#: Which graph each shape runs. One place, and deliberately not derived from
#: the adapter's builder registry: what a process can *compile* and what a
#: submitter may *ask for* are different questions, and a deployment running a
#: graph it does not offer at submission is a legitimate state.
GRAPH_FOR_CHOICE: Final[Mapping[TaskGraphChoice, GraphVersion]] = {
    "research": GRAPH_VERSION_V1,
    "general": GRAPH_VERSION_V2,
}

#: A timeline read is a client-supplied request. Unbounded, it is a way to ask
#: the server to hold a whole Task's history in memory on demand.
DEFAULT_TIMELINE_LIMIT: Final[int] = 200
MAX_TIMELINE_LIMIT: Final[int] = 500

#: Same reasoning for a list page. The ceiling is enforced here rather than at
#: the route so a CLI or a test cannot ask for more than an HTTP client can.
DEFAULT_PAGE_LIMIT: Final[int] = 50
MAX_PAGE_LIMIT: Final[int] = 200


def task_stream_id(task: TaskRun) -> Identifier:
    """The one stream a Task's events belong to.

    It is the workflow thread, and it is derived in exactly one place so the
    writer and the reader cannot disagree about where a Task's events went. A
    Task and a thread are one-to-one -- the Registry's unique constraint says
    so in both directions -- which is what lets a single ``(stream, sequence)``
    cursor mean "everything about this Task up to here".
    """

    return task.thread_id


class TaskPage(DomainModel):
    """One page of a caller's Tasks, and where to continue from.

    ``cursor`` is present only when the page filled its limit. An absent cursor
    says "this is the end", and a page that stopped short of the limit cannot
    have more behind it -- so returning one anyway would send every client on a
    guaranteed-empty extra round trip, and returning one for an empty page
    would make "no Tasks" indistinguishable from "keep going".
    """

    tasks: tuple[TaskRun, ...]
    cursor: ListCursor | None = None


class TaskTimeline(DomainModel):
    """One slice of a Task's events, what it skipped, and where to continue.

    ``cursor`` is absent only for a slice that examined nothing. It is what a
    client sends back to resume, so it is the last position this slice
    *reached* rather than the end of the stream: a slice that stopped at the
    limit and one that reached the end resume identically. Reached, not
    delivered -- a slice whose last row was undecodable still moves the caller
    past it, or the next request would meet the same row and no client would
    ever advance. A slice can therefore carry a cursor and no events at all.

    ``skipped_sequences`` names the positions this slice examined and could not
    deliver. It is a field rather than a log line because a partial history
    presented as a whole one is the failure this path introduces: the rows are
    still in the log, but this caller did not receive them, and the only honest
    way to hand back a shorter tuple is to hand back what is missing with it.
    """

    task_id: Identifier
    events: tuple[EventEnvelope, ...]
    cursor: EventCursor | None = None
    skipped_sequences: tuple[int, ...] = ()

    @property
    def skipped(self) -> int:
        """How many stored positions this slice could not deliver."""

        return len(self.skipped_sequences)


class _QuarantinedEvent(Protocol):
    """The one thing a timeline needs from a row it did not receive."""

    @property
    def sequence(self) -> int: ...


class _ReplayPage(Protocol):
    """One page of a replay: what arrived, what did not, how far it looked."""

    @property
    def events(self) -> tuple[EventEnvelope, ...]: ...
    @property
    def quarantined(self) -> tuple[_QuarantinedEvent, ...]: ...
    @property
    def resume_after(self) -> int | None: ...


@runtime_checkable
class IsolatingEventLog(Protocol):
    """A log that can replay past a row it cannot decode, and name it.

    Structural, and stated here rather than added to ``EventLogPort``, for two
    reasons that point the same way. The capability belongs to a store that can
    see raw rows -- a port method returning "everything except what broke"
    would make degraded replay the contract for every implementation, including
    ones with no way to hold a damaged row. And this layer must not import the
    adapter that has it (``tests/architecture/test_dependency_boundaries.py``),
    so the shape is what travels, not the class.

    A log without it is not a lesser log: it keeps the strict read, which is
    the behaviour this method had before isolation existed.
    """

    async def read_isolating(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
    ) -> _ReplayPage: ...


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
    # Which graph a Task runs when its submitter did not choose one. A
    # deployment decision, and still not a version a request may name: the
    # optional ``graph`` argument below is a closed set of *shapes*, mapped
    # here, so a caller cannot pin itself to a version nobody deploys any more
    # or to one that means something else now.
    graph_version: GraphVersion = GRAPH_VERSION_V1

    async def submit(
        self,
        principal: PrincipalContext,
        *,
        input_ref: Identifier,
        submission_dedup_key: Identifier,
        input_fingerprint: str | None = None,
        objective_preview: str | None = None,
        graph: TaskGraphChoice | None = None,
        intent: TaskIntent | None = None,
    ) -> TaskRun:
        """Open a Task, or return the one this caller's key already opened.

        The thread id is minted here and never supplied. Two submissions of one
        key must reach the same Task, and a caller-supplied thread would let a
        retry hand the Registry a *different* thread for the same key -- which
        the unique constraint would then refuse, turning an idempotent retry
        into an error.

        The graph is resolved here and stored on the row, which is what freezes
        it (ADR-031 §2.3). A Task already running does not change shape because
        somebody redeployed with another default -- the Worker reads the row,
        not the configuration -- and the deployment default applies only to the
        submissions that did not choose. ``graph_version`` is one of the two
        fields the Registry compares on an idempotent retry, so the same key
        submitted with a different graph is a conflict rather than a silent
        return of the first Task.
        """

        decided = self.semantics()
        return await self.registry.submit(
            TaskSubmission(
                tenant_id=principal.tenant_id,
                owner_id=principal.principal_id,
                thread_id=new_workflow_thread_id(),
                graph_version=(
                    self.graph_version if graph is None else GRAPH_FOR_CHOICE[graph]
                ),
                input_ref=input_ref,
                input_fingerprint=(
                    input_fingerprint
                    if input_fingerprint is not None
                    else _reference_fingerprint(input_ref)
                ),
                submission_dedup_key=submission_dedup_key,
                objective_preview=objective_preview,
                run_semantics_snapshot=decided.run_semantics_snapshot,
                run_semantics_revision=decided.run_semantics_revision,
                submitted_policy_revision=decided.policy_revision,
                submitted_policy_fingerprint=decided.policy_fingerprint,
                submitted_authorization_envelope=decided.authorization_envelope,
                submitted_principal_scopes=principal.scopes,
                intent=intent,
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

    async def list(
        self,
        principal: PrincipalContext,
        *,
        statuses: tuple[TaskStatus, ...] = (),
        limit: int = DEFAULT_PAGE_LIMIT,
        after: ListCursor | None = None,
    ) -> TaskPage:
        """This caller's own Tasks, newest first.

        There is no id to probe here and therefore no 404 to be careful about:
        a caller lists as itself, and a tenant or owner it is not gets an empty
        page rather than a refusal. The narrowing happens in the Registry query,
        not here, so this method cannot be the one place it was left out.
        """

        if limit < 1:
            raise ValueError("limit must be positive")
        bounded = min(limit, MAX_PAGE_LIMIT)
        tasks = await self.registry.list_for_owner(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            statuses=statuses,
            limit=bounded,
            after=after,
        )
        return TaskPage(tasks=tasks, cursor=_page_cursor(tasks, bounded))

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

    async def delete(
        self,
        principal: PrincipalContext,
        task_id: Identifier,
    ) -> None:
        """Delete one caller-owned settled Task, its records and its stream.

        Authorized the same way cancellation is, and for the same reason: the
        ``get`` in front makes "not yours" and "not there" the same answer, so
        deleting cannot be used to discover that a Task exists.

        Whether the Task is settled enough to delete is the Registry's
        judgement, not this method's -- it is the thing that holds the state
        machine, and duplicating the rule here is how two answers to one
        question start disagreeing.
        """

        await self.get(principal, task_id)
        await self.registry.delete(task_id)

    async def timeline(
        self,
        principal: PrincipalContext,
        task_id: Identifier,
        *,
        after: EventCursor | None = None,
        limit: int = DEFAULT_TIMELINE_LIMIT,
        run_id: Identifier | None = None,
    ) -> TaskTimeline:
        """This Task's events, in order, from ``after`` onwards.

        ``run_id`` narrows to one run inside the Task's stream (ADR-083) --
        which since delegation includes runs a model started mid-loop. It is a
        parameter here rather than a second endpoint because the authorization
        is the same authorization and the cursor is the same cursor: narrowing
        must not become a path with its own answer to "may this principal read
        this Task".

        The authorization check is the same one ``get`` performs, and it runs
        *first*: a timeline read that answered differently for another owner's
        Task would leak exactly what the Task read refuses to.

        A cursor from another stream is refused rather than ignored. Ignoring
        it would silently serve this Task's history to a client that asked to
        continue a different one, and it is a client-supplied value.

        A log that can isolate an undecodable row is asked to, because the
        alternative is that one such row makes the whole Task unreadable. What
        it skipped comes back in ``skipped_sequences``; nothing about the
        strict read changes for a log that cannot.
        """

        log = self.events
        if log is None:
            raise TimelineUnavailableError(
                "this task service was assembled without an event log"
            )
        if limit < 1:
            raise ValueError("limit must be positive")

        task = await self.get(principal, task_id)
        stream_id = task_stream_id(task)
        if after is not None and after.stream_id != stream_id:
            raise NotFoundError("task not found")

        after_sequence = None if after is None else after.sequence
        # Capped identically on both paths. The bound is what keeps a read from
        # being a way to ask the server to hold a whole Task in memory, and a
        # second path that forgot it would be the way around the first.
        bounded = min(limit, MAX_TIMELINE_LIMIT)

        if isinstance(log, IsolatingEventLog) and run_id is None:
            # The isolating read has no narrowed form, and giving it one would
            # mean deciding what `skipped_sequences` means for a page that was
            # filtered: a row this process cannot decode has no readable
            # `run_id`, so it belongs to neither the narrowed page nor the rest.
            # A narrowed read is a navigation aid rather than the Task's
            # history, so it takes the strict path and stops at such a row --
            # the unnarrowed timeline beside it is what stays readable.
            page = await log.read_isolating(
                stream_id, after_sequence=after_sequence, limit=bounded
            )
            return TaskTimeline(
                task_id=task.task_id,
                events=page.events,
                cursor=_resume_cursor(stream_id, page.resume_after, after),
                skipped_sequences=tuple(record.sequence for record in page.quarantined),
            )

        recorded = await log.read(
            stream_id,
            after_sequence=after_sequence,
            limit=bounded,
            run_id=run_id,
        )
        return TaskTimeline(
            task_id=task.task_id,
            events=recorded,
            cursor=_cursor_after(stream_id, recorded, after),
        )

    async def run_tree(
        self,
        principal: PrincipalContext,
        task_id: Identifier,
    ) -> TaskRunTree:
        """Which runs this Task holds, and which of them started which.

        The navigation half of ADR-083: this answers "what is in here", and
        ``timeline(..., run_id=...)`` answers "show me that one". Split that way
        because they have very different costs -- the tree walks the stream once
        and is small, the narrowed read is an index lookup and can be asked for
        repeatedly as somebody clicks around.

        Paged internally rather than read in one statement, because the page
        size is the store's protection and this method has no business
        suspending it. What it does instead is stop, and say that it stopped.
        """

        log = self.events
        if log is None:
            raise TimelineUnavailableError(
                "this task service was assembled without an event log"
            )

        task = await self.get(principal, task_id)
        stream_id = task_stream_id(task)

        collected: list[EventEnvelope] = []
        after_sequence: int | None = None
        complete = True
        while True:
            page = await log.read(
                stream_id,
                after_sequence=after_sequence,
                limit=MAX_TIMELINE_LIMIT,
            )
            if not page:
                break
            collected.extend(page)
            last = page[-1].sequence
            if last is None:  # pragma: no cover - durable rows always carry one
                break
            after_sequence = last
            if len(collected) >= MAX_TREE_EVENTS:
                complete = False
                break

        tree = build_run_tree(stream_id, collected)
        return TaskRunTree(
            task_id=task.task_id,
            stream_id=stream_id,
            roots=tree.roots,
            complete=complete,
        )


#: How many events one tree read may examine before it stops looking.
#:
#: The tree needs the whole stream, because the delegation that names a child
#: can sit anywhere in it -- there is no page whose absence is safe. Reading
#: without a bound would make one request able to pull an arbitrarily long Task
#: into memory, which is what every other read here is capped to prevent.
#:
#: A Task that reaches this is one whose timeline the UI is already paging, and
#: the answer says so (``complete=False``) rather than presenting a partial tree
#: as a whole one -- the same rule ``skipped_sequences`` follows one method up.
MAX_TREE_EVENTS: Final[int] = 10_000


class TaskRunTree(DomainModel):
    """The runs one Task's stream holds, and whether that is all of them."""

    task_id: Identifier
    stream_id: Identifier
    roots: tuple[RunNode, ...] = ()
    #: ``False`` when the read stopped at :data:`MAX_TREE_EVENTS`. A tree built
    #: from part of a stream can be missing whole branches, so the flag is not
    #: decoration: without it a truncated answer is indistinguishable from a
    #: Task that simply delegated less.
    complete: bool = True


class TimelineUnavailableError(RuntimeError):
    """The service can open and read Tasks, but was given no event log."""


def _resume_cursor(
    stream_id: Identifier,
    resume_after: int | None,
    previous: EventCursor | None,
) -> EventCursor | None:
    """Where an isolating slice leaves the caller.

    ``resume_after`` is the highest position the page *examined*, quarantined
    rows included. Taking the last delivered event instead would hand back a
    cursor sitting in front of an undecodable row whenever that row is last on
    the page, and the next request would read it again -- the client would poll
    forever and never move.

    ``None`` means the page examined nothing, and then the caller's own cursor
    is still the truth. Passing it through as the new cursor is the trap: a
    ``None`` written into the cursor is indistinguishable from "start at the
    beginning", so an empty page would send the client back to the head of the
    stream and replay the entire Task on every poll.
    """

    if resume_after is None:
        return previous
    return EventCursor(stream_id=stream_id, sequence=resume_after)


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


def _page_cursor(tasks: tuple[TaskRun, ...], limit: int) -> ListCursor | None:
    """Where to continue, or nothing when the page did not fill.

    A short page cannot have more behind it, so a cursor there would send every
    client on a guaranteed-empty extra request -- and on an empty page it would
    make "you have no Tasks" indistinguishable from "keep going".
    """

    if not tasks or len(tasks) < limit:
        return None
    last = tasks[-1]
    return ListCursor(created_at=last.created_at, last_id=last.task_id)


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
    "DEFAULT_PAGE_LIMIT",
    "DEFAULT_TIMELINE_LIMIT",
    "GRAPH_FOR_CHOICE",
    "MAX_PAGE_LIMIT",
    "MAX_TIMELINE_LIMIT",
    "IsolatingEventLog",
    "SubmittedSemantics",
    "TaskGraphChoice",
    "TaskPage",
    "TaskService",
    "TaskTimeline",
    "TimelineUnavailableError",
    "task_stream_id",
]
