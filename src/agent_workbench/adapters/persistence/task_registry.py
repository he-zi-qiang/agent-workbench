"""The Task Registry, in PostgreSQL.

Every transition is one conditional UPDATE whose ``WHERE`` names both the task
and the statuses it may legally move from, and those statuses are read out of
the domain's transition table rather than written again here. A rule restated
in SQL is a second copy of that rule, and the copy is the one that keeps
running after somebody edits the first.

Matching no rows is the signal, not an anomaly. A Task that was cancelled while
a Worker was running it, or settled by something else, produces exactly that --
so a zero-row update reads the row back and raises with the status it actually
found, instead of returning quietly and letting the Worker believe it settled
something.

Claims are short ``FOR UPDATE SKIP LOCKED`` transactions.  A claim grants a
time-bounded, monotonically fenced epoch; every execution-side lifecycle write
must match that epoch, owner, and unexpired lease.  E1 intentionally fences
only this Registry row.  Fencing the LangGraph checkpointer is E2 work.
"""

from __future__ import annotations

from typing import Final, NoReturn, cast

from pydantic import TypeAdapter
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from agent_workbench.adapters.persistence.event_log import PostgresEventLog
from agent_workbench.adapters.persistence.models import task_runs
from agent_workbench.domain.events import (
    EventPayload,
    TaskAwaitingApproval,
    TaskCancelled,
    TaskClaimed,
    TaskDeadLettered,
    TaskFailed,
    TaskParkedForMigration,
    TaskRetryScheduled,
    TaskSubmitted,
    TaskSucceeded,
)
from agent_workbench.domain.identifiers import Identifier, new_id
from agent_workbench.domain.task_registry import TaskStatus, sources_for
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.task_registry import (
    ExecutionLease,
    StaleExecutionError,
    TaskClaim,
    TaskRun,
    TaskStatusDetail,
    TaskSubmission,
    TaskSubmissionConflictError,
    TaskTransitionRejectedError,
)

# One validator, reused: building a TypeAdapter per call would rebuild the same
# schema on every transition.
_DETAIL: Final[TypeAdapter[str]] = TypeAdapter(TaskStatusDetail)

# The request fields that identify what was asked for. A repeated key whose
# request differs in either is a different request wearing the same key, and
# answering it with the first Task would be answering a question nobody asked.
#
# Do *not* include thread_id or the submitted semantics/policy fields here.
# Those are server decisions made while serving a submission. A retry can
# legitimately reach a restarted deployment or a fresh TaskService instance,
# which will mint those fields again; idempotency must return the original row
# rather than reject that ordinary retry for disagreeing with its new values.
_SUBMISSION_IDENTITY = (
    "graph_version",
    "input_fingerprint",
)


class PostgresTaskRegistry:
    """``TaskRegistry`` over the ``task_runs`` table."""

    __slots__ = ("_engine", "_events")

    def __init__(
        self, engine: AsyncEngine, *, events: PostgresEventLog | None = None
    ) -> None:
        self._engine = engine
        # Defaulted rather than optional. A Task that exists without the event
        # saying why is the failure this whole transaction exists to prevent,
        # so there is no way to construct a registry that skips it -- only a
        # way to hand it a different log.
        self._events = events or PostgresEventLog(engine)

    async def submit(self, submission: TaskSubmission) -> TaskRun:
        values = {
            "task_id": new_id("task"),
            "status": "queued",
            "status_detail": None,
            **submission.model_dump(mode="json"),
        }
        async with self._engine.begin() as connection:
            # Insert-or-nothing, then read. The alternative -- read, then
            # insert if absent -- loses the race between two submissions of one
            # key with a duplicate-key error rather than idempotency.
            await connection.execute(
                pg_insert(task_runs)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        "tenant_id",
                        "owner_id",
                        "submission_dedup_key",
                    ]
                )
            )
            row = await self._by_submission_key(connection, submission)
            if row is not None:
                # Before the event, not after. A key reused for a different
                # request is a submission conflict; letting it reach the log
                # would report it as an event-key conflict instead -- an error
                # about this project's own bookkeeping, raised at a caller who
                # made an ordinary mistake.
                stored = _to_run(row)
                _require_same_submission(stored, submission)
                # Same transaction, and idempotent by key: a Task and the event
                # that opened it commit or roll back together, and a repeated
                # submission that returns the first Task does not append a
                # second event describing it.
                await self._events.append_durable_in_transaction(
                    connection,
                    EventScope(
                        # The first submission chose the Task's thread. A
                        # retried request has a freshly minted candidate, but
                        # its event must remain on the stored Task's stream.
                        stream_id=stored.thread_id,
                        # A Task *is* the run at this level -- `task_runs` is
                        # the table of them. No agent run exists yet, and
                        # inventing an id for one would be a trace to nothing.
                        run_id=row["task_id"],
                        task_id=row["task_id"],
                    ),
                    TaskSubmitted(
                        graph_version=stored.graph_version,
                        input_ref=stored.input_ref,
                    ),
                    event_key=_submission_event_key(row["task_id"]),
                )

        if row is None:  # pragma: no cover - inserted above, same transaction
            raise RuntimeError("the submitted task vanished inside its transaction")
        return _to_run(row)

    async def get(self, task_id: Identifier) -> TaskRun | None:
        async with self._engine.connect() as connection:
            row = await self._by_id(connection, task_id)
        return None if row is None else _to_run(row)

    async def claim_next(
        self, worker_id: Identifier, *, lease_seconds: int
    ) -> TaskClaim | None:
        """Claim one eligible Task without holding a transaction for its graph."""
        # Two conditions here are defence in depth, established by sabotage:
        # the update's `status`/`available_at` repeat what the locking select
        # already guaranteed, since `FOR UPDATE` means the row cannot change
        # between the two statements. And `skip_locked` is a liveness property
        # rather than a correctness one -- without it a second claimer blocks
        # behind the first instead of moving on, which no assertion about
        # outcomes can see. Both stay; neither is load-bearing today.
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        async with self._engine.begin() as connection:
            task_id = (
                (
                    await connection.execute(
                        select(task_runs.c.task_id)
                        .where(
                            task_runs.c.status == "queued",
                            task_runs.c.available_at <= func.now(),
                        )
                        .order_by(
                            task_runs.c.available_at,
                            task_runs.c.created_at,
                            task_runs.c.task_id,
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .first()
            )
            if task_id is None:
                return None
            row = (
                (
                    await connection.execute(
                        update(task_runs)
                        .where(
                            task_runs.c.task_id == task_id,
                            task_runs.c.status == "queued",
                            task_runs.c.available_at <= func.now(),
                        )
                        .values(
                            status="running",
                            lease_owner=worker_id,
                            lease_epoch=task_runs.c.lease_epoch + 1,
                            lease_until=func.now() + _seconds_interval(lease_seconds),
                            heartbeat_at=func.now(),
                            attempt_count=task_runs.c.attempt_count + 1,
                            updated_at=func.now(),
                        )
                        .returning(task_runs)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:  # pragma: no cover - row remains locked until update
                return None
            task = _to_run(row)
            await self._append_lifecycle_event(
                connection,
                task,
                TaskClaimed(
                    task_id=task.task_id,
                    epoch=task.lease_epoch,
                    attempt=task.attempt_count,
                ),
            )
            return TaskClaim(
                task=task,
                lease=ExecutionLease(
                    task_id=task.task_id,
                    worker_id=worker_id,
                    epoch=task.lease_epoch,
                ),
            )

    async def heartbeat(self, lease: ExecutionLease, *, lease_seconds: int) -> TaskRun:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        update(task_runs)
                        .where(*_live_lease_conditions(lease))
                        .values(
                            lease_until=func.now() + _seconds_interval(lease_seconds),
                            heartbeat_at=func.now(),
                            updated_at=func.now(),
                        )
                        .returning(task_runs)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                await self._raise_stale(connection, lease, attempted="heartbeat")
        return _to_run(row)

    async def reclaim_expired(
        self,
        *,
        limit: int,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> tuple[TaskRun, ...]:
        if limit < 1 or max_attempts < 1 or retry_base_seconds < 1:
            raise ValueError("reclaim limits and retry_base_seconds must be positive")
        if retry_max_seconds < retry_base_seconds:
            raise ValueError("retry_max_seconds must be >= retry_base_seconds")
        # Four of the conditions below are deliberate defence in depth rather
        # than load-bearing checks, and a sabotage round established which:
        #
        # * `status = 'running'` (both here and in the update) cannot fail. The
        #   lease-lifecycle constraint gives a non-running row a NULL
        #   `lease_until`, and `NULL < now()` is not true, so the expiry
        #   condition already excludes every non-running row. It stays as the
        #   direct statement of intent, and becomes load-bearing the day that
        #   constraint is relaxed.
        # * the update's `lease_epoch` and `lease_until` conditions cannot fail
        #   either: the select above took `FOR UPDATE`, so the row cannot change
        #   between the two statements. They stay because the day this select
        #   stops locking -- or the update moves to its own transaction -- they
        #   are the only thing standing between a sweep and a lease that was
        #   renewed underneath it.
        #
        # Removing the expiry condition from *both* statements does fail a test,
        # which is the property that actually matters: a live lease is left
        # alone.
        recovered: list[TaskRun] = []
        async with self._engine.begin() as connection:
            expired = (
                (
                    await connection.execute(
                        select(task_runs)
                        .where(
                            task_runs.c.status == "running",
                            task_runs.c.lease_until < func.now(),
                        )
                        .order_by(task_runs.c.lease_until, task_runs.c.task_id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .mappings()
                .all()
            )
            for current in expired:
                attempts = int(current["attempt_count"])
                exhausted = attempts >= max_attempts
                values: dict[str, object] = {
                    "status": "dead_letter" if exhausted else "queued",
                    "status_detail": (
                        f"lease expired after {attempts} attempts"
                        if exhausted
                        else None
                    ),
                    "lease_owner": None,
                    "lease_until": None,
                    "heartbeat_at": None,
                    "updated_at": func.now(),
                }
                if not exhausted:
                    delay = min(
                        retry_max_seconds,
                        retry_base_seconds * 2 ** max(0, attempts - 1),
                    )
                    values["available_at"] = func.now() + _seconds_interval(delay)
                row = (
                    (
                        await connection.execute(
                            update(task_runs)
                            .where(
                                task_runs.c.task_id == current["task_id"],
                                task_runs.c.status == "running",
                                task_runs.c.lease_epoch == current["lease_epoch"],
                                task_runs.c.lease_until < func.now(),
                            )
                            .values(**values)
                            .returning(task_runs)
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is not None:
                    recovered_task = _to_run(row)
                    payload: EventPayload
                    if exhausted:
                        payload = TaskDeadLettered(
                            task_id=recovered_task.task_id,
                            epoch=recovered_task.lease_epoch,
                            attempt=recovered_task.attempt_count,
                        )
                    else:
                        payload = TaskRetryScheduled(
                            task_id=recovered_task.task_id,
                            epoch=recovered_task.lease_epoch,
                            attempt=recovered_task.attempt_count,
                            reason_code="lease_expired",
                            delay_seconds=retry_base_seconds
                            if attempts < 1
                            else min(
                                retry_max_seconds,
                                retry_base_seconds * 2 ** (attempts - 1),
                            ),
                        )
                    await self._append_lifecycle_event(
                        connection, recovered_task, payload
                    )
                    recovered.append(recovered_task)
        return tuple(recovered)

    async def mark_succeeded(self, lease: ExecutionLease) -> TaskRun:
        return await self._move(lease, to="succeeded", detail=None)

    async def mark_failed(self, lease: ExecutionLease, *, reason: str) -> TaskRun:
        return await self._move(lease, to="failed", detail=reason)

    async def park_for_migration(
        self, lease: ExecutionLease, *, reason: str
    ) -> TaskRun:
        return await self._move(lease, to="waiting_migration", detail=reason)

    async def await_approval(self, lease: ExecutionLease) -> TaskRun:
        return await self._move(lease, to="waiting_approval", detail=None)

    async def release_for_retry(
        self, lease: ExecutionLease, *, delay_seconds: int
    ) -> TaskRun:
        """Return a live claim to the queue without allowing a stale release."""
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        update(task_runs)
                        .where(*_live_lease_conditions(lease))
                        .values(
                            status="queued",
                            status_detail=None,
                            lease_owner=None,
                            lease_until=None,
                            heartbeat_at=None,
                            available_at=func.now() + _seconds_interval(delay_seconds),
                            updated_at=func.now(),
                        )
                        .returning(task_runs)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                await self._raise_stale(connection, lease, attempted="queued")
            task = _to_run(row)
            await self._append_lifecycle_event(
                connection,
                task,
                TaskRetryScheduled(
                    task_id=task.task_id,
                    epoch=task.lease_epoch,
                    attempt=task.attempt_count,
                    reason_code="retry_requested",
                    delay_seconds=delay_seconds,
                ),
            )
        return task

    async def cancel(self, task_id: Identifier, *, reason: str) -> TaskRun:
        detail = _DETAIL.validate_python(reason)
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        update(task_runs)
                        .where(
                            task_runs.c.task_id == task_id,
                            task_runs.c.status.in_(sorted(sources_for("cancelled"))),
                        )
                        .values(
                            status="cancelled",
                            status_detail=detail,
                            lease_owner=None,
                            lease_until=None,
                            heartbeat_at=None,
                            updated_at=func.now(),
                        )
                        .returning(task_runs)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                current = await self._by_id(connection, task_id)
                raise TaskTransitionRejectedError(
                    task_id=task_id,
                    found_status=None if current is None else current["status"],
                    attempted="cancelled",
                )
            task = _to_run(row)
            await self._append_lifecycle_event(
                connection,
                task,
                TaskCancelled(
                    task_id=task.task_id,
                    epoch=task.lease_epoch,
                    attempt=task.attempt_count,
                ),
            )
        return task

    async def _move(
        self, lease: ExecutionLease, *, to: TaskStatus, detail: str | None
    ) -> TaskRun:
        # Whether a status carries a reason is settled by the method
        # signatures and by the database's own constraint; what neither of
        # them catches is a reason that is present but empty. The column
        # accepts it, and `TaskRun` then refuses to read the row back -- a row
        # written successfully and unreadable afterwards. Validated through the
        # same type the model uses, so the two cannot disagree about what
        # counts as a reason.
        if detail is not None:
            detail = _DETAIL.validate_python(detail)

        sources = sources_for(to)
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        update(task_runs)
                        .where(
                            *_live_lease_conditions(lease),
                            task_runs.c.status.in_(sorted(sources)),
                        )
                        .values(
                            status=to,
                            status_detail=detail,
                            lease_owner=None,
                            lease_until=None,
                            heartbeat_at=None,
                            updated_at=func.now(),
                        )
                        .returning(task_runs)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                await self._raise_stale(connection, lease, attempted=to)
            task = _to_run(row)
            await self._append_lifecycle_event(
                connection, task, _transition_event(task, to)
            )
        return task

    async def _raise_stale(
        self, connection: AsyncConnection, lease: ExecutionLease, *, attempted: str
    ) -> NoReturn:
        current = await self._by_id(connection, lease.task_id)
        if current is not None and current["status"] == "running":
            raise StaleExecutionError(lease)
        raise TaskTransitionRejectedError(
            task_id=lease.task_id,
            found_status=None if current is None else current["status"],
            attempted=attempted,  # type: ignore[arg-type]
        )

    async def _by_id(
        self, connection: AsyncConnection, task_id: Identifier
    ) -> RowMapping | None:
        return (
            (
                await connection.execute(
                    select(task_runs).where(task_runs.c.task_id == task_id)
                )
            )
            .mappings()
            .first()
        )

    async def _by_submission_key(
        self, connection: AsyncConnection, submission: TaskSubmission
    ) -> RowMapping | None:
        return (
            (
                await connection.execute(
                    select(task_runs).where(
                        task_runs.c.tenant_id == submission.tenant_id,
                        task_runs.c.owner_id == submission.owner_id,
                        task_runs.c.submission_dedup_key
                        == submission.submission_dedup_key,
                    )
                )
            )
            .mappings()
            .first()
        )

    async def _append_lifecycle_event(
        self,
        connection: AsyncConnection,
        task: TaskRun,
        payload: EventPayload,
    ) -> None:
        """Append a transition to the Task's own stream in this transaction."""

        await self._events.append_durable_in_transaction(
            connection,
            EventScope(
                stream_id=task.thread_id,
                run_id=task.task_id,
                task_id=task.task_id,
            ),
            payload,
            event_key=_lifecycle_event_key(task, payload.kind),
        )


def _require_same_submission(stored: TaskRun, submission: TaskSubmission) -> None:
    for field in _SUBMISSION_IDENTITY:
        if getattr(stored, field) != getattr(submission, field):
            raise TaskSubmissionConflictError(
                owner_id=submission.owner_id,
                submission_dedup_key=submission.submission_dedup_key,
            )


def _transition_event(task: TaskRun, to: TaskStatus) -> EventPayload:
    """Map a Registry lifecycle target to its public, detail-free event."""

    if to == "succeeded":
        return TaskSucceeded(
            task_id=task.task_id,
            epoch=task.lease_epoch,
            attempt=task.attempt_count,
        )
    if to == "failed":
        return TaskFailed(
            task_id=task.task_id,
            epoch=task.lease_epoch,
            attempt=task.attempt_count,
        )
    if to == "waiting_approval":
        return TaskAwaitingApproval(
            task_id=task.task_id,
            epoch=task.lease_epoch,
            attempt=task.attempt_count,
        )
    if to == "waiting_migration":
        return TaskParkedForMigration(
            task_id=task.task_id,
            epoch=task.lease_epoch,
            attempt=task.attempt_count,
        )
    raise AssertionError(f"no lifecycle event for transition to {to}")


def _submission_event_key(task_id: str) -> str:
    """One durable submission event per Task, forever.

    Derived from the Task rather than from the caller's key, because the Task
    is what the event describes and it is already unique.
    """

    return f"task_submitted:{task_id}"


def _lifecycle_event_key(task: TaskRun, kind: str) -> str:
    """A transition's monotonic fence makes its timeline key replay-safe."""

    return f"task_lifecycle:{task.task_id}:{task.lease_epoch}:{kind}"


def _live_lease_conditions(
    lease: ExecutionLease,
) -> tuple[ColumnElement[bool], ...]:
    """The one fenced Registry-write predicate used by heartbeat and settle.

    This is not E2 fencing: it protects only the Task row.  A stale graph may
    still attempt a checkpoint write until the checkpointer gains the same
    epoch predicate in the following work package.
    """

    return (
        task_runs.c.task_id == lease.task_id,
        task_runs.c.status == "running",
        task_runs.c.lease_owner == lease.worker_id,
        task_runs.c.lease_epoch == lease.epoch,
        task_runs.c.lease_until > func.now(),
    )


def _seconds_interval(seconds: int) -> ColumnElement[object]:
    """A bindable PostgreSQL interval; SQLAlchemy has no named make_interval."""

    return cast(
        ColumnElement[object],
        text("(:seconds * interval '1 second')").bindparams(seconds=seconds),
    )


def _to_run(row: RowMapping) -> TaskRun:
    # Validated back through the model that describes the row, so a value this
    # process cannot interpret fails at the boundary rather than halfway
    # through a Worker.
    return TaskRun.model_validate(dict(row))


__all__ = ["PostgresTaskRegistry"]
