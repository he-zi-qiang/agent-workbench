"""The durable event log, in PostgreSQL.

Sequences are assigned under the stream row's lock, not by an identity column.
The difference is gaps: an identity value consumed by a transaction that rolls
back is never written, so the stream would be unique but full of holes -- and a
subscriber resuming from a cursor cannot tell a hole from an event it has not
received yet. Holding the row makes appends to one stream serialise, which is
what lets ``(stream_id, sequence)`` mean "everything up to here".

The stream row is created on first append rather than by a separate call. A log
whose producer had to remember to declare a stream first would have a failure
mode that only appears under a race, and the fix would be this INSERT anyway.

Transient events are returned and not stored. They carry no sequence, so
storing them would either give them a position nothing can replay from or leave
a row a cursor skips over -- both are ways of making the cursor mean less than
it says.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.models import event_streams, events
from agent_workbench.domain.events import (
    EVENT_DURABILITY,
    EventEnvelope,
    EventPayload,
)
from agent_workbench.domain.identifiers import new_event_id
from agent_workbench.ports.event_log import EventScope

MAX_READ_LIMIT = 1000


class PostgresEventLog:
    """Append-only events with per-stream, gap-free ordering."""

    __slots__ = ("_clock", "_engine")

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        # Injected for the same reason the in-memory log injects one: a test
        # that races the wall clock is a test that fails on a slow machine.
        self._clock = clock

    async def append(
        self,
        scope: EventScope,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
    ) -> EventEnvelope:
        event_id = new_event_id()
        durability = EVENT_DURABILITY[payload.kind]

        if durability == "transient":
            # Never stored, and never given a position. A transient event that
            # occupied a sequence would make a cursor skip; one that was stored
            # without a sequence could not be replayed in order.
            return EventEnvelope(
                event_id=event_id,
                stream_id=scope.stream_id,
                run_id=scope.run_id,
                event_type=payload.kind,
                durability=durability,
                timestamp=self._clock(),
                payload=payload,
                task_id=scope.task_id,
                graph_node_id=scope.graph_node_id,
                parent_event_id=parent_event_id,
            )

        async with self._engine.begin() as connection:
            sequence = await self._next_sequence(connection, scope)
            envelope = EventEnvelope(
                event_id=event_id,
                stream_id=scope.stream_id,
                run_id=scope.run_id,
                event_type=payload.kind,
                durability=durability,
                timestamp=self._clock(),
                payload=payload,
                sequence=sequence,
                task_id=scope.task_id,
                graph_node_id=scope.graph_node_id,
                parent_event_id=parent_event_id,
            )
            await connection.execute(
                insert(events).values(
                    event_id=envelope.event_id,
                    stream_id=envelope.stream_id,
                    run_id=envelope.run_id,
                    sequence=sequence,
                    event_type=envelope.event_type,
                    payload=payload.model_dump(mode="json"),
                    task_id=envelope.task_id,
                    graph_node_id=envelope.graph_node_id,
                    parent_event_id=envelope.parent_event_id,
                )
            )
        return envelope

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
    ) -> tuple[EventEnvelope, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        # Bounded here as well as by the caller: a replay is a client-supplied
        # request, and one that asked for everything would be a way to make the
        # server hold an entire stream in memory on demand.
        capped = min(limit, MAX_READ_LIMIT)

        query = (
            select(
                events.c.event_id,
                events.c.stream_id,
                events.c.run_id,
                events.c.sequence,
                events.c.event_type,
                events.c.payload,
                events.c.task_id,
                events.c.graph_node_id,
                events.c.parent_event_id,
                events.c.recorded_at,
            )
            .where(events.c.stream_id == stream_id)
            .order_by(events.c.sequence)
            .limit(capped)
        )
        if after_sequence is not None:
            query = query.where(events.c.sequence > after_sequence)

        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).all()

        # Validated back through the same model that wrote it, so a row from a
        # contract this process does not know fails closed at the boundary
        # rather than arriving half-understood in somebody's replay.
        return tuple(
            EventEnvelope.model_validate(
                {
                    "event_id": row.event_id,
                    "stream_id": row.stream_id,
                    "run_id": row.run_id,
                    "event_type": row.event_type,
                    "durability": "durable",
                    "payload": row.payload,
                    "sequence": row.sequence,
                    "task_id": row.task_id,
                    "graph_node_id": row.graph_node_id,
                    "parent_event_id": row.parent_event_id,
                    "timestamp": row.recorded_at,
                }
            )
            for row in rows
        )

    async def _next_sequence(
        self, connection: AsyncConnection, scope: EventScope
    ) -> int:
        """Take the stream's next position, holding its row while doing so.

        The stream is created if absent. Two appends racing to create the same
        one both insert conditionally, then both lock whatever ended up there
        -- the same shape the document store uses, and for the same reason: a
        plain insert loses that race with a duplicate-key error.
        """

        await connection.execute(
            pg_insert(event_streams)
            .values(stream_id=scope.stream_id, last_sequence=0)
            .on_conflict_do_nothing(index_elements=["stream_id"])
        )
        row = (
            await connection.execute(
                select(event_streams.c.last_sequence)
                .where(event_streams.c.stream_id == scope.stream_id)
                .with_for_update()
            )
        ).first()
        if row is None:  # pragma: no cover - inserted above, inside this txn
            raise RuntimeError(f"event stream {scope.stream_id} vanished mid-append")
        sequence = cast(int, row.last_sequence) + 1
        await connection.execute(
            update(event_streams)
            .where(event_streams.c.stream_id == scope.stream_id)
            .values(last_sequence=sequence)
        )
        return sequence


__all__ = ["MAX_READ_LIMIT", "PostgresEventLog"]
