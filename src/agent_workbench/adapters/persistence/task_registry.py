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

Ordering is oldest-queued-first and nothing more. Priority, ``SKIP LOCKED``
claiming, leases and epochs belong to multiple Workers (WP08); a single Worker
needs none of them, and adding them here would be coordination nobody has
tested under contention yet.
"""

from __future__ import annotations

from typing import Final

from pydantic import TypeAdapter
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.models import task_runs
from agent_workbench.domain.identifiers import Identifier, new_id
from agent_workbench.domain.task_registry import TaskStatus, sources_for
from agent_workbench.ports.task_registry import (
    TaskRun,
    TaskStatusDetail,
    TaskSubmission,
    TaskSubmissionConflictError,
    TaskTransitionRejectedError,
)

# One validator, reused: building a TypeAdapter per call would rebuild the same
# schema on every transition.
_DETAIL: Final[TypeAdapter[str]] = TypeAdapter(TaskStatusDetail)

# The submission fields that identify what was asked for. A repeated key whose
# submission differs in any of them is a different request wearing the same
# key, and answering it with the first Task would be answering a question
# nobody asked.
_SUBMISSION_IDENTITY = (
    "tenant_id",
    "owner_id",
    "thread_id",
    "graph_version",
    "input_ref",
)


class PostgresTaskRegistry:
    """``TaskRegistry`` over the ``task_runs`` table."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def submit(self, submission: TaskSubmission) -> TaskRun:
        values = {
            "task_id": new_id("task"),
            "status": "queued",
            "status_detail": None,
            **submission.model_dump(),
        }
        async with self._engine.begin() as connection:
            # Insert-or-nothing, then read. The alternative -- read, then
            # insert if absent -- loses the race between two submissions of one
            # key with a duplicate-key error rather than idempotency.
            await connection.execute(
                pg_insert(task_runs)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=["owner_id", "submission_dedup_key"]
                )
            )
            row = await self._by_submission_key(connection, submission)

        if row is None:  # pragma: no cover - inserted above, same transaction
            raise RuntimeError("the submitted task vanished inside its transaction")
        stored = _to_run(row)
        for field in _SUBMISSION_IDENTITY:
            if getattr(stored, field) != getattr(submission, field):
                raise TaskSubmissionConflictError(
                    owner_id=submission.owner_id,
                    submission_dedup_key=submission.submission_dedup_key,
                )
        return stored

    async def get(self, task_id: Identifier) -> TaskRun | None:
        async with self._engine.connect() as connection:
            row = await self._by_id(connection, task_id)
        return None if row is None else _to_run(row)

    async def start_next(self) -> TaskRun | None:
        picked = (
            select(task_runs.c.task_id)
            .where(task_runs.c.status == "queued")
            .order_by(task_runs.c.created_at, task_runs.c.task_id)
            .limit(1)
            .scalar_subquery()
        )
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        update(task_runs)
                        .where(
                            task_runs.c.task_id == picked,
                            # Not redundant with the sub-select, and measurably
                            # so. When another transaction has already claimed
                            # this row, PostgreSQL re-checks the qualification
                            # against the updated row but does not re-run the
                            # sub-select, so `task_id = picked` still matches
                            # and the row is handed out a second time. This
                            # clause is what turns that into zero rows.
                            task_runs.c.status == "queued",
                        )
                        .values(status="running", updated_at=func.now())
                        .returning(task_runs)
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _to_run(row)

    async def mark_succeeded(self, task_id: Identifier) -> TaskRun:
        return await self._move(task_id, to="succeeded", detail=None)

    async def mark_failed(self, task_id: Identifier, *, reason: str) -> TaskRun:
        return await self._move(task_id, to="failed", detail=reason)

    async def park_for_migration(self, task_id: Identifier, *, reason: str) -> TaskRun:
        return await self._move(task_id, to="waiting_migration", detail=reason)

    async def await_approval(self, task_id: Identifier) -> TaskRun:
        return await self._move(task_id, to="waiting_approval", detail=None)

    async def cancel(self, task_id: Identifier, *, reason: str) -> TaskRun:
        return await self._move(task_id, to="cancelled", detail=reason)

    async def _move(
        self, task_id: Identifier, *, to: TaskStatus, detail: str | None
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
                            task_runs.c.task_id == task_id,
                            task_runs.c.status.in_(sorted(sources)),
                        )
                        .values(status=to, status_detail=detail, updated_at=func.now())
                        .returning(task_runs)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                # Read inside the same transaction: the status reported is the
                # one that refused the move, not one a later writer produced.
                current = await self._by_id(connection, task_id)
                raise TaskTransitionRejectedError(
                    task_id=task_id,
                    found_status=None if current is None else current["status"],
                    attempted=to,
                )
        return _to_run(row)

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
                        task_runs.c.owner_id == submission.owner_id,
                        task_runs.c.submission_dedup_key
                        == submission.submission_dedup_key,
                    )
                )
            )
            .mappings()
            .first()
        )


def _to_run(row: RowMapping) -> TaskRun:
    # Validated back through the model that describes the row, so a value this
    # process cannot interpret fails at the boundary rather than halfway
    # through a Worker.
    return TaskRun.model_validate(dict(row))


__all__ = ["PostgresTaskRegistry"]
