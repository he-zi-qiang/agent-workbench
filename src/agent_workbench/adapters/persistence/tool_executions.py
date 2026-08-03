"""The external side-effect ledger, in PostgreSQL.

Every write here takes the fixed cross-table lock order: ``task_runs`` first,
then ``tool_executions``. The Task row is locked rather than merely read,
because what is being checked against it is the *live* lease -- and a reclaim
committing between the check and the insert would leave an intent recorded under
a claim somebody else now holds.

The fence is the Task's own lease, not a copy of it. ``lease_epoch`` advances
on every claim, so requiring the row to be ``running`` at exactly the attempting
epoch, with an unexpired lease, is the same predicate the Registry uses for its
own lifecycle writes. A Worker whose lease expired cannot record an intent, and
-- more importantly -- cannot report a result for an operation the Worker that
replaced it is now responsible for.

One condition in that fence is unfalsifiable, and a sabotage round established
it rather than assuming it: ``status = 'running'`` cannot fail on its own. The
``task_runs_lease_lifecycle`` CHECK gives every non-running row a NULL
``lease_until``, and ``NULL > now()`` is not true, so the expiry condition
already excludes every row the status condition would. It stays as the direct
statement of intent, and becomes load-bearing the day that constraint is
relaxed. The expiry condition itself is *not* redundant, and has its own test:
a lease can lapse while the row is still ``running`` at the same epoch, and an
effect dispatched in that window is one the next Worker dispatches again.

The same fence is what makes an *old* ``intended`` row readable as the state it
is. A row recorded under an epoch that is no longer the live one belongs to a
Worker that cannot come back to settle it -- the ledger refuses its writes -- so
the attempt that claims the operation next abandons it to
``needs_reconciliation`` rather than inheriting permission to dispatch from it.
That is the one transition this ledger performs on its own, and it is the
conservative one: it can only ever refuse an effect.

Two of the guards below are stricter than any single test can show, and this
says which:

* re-reading the stored row after the insert-or-nothing is not defensive. It is
  how a *loser* of a race learns what the winner recorded, which is the same
  path a legitimate retry takes; there is no separate "already exists" branch to
  get wrong.
* the settlement update matches on ``status = 'intended'`` as well as on the
  epoch. The epoch alone would be enough while one Worker holds the Task, since
  nothing else can write. It stays for the case the epoch cannot cover: the same
  Worker reporting twice, which is a bug in a caller rather than a race, and
  which would otherwise overwrite a recorded outcome with a later guess.
"""

from __future__ import annotations

from typing import Final

from pydantic import TypeAdapter
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.models import task_runs, tool_executions
from agent_workbench.domain.identifiers import Identifier, new_id
from agent_workbench.domain.schema import ShortText
from agent_workbench.ports.tool_executions import (
    OperationKey,
    ToolExecutionIntent,
    ToolExecutionNotWritableError,
    ToolExecutionRecord,
    ToolOperationConflictError,
)

#: One validator, reused. ``outcome_detail`` reaches an operator's screen and a
#: row, so it is bounded through the same type the record declares rather than
#: by whatever the caller happened to pass.
_DETAIL: Final[TypeAdapter[str]] = TypeAdapter(ShortText)


class PostgresToolExecutionLedger:
    """``ToolExecutionLedger`` over the ``tool_executions`` table."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record_intent(self, intent: ToolExecutionIntent) -> ToolExecutionRecord:
        async with self._engine.begin() as connection:
            await self._require_live_lease(
                connection,
                task_id=intent.task_id,
                operation_key=intent.operation_key,
                lease_epoch=intent.lease_epoch,
            )
            # Insert-or-nothing, then read. The alternative -- read, then insert
            # if absent -- loses the race between two attempts on one key with a
            # duplicate-key error rather than with the winner's row.
            await connection.execute(
                pg_insert(tool_executions)
                .values(
                    execution_id=new_id("texec"),
                    task_id=intent.task_id,
                    operation_key=intent.operation_key,
                    tool_name=intent.tool_name,
                    canonical_request_hash=intent.canonical_request_hash,
                    status="intended",
                    lease_epoch=intent.lease_epoch,
                    agent_run_id=intent.agent_run_id,
                    tool_call_id=intent.tool_call_id,
                    policy_identity=intent.policy_identity,
                )
                .on_conflict_do_nothing(index_elements=["task_id", "operation_key"])
            )
            row = await self._by_key(
                connection, task_id=intent.task_id, operation_key=intent.operation_key
            )
            if row is None:  # pragma: no cover - inserted above, same txn
                raise RuntimeError(
                    "the recorded intent vanished inside its transaction"
                )
            if row["status"] == "intended" and int(row["lease_epoch"]) < (
                intent.lease_epoch
            ):
                row = await self._abandon(connection, row=row, intent=intent)
            # Raised after the transaction commits, so a conflicting request
            # cannot roll back the abandonment above: whether the arguments
            # match is a fact about *this* attempt, and the dead attempt's
            # outcome is unknown either way.
            conflicted = row["canonical_request_hash"] != intent.canonical_request_hash
        if conflicted:
            # One key, two requests. Returning the stored row here would be
            # telling the caller that an effect it never asked for has already
            # been performed.
            raise ToolOperationConflictError(
                task_id=intent.task_id,
                operation_key=intent.operation_key,
                recorded_hash=str(row["canonical_request_hash"]),
                attempted_hash=intent.canonical_request_hash,
            )
        return _to_record(row)

    async def _abandon(
        self,
        connection: AsyncConnection,
        *,
        row: RowMapping,
        intent: ToolExecutionIntent,
    ) -> RowMapping:
        """Hand a dead attempt's open intent to a human, not to the next Worker.

        An ``intended`` row recorded under an epoch that is no longer the live
        one is the unknown window with nobody left inside it: the Worker that
        wrote it either never dispatched, or dispatched and died before it could
        say so, and no later reader can tell those apart. Returning it as it
        stands would make :attr:`ToolExecutionRecord.may_dispatch` true for the
        Worker that claimed the Task next, which is how an export that already
        landed gets exported a second time and called a retry.

        So the takeover settles it as ``needs_reconciliation`` instead. That
        costs an operation nobody may repeat automatically, which is the price
        of the alternative being an effect nobody can withdraw.
        """

        stale_epoch = int(row["lease_epoch"])
        detail = _DETAIL.validate_python(
            f"lease epoch {stale_epoch} recorded this intent and never reported "
            f"it; epoch {intent.lease_epoch} claimed the task and did not repeat "
            "the effect"
        )
        abandoned = (
            (
                await connection.execute(
                    update(tool_executions)
                    .where(
                        tool_executions.c.task_id == intent.task_id,
                        tool_executions.c.operation_key == intent.operation_key,
                        tool_executions.c.status == "intended",
                        tool_executions.c.lease_epoch == stale_epoch,
                    )
                    .values(
                        status="needs_reconciliation",
                        outcome_detail=detail,
                        settled_at=func.now(),
                    )
                    .returning(tool_executions)
                )
            )
            .mappings()
            .first()
        )
        if abandoned is None:  # pragma: no cover - read under the task row lock
            raise RuntimeError("the abandoned intent vanished inside its transaction")
        return abandoned

    async def record_result(
        self,
        *,
        task_id: Identifier,
        operation_key: OperationKey,
        lease_epoch: int,
        succeeded: bool,
        detail: str | None = None,
    ) -> ToolExecutionRecord:
        return await self._settle(
            task_id=task_id,
            operation_key=operation_key,
            lease_epoch=lease_epoch,
            status="succeeded" if succeeded else "failed",
            detail=detail,
        )

    async def mark_for_reconciliation(
        self,
        *,
        task_id: Identifier,
        operation_key: OperationKey,
        lease_epoch: int,
        detail: str,
    ) -> ToolExecutionRecord:
        return await self._settle(
            task_id=task_id,
            operation_key=operation_key,
            lease_epoch=lease_epoch,
            status="needs_reconciliation",
            detail=detail,
        )

    async def get(
        self, *, task_id: Identifier, operation_key: OperationKey
    ) -> ToolExecutionRecord | None:
        async with self._engine.connect() as connection:
            row = await self._by_key(
                connection, task_id=task_id, operation_key=operation_key
            )
        return None if row is None else _to_record(row)

    async def _settle(
        self,
        *,
        task_id: Identifier,
        operation_key: OperationKey,
        lease_epoch: int,
        status: str,
        detail: str | None,
    ) -> ToolExecutionRecord:
        bounded = None if detail is None else _DETAIL.validate_python(detail)
        async with self._engine.begin() as connection:
            await self._require_live_lease(
                connection,
                task_id=task_id,
                operation_key=operation_key,
                lease_epoch=lease_epoch,
            )
            row = (
                (
                    await connection.execute(
                        update(tool_executions)
                        .where(
                            tool_executions.c.task_id == task_id,
                            tool_executions.c.operation_key == operation_key,
                            # Only the attempt that recorded the intent may
                            # report its outcome, and only once.
                            tool_executions.c.lease_epoch == lease_epoch,
                            tool_executions.c.status == "intended",
                        )
                        .values(
                            status=status,
                            outcome_detail=bounded,
                            settled_at=func.now(),
                        )
                        .returning(tool_executions)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                current = await self._by_key(
                    connection, task_id=task_id, operation_key=operation_key
                )
                raise ToolExecutionNotWritableError(
                    operation_key=operation_key,
                    found_status=None if current is None else str(current["status"]),
                    found_lease_epoch=(
                        None if current is None else int(current["lease_epoch"])
                    ),
                    attempted_lease_epoch=lease_epoch,
                )
        return _to_record(row)

    async def _require_live_lease(
        self,
        connection: AsyncConnection,
        *,
        task_id: Identifier,
        operation_key: OperationKey,
        lease_epoch: int,
    ) -> None:
        """Refuse any write that is not the Task's current claim.

        Locked, not merely read: a reclaim committing between this check and the
        write below would leave the row attributed to a claim somebody else now
        holds -- and for an intent, that is a Worker about to dispatch an effect
        the Task no longer authorizes it to.
        """

        task = (
            (
                await connection.execute(
                    select(task_runs.c.status, task_runs.c.lease_epoch)
                    .where(
                        task_runs.c.task_id == task_id,
                        task_runs.c.status == "running",
                        task_runs.c.lease_epoch == lease_epoch,
                        task_runs.c.lease_until > func.now(),
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if task is not None:
            return
        found = (
            (
                await connection.execute(
                    select(task_runs.c.status, task_runs.c.lease_epoch).where(
                        task_runs.c.task_id == task_id
                    )
                )
            )
            .mappings()
            .first()
        )
        raise ToolExecutionNotWritableError(
            operation_key=operation_key,
            found_status=None if found is None else str(found["status"]),
            found_lease_epoch=None if found is None else int(found["lease_epoch"]),
            attempted_lease_epoch=lease_epoch,
        )

    async def _by_key(
        self,
        connection: AsyncConnection,
        *,
        task_id: Identifier,
        operation_key: OperationKey,
    ) -> RowMapping | None:
        return (
            (
                await connection.execute(
                    select(tool_executions).where(
                        tool_executions.c.task_id == task_id,
                        tool_executions.c.operation_key == operation_key,
                    )
                )
            )
            .mappings()
            .first()
        )


def _to_record(row: RowMapping) -> ToolExecutionRecord:
    return ToolExecutionRecord.model_validate(dict(row))


__all__ = ["PostgresToolExecutionLedger"]
