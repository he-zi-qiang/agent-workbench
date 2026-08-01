"""The approvals ledger, in PostgreSQL.

The decision is one transaction with a fixed lock order: ``task_runs`` first,
then ``approvals``, then the event stream -- the order the architecture baseline
fixes for every cross-table write, so an approval and a cancellation racing each
other queue up rather than deadlock.

Which of them wins is decided by the Task row, not by arrival time. Cancelling
takes ``waiting_approval`` to a terminal status; deciding requires
``waiting_approval``. Whichever commits first leaves the other matching zero
rows, and zero rows is refused rather than ignored.

Three of the guards below are defence in depth rather than load-bearing, and a
sabotage round established which:

* the ``waiting_approval`` requirement is checked twice -- once explicitly and
  again in the requeue's ``WHERE`` -- so removing either alone changes nothing.
  Removing *both* does fail, which is the property that matters.
* locking the Task row does not change any outcome. Without it the requeue still
  re-evaluates its condition against the row a cancellation just wrote and
  matches nothing. The lock is here for the fixed cross-table order, so an
  approval and a cancellation queue instead of deadlocking -- which no assertion
  about results can observe.
* the version fence on the approval update repeats the comparison made a few
  lines above. It is reachable only by two decisions running concurrently, which
  would need a seam in this method to stage deterministically; it is kept rather
  than tested, and said so here.
"""

from __future__ import annotations

from sqlalchemy import func, literal, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence.event_log import PostgresEventLog
from agent_workbench.adapters.persistence.models import approvals, task_runs
from agent_workbench.adapters.persistence.notifications import notify_task_ready
from agent_workbench.domain.events import TaskApprovalDecided, TaskApprovalRequested
from agent_workbench.domain.identifiers import Identifier, new_id
from agent_workbench.domain.pagination import ListCursor
from agent_workbench.domain.task_registry import ApprovalDecision
from agent_workbench.ports.approvals import (
    ApprovalNotDecidableError,
    ApprovalRecord,
    ApprovalStatus,
    ApprovalTaskNotFoundError,
)
from agent_workbench.ports.event_log import EventScope


class PostgresApprovalStore:
    """``ApprovalStore`` over the ``approvals`` table."""

    __slots__ = ("_engine", "_events")

    def __init__(
        self, engine: AsyncEngine, *, events: PostgresEventLog | None = None
    ) -> None:
        self._engine = engine
        self._events = events or PostgresEventLog(engine)

    async def request(
        self,
        *,
        task_id: Identifier,
        graph_node_operation_id: Identifier,
        tenant_id: Identifier,
        owner_id: Identifier,
    ) -> ApprovalRecord:
        async with self._engine.begin() as connection:
            # task_runs first, as everywhere else: read rather than locked,
            # because nothing here writes it. What it supplies is the stream the
            # event belongs on -- a Task's timeline is its thread, and that is
            # not something to reconstruct from an id shape.
            task = (
                (
                    await connection.execute(
                        select(task_runs.c.thread_id).where(
                            task_runs.c.task_id == task_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if task is None:
                raise ApprovalTaskNotFoundError(task_id)
            # Insert-or-nothing, then read: a node re-entered after a crash asks
            # the same question rather than opening a second approval, and the
            # loser of a race gets the winner's row instead of a duplicate key.
            await connection.execute(
                pg_insert(approvals)
                .values(
                    approval_id=new_id("approval"),
                    task_id=task_id,
                    graph_node_operation_id=graph_node_operation_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    status="pending",
                    decision_version=0,
                )
                .on_conflict_do_nothing(
                    index_elements=["task_id", "graph_node_operation_id"]
                )
            )
            row = (
                (
                    await connection.execute(
                        select(approvals).where(
                            approvals.c.task_id == task_id,
                            approvals.c.graph_node_operation_id
                            == graph_node_operation_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:  # pragma: no cover - inserted above, same txn
                raise RuntimeError(
                    "the requested approval vanished inside its transaction"
                )
            # In the same transaction, and keyed by the approval: the row and
            # the event that makes it findable commit together, and a re-entered
            # node leaves one of each. Without this the only way for a client to
            # reach an approval would be to guess its id -- the ledger is not
            # readable by task, and the interrupt lives in the checkpoint.
            await self._events.append_durable_in_transaction(
                connection,
                EventScope(
                    stream_id=str(task["thread_id"]),
                    run_id=task_id,
                    task_id=task_id,
                ),
                TaskApprovalRequested(
                    task_id=task_id,
                    approval_id=str(row["approval_id"]),
                    graph_node_operation_id=graph_node_operation_id,
                ),
                event_key=f"approval_requested:{row['approval_id']}",
            )
        return _to_record(row)

    async def get(self, approval_id: Identifier) -> ApprovalRecord | None:
        async with self._engine.connect() as connection:
            row = await self._by_id(connection, approval_id)
        return None if row is None else _to_record(row)

    async def list_for_owner(
        self,
        *,
        tenant_id: Identifier,
        owner_id: Identifier,
        statuses: tuple[ApprovalStatus, ...] = (),
        limit: int,
        after: ListCursor | None = None,
    ) -> tuple[ApprovalRecord, ...]:
        """Newest first, keyset-paged, narrowed in the query itself."""

        if limit < 1:
            raise ValueError("limit must be positive")
        query = (
            select(approvals)
            .where(
                # Both, in the query. Narrowing after the fact would make every
                # later caller of this method a place it can be left out.
                approvals.c.tenant_id == tenant_id,
                approvals.c.owner_id == owner_id,
            )
            .order_by(approvals.c.created_at.desc(), approvals.c.approval_id.desc())
            .limit(limit)
        )
        if statuses:
            query = query.where(approvals.c.status.in_(statuses))
        if after is not None:
            # Row-value comparison, so the tie-break is part of the same
            # predicate rather than a second condition that can disagree with
            # the ORDER BY it is meant to match.
            query = query.where(
                tuple_(approvals.c.created_at, approvals.c.approval_id)
                < tuple_(literal(after.created_at), literal(after.last_id))
            )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).mappings().all()
        return tuple(_to_record(row) for row in rows)

    async def decide(
        self,
        approval_id: Identifier,
        *,
        decision: ApprovalDecision,
        decision_version: int,
        decided_by: Identifier,
    ) -> ApprovalRecord:
        if decision_version < 1:
            raise ValueError("decision_version must be positive")

        async with self._engine.begin() as connection:
            existing = await self._by_id(connection, approval_id)
            if existing is None:
                raise ApprovalNotDecidableError(
                    approval_id=approval_id, task_status=None, approval_status=None
                )
            if existing["decision_version"] >= decision_version:
                if existing["status"] == "pending":
                    # Version 0 is pending; nothing at or below it is a decision.
                    raise ApprovalNotDecidableError(
                        approval_id=approval_id,
                        task_status=None,
                        approval_status="pending",
                    )
                # The same decision again. One row, one requeue: returning the
                # stored record is what makes a retried request harmless.
                return _to_record(existing)

            # task_runs first, and locked: this is the fixed cross-table order,
            # so an approval and a cancellation racing each other serialise
            # instead of deadlocking.
            task = (
                (
                    await connection.execute(
                        select(task_runs)
                        .where(task_runs.c.task_id == existing["task_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if task is None or task["status"] != "waiting_approval":
                # A cancellation committed while somebody was deciding. The
                # terminal state is the fact; this decision is not.
                raise ApprovalNotDecidableError(
                    approval_id=approval_id,
                    task_status=None if task is None else str(task["status"]),
                    approval_status=str(existing["status"]),
                )

            decided = (
                (
                    await connection.execute(
                        update(approvals)
                        .where(
                            approvals.c.approval_id == approval_id,
                            # Fenced on the version it read, so two decisions
                            # arriving together cannot both apply.
                            approvals.c.decision_version < decision_version,
                        )
                        .values(
                            status=decision,
                            decision_version=decision_version,
                            decided_by=decided_by,
                            decided_at=func.now(),
                        )
                        .returning(approvals)
                    )
                )
                .mappings()
                .first()
            )
            if decided is None:  # pragma: no cover - fenced above in this txn
                raise ApprovalNotDecidableError(
                    approval_id=approval_id,
                    task_status=str(task["status"]),
                    approval_status=str(existing["status"]),
                )

            requeued = (
                await connection.execute(
                    update(task_runs)
                    .where(
                        task_runs.c.task_id == existing["task_id"],
                        task_runs.c.status == "waiting_approval",
                    )
                    .values(
                        status="queued",
                        resume_kind="approval",
                        resume_approval_id=approval_id,
                        available_at=func.now(),
                        updated_at=func.now(),
                    )
                    .returning(task_runs.c.task_id)
                )
            ).first()
            if requeued is None:  # pragma: no cover - locked and checked above
                raise ApprovalNotDecidableError(
                    approval_id=approval_id,
                    task_status=str(task["status"]),
                    approval_status=str(existing["status"]),
                )

            await self._events.append_durable_in_transaction(
                connection,
                EventScope(
                    stream_id=str(task["thread_id"]),
                    run_id=str(task["task_id"]),
                    task_id=str(task["task_id"]),
                ),
                TaskApprovalDecided(
                    task_id=str(task["task_id"]),
                    approval_id=approval_id,
                    decision=decision,
                    decision_version=decision_version,
                ),
                event_key=f"approval_decided:{approval_id}:{decision_version}",
            )
            # The fourth thing in the same transaction, and the only one that is
            # not a fact: a wake-up for the Task this decision just requeued. A
            # refused decision rolls back and sends nothing, so no Worker is ever
            # sent to look at a Task a human did not release.
            await notify_task_ready(connection, task_id=str(task["task_id"]))
        return _to_record(decided)

    async def _by_id(self, connection: object, approval_id: str) -> RowMapping | None:
        return (
            (
                await connection.execute(  # type: ignore[attr-defined]
                    select(approvals).where(approvals.c.approval_id == approval_id)
                )
            )
            .mappings()
            .first()
        )


def _to_record(row: RowMapping) -> ApprovalRecord:
    return ApprovalRecord.model_validate(dict(row))


__all__ = ["PostgresApprovalStore"]
