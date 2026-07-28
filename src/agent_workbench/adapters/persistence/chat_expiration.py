"""Atomic expiry of PostgreSQL-backed Chat executions.

The Turn ledger and event log are two views of one terminal fact.  This
coordinator owns the transaction that moves an expired fixed execution lease
to ``failed`` and appends ``ChatTurnExpired``.  ``SKIP LOCKED`` lets multiple
reapers share the scan without waiting for a live request or one another.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.event_log import PostgresEventLog
from agent_workbench.adapters.persistence.models import chat_turns
from agent_workbench.domain.events import ChatTurnExpired
from agent_workbench.domain.runs import stale_execution_outcome
from agent_workbench.ports.conversation_store import (
    StoredChatTurn,
    chat_turn_terminal_event_key,
)
from agent_workbench.ports.event_log import EventScope

logger = logging.getLogger(__name__)
_OrderKey = tuple[datetime, str]


class _CandidateExpirationError(RuntimeError):
    """One selected Turn failed before its atomic expiry transaction committed."""

    def __init__(self, turn_id: str, order_key: _OrderKey) -> None:
        self.turn_id = turn_id
        self.order_key = order_key
        super().__init__(f"failed to expire chat turn {turn_id}")


class PostgresChatExpirationCoordinator:
    """Commit each expired Turn and terminal event as one isolated unit."""

    __slots__ = ("_engine", "_events", "_resume_after")

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._events = PostgresEventLog(engine)
        self._resume_after: _OrderKey | None = None

    async def expire_due(
        self,
        *,
        limit: int,
    ) -> tuple[StoredChatTurn, ...]:
        """Attempt one stable, non-blocking and poison-isolated batch."""

        if limit < 1:
            raise ValueError("chat expiration limit must be positive")

        attempted: set[str] = set()
        expired: list[StoredChatTurn] = []
        wrapped = False
        while len(attempted) < limit:
            try:
                candidate = await self._expire_next(
                    excluding=attempted,
                    after=self._resume_after,
                )
            except _CandidateExpirationError as exc:
                attempted.add(exc.turn_id)
                self._resume_after = exc.order_key
                logger.exception(
                    "isolated failed Chat expiration candidate %s",
                    exc.turn_id,
                )
                continue
            if candidate is None:
                if self._resume_after is not None and not wrapped:
                    # Continue the stable scan from the beginning once. The
                    # per-call exclusion set prevents a poison candidate from
                    # being retried in the same batch, while the persisted
                    # cursor prevents limit=1 from starving later Turns.
                    self._resume_after = None
                    wrapped = True
                    continue
                break
            terminal, order_key = candidate
            attempted.add(terminal.turn_id)
            self._resume_after = order_key
            expired.append(terminal)
        return tuple(expired)

    async def _expire_next(
        self,
        *,
        excluding: set[str],
        after: _OrderKey | None,
    ) -> tuple[StoredChatTurn, _OrderKey] | None:
        """Commit the oldest available candidate, or isolate its failure."""

        async with self._engine.begin() as connection:
            query = (
                select(chat_turns)
                .where(chat_turns.c.status == "running")
                .where(chat_turns.c.lease_until <= func.statement_timestamp())
                .order_by(
                    chat_turns.c.lease_until,
                    chat_turns.c.turn_id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if excluding:
                query = query.where(
                    chat_turns.c.turn_id.not_in(tuple(sorted(excluding)))
                )
            if after is not None:
                lease_until, turn_id = after
                query = query.where(
                    or_(
                        chat_turns.c.lease_until > lease_until,
                        and_(
                            chat_turns.c.lease_until == lease_until,
                            chat_turns.c.turn_id > turn_id,
                        ),
                    )
                )
            row = (await connection.execute(query)).mappings().first()
            if row is None:
                return None

            turn_id = cast(str, row["turn_id"])
            order_key = (cast(datetime, row["lease_until"]), turn_id)
            try:
                running = _turn_from_row(row)
                failure = stale_execution_outcome(running.run_id)
                updated_row = (
                    (
                        await connection.execute(
                            update(chat_turns)
                            .where(chat_turns.c.turn_id == running.turn_id)
                            .where(chat_turns.c.status == "running")
                            .values(
                                status="failed",
                                lease_until=None,
                                failure_outcome=failure.model_dump(mode="json"),
                                updated_at=func.now(),
                            )
                            .returning(chat_turns)
                        )
                    )
                    .mappings()
                    .one()
                )
                terminal = _turn_from_row(updated_row)
                await self._after_update(connection, terminal)

                await self._events.append_durable_in_transaction(
                    connection,
                    EventScope(
                        stream_id=terminal.session_id,
                        run_id=terminal.run_id,
                    ),
                    ChatTurnExpired(turn_id=terminal.turn_id),
                    event_key=chat_turn_terminal_event_key(terminal.turn_id),
                )
                await self._after_event(connection, terminal)
                return terminal, order_key
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _CandidateExpirationError(turn_id, order_key) from exc

    async def _after_update(
        self,
        connection: AsyncConnection,
        turn: StoredChatTurn,
    ) -> None:
        """Fault-injection seam after the Turn update, before its event."""

        del connection, turn

    async def _after_event(
        self,
        connection: AsyncConnection,
        turn: StoredChatTurn,
    ) -> None:
        """Fault-injection seam after the event, before transaction commit."""

        del connection, turn


def _turn_from_row(row: RowMapping) -> StoredChatTurn:
    """Strictly reconstruct the versioned aggregate from one SQL row."""

    return StoredChatTurn.model_validate(
        {
            "turn_id": row["turn_id"],
            "session_id": row["session_id"],
            "idempotency_key": row["idempotency_key"],
            "request_hash": row["request_hash"],
            "run_id": row["run_id"],
            "status": row["status"],
            "lease_until": row["lease_until"],
            "user_message_id": row["user_message_id"],
            "assistant_message_id": row["assistant_message_id"],
            "result": row["result"],
            "failure_outcome": row["failure_outcome"],
        }
    )


__all__ = ["PostgresChatExpirationCoordinator"]
