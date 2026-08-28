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

Two things can make a stored row unreadable to the process replaying it: it was
written by an older envelope contract, or it is damaged. They get different
answers. An older row is a translation problem, and
:class:`EventUpcasterRegistry` is where the translation lives. A damaged one is
not translatable at all, and the only choices are to stop the replay or to skip
the row -- :meth:`PostgresEventLog.read_isolating` skips it *and says so*, which
is the difference between a partial replay and a partial replay nobody noticed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.models import event_streams, events
from agent_workbench.domain.events import (
    EVENT_DURABILITY,
    EventEnvelope,
    EventPayload,
)
from agent_workbench.domain.identifiers import new_event_id
from agent_workbench.domain.schema import DOMAIN_SCHEMA_VERSION
from agent_workbench.ports.event_log import (
    EventKey,
    EventKeyConflictError,
    EventScope,
    validate_event_key,
)

logger = logging.getLogger(__name__)

MAX_READ_LIMIT = 1000

# One stored envelope as the plain mapping a row decodes into, before
# `EventEnvelope` has seen it. Deliberately not an `EventEnvelope`: that model
# refuses every version but the current one, so an old envelope cannot be
# represented as one -- which is the entire reason an upcaster exists.
StoredEnvelope = Mapping[str, object]
EventUpcaster = Callable[[StoredEnvelope], StoredEnvelope]

# A quarantine record is returned to a caller and written to a log line, so its
# reason is bounded like every other string that crosses this boundary. One
# corrupt row must not be able to write an unbounded log line.
QUARANTINE_REASON_MAX_LENGTH = 512


class EventUpcasterRegistry:
    """How an envelope written by an older contract reaches the current one.

    Keyed by ``(event_type, from_version)``, and each entry raises an envelope
    exactly one version. Replay applies them as a chain until the envelope
    reaches ``DOMAIN_SCHEMA_VERSION``. Entries that jumped straight to the
    current version would all have to be rewritten on every schema release,
    and the release that missed one would hand validation an envelope claiming
    a version whose shape it does not have.

    The bump belongs to the chain rather than to the registered function. An
    upcaster responsible for its own ``schema_version`` could forget it, and
    the loop would then apply the same step forever.
    """

    __slots__ = ("_steps",)

    def __init__(self) -> None:
        self._steps: dict[tuple[str, int], EventUpcaster] = {}

    def register(
        self,
        event_type: str,
        from_version: int,
        upcaster: EventUpcaster,
    ) -> None:
        """Record how to raise ``event_type`` from ``from_version`` by one.

        A duplicate is refused rather than replaced: two upcasters for one
        step is a merge accident, and keeping whichever was imported last
        would let import order decide a migration.
        """

        # Zero is admissible. `DOMAIN_SCHEMA_VERSION` is 1, so zero is the only
        # version below the current one, and it reads correctly -- a row from
        # before envelopes carried a version. Refusing it would leave this
        # mechanism with nothing that could legally be registered, and a
        # migration mechanism first exercised during a migration is a mechanism
        # nobody has run.
        if not 0 <= from_version < DOMAIN_SCHEMA_VERSION:
            raise ValueError(
                "an upcaster starts below the current domain schema version "
                f"{DOMAIN_SCHEMA_VERSION}, received {from_version}"
            )
        step = (event_type, from_version)
        if step in self._steps:
            raise ValueError(
                f"an upcaster for {event_type} v{from_version} is already registered"
            )
        self._steps[step] = upcaster

    def raise_to_current(
        self,
        stored: StoredEnvelope,
        *,
        from_version: int,
    ) -> StoredEnvelope:
        """Apply the chain as far as it reaches.

        A missing step returns what has been reached so far instead of
        raising. The caller validates the result either way, so an envelope
        nothing can raise fails exactly as an unknown version failed before
        upcasting existed: one refusal path, not two that have to agree.
        """

        working = stored
        version = from_version
        while version < DOMAIN_SCHEMA_VERSION:
            # Re-read each round, because renaming an event type is one of the
            # migrations this mechanism exists for: the next step is looked up
            # under the name the previous step produced.
            event_type = working.get("event_type")
            if not isinstance(event_type, str):
                # An upcaster dropped the discriminator. Envelope validation
                # names that better than a KeyError from inside this loop.
                return working
            upcaster = self._steps.get((event_type, version))
            if upcaster is None:
                return working
            version += 1
            raised = dict(upcaster(working))
            raised["schema_version"] = version
            working = raised
        return working


# Empty, and correct that way. `DOMAIN_SCHEMA_VERSION` is still 1, so no row in
# any database was written by an older envelope contract, and an entry here
# would describe a migration that never happened. Real upcasters arrive here in
# the change that raises the version. Tests that exercise the mechanism build
# their own registry instead of registering into this one -- a fabricated
# historical step here would be applied to production rows.
DEFAULT_EVENT_UPCASTERS = EventUpcasterRegistry()


@dataclass(frozen=True, slots=True)
class QuarantinedEvent:
    """One stored row a replay could not turn into an envelope.

    Enough to find the row again by hand: which stream, which position, which
    id. The reason names the fields that failed and never their values -- the
    domain models hide rejected input from their errors precisely because
    payloads carry document text and model output.
    """

    stream_id: str
    sequence: int
    event_id: str
    event_type: str
    schema_version: int
    reason: str


@dataclass(frozen=True, slots=True)
class ReplayPage:
    """What one isolating replay delivered, and what it refused to deliver.

    ``events`` alone cannot carry a skip: a replay that dropped a row is a
    shorter tuple, and a shorter tuple is also what the end of a stream looks
    like. The skipped rows therefore travel beside the delivered ones, so a
    caller that ignores them is ignoring something it was handed rather than
    something it was never told.

    ``resume_after`` is the highest sequence this page *examined*, quarantined
    rows included -- not the position of the last delivered event. The two
    differ exactly when the last row of a page is quarantined, which is the
    case that matters: this repository's replay loops advance their cursor to
    the last event they received, so a cursor taken from ``events`` would begin
    the next page before the poison row and read it again, forever.

    Skipping does not put a hole in the log. The per-stream sequence stays
    gap-free -- nothing is deleted and no position is reused -- but a
    subscriber that resumes from ``resume_after`` has moved past a row it never
    received. That is a real gap in what one *subscriber* saw, and
    ``quarantined`` is where it is stated out loud instead of being left for
    someone to infer from a count that looks plausible.
    """

    events: tuple[EventEnvelope, ...]
    quarantined: tuple[QuarantinedEvent, ...]
    resume_after: int | None

    @property
    def skipped(self) -> int:
        """How many stored rows this page could not deliver."""

        return len(self.quarantined)


class PostgresEventLog:
    """Append-only events with per-stream, gap-free ordering."""

    __slots__ = ("_clock", "_engine", "_upcasters")

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        upcasters: EventUpcasterRegistry | None = None,
    ) -> None:
        self._engine = engine
        # Injected for the same reason the in-memory log injects one: a test
        # that races the wall clock is a test that fails on a slow machine.
        self._clock = clock
        # Defaults to the process-wide registry so that every reader of a
        # stream agrees on what an old row means. A caller supplies its own
        # only to exercise the mechanism against versions this contract has
        # never had; production rows must not meet an invented step.
        self._upcasters = (
            upcasters if upcasters is not None else DEFAULT_EVENT_UPCASTERS
        )

    async def append(
        self,
        scope: EventScope,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        durability = EVENT_DURABILITY[payload.kind]
        event_key = validate_event_key(event_key)

        if durability == "transient":
            if event_key is not None:
                raise ValueError("transient events cannot carry an event_key")
            # Never stored, and never given a position. A transient event that
            # occupied a sequence would make a cursor skip; one that was stored
            # without a sequence could not be replayed in order.
            return EventEnvelope(
                event_id=new_event_id(),
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
            return await self.append_durable_in_transaction(
                connection,
                scope,
                payload,
                parent_event_id=parent_event_id,
                event_key=event_key,
            )

    async def append_durable_in_transaction(
        self,
        connection: AsyncConnection,
        scope: EventScope,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
        event_key: EventKey | None = None,
        first_write_wins: bool = False,
    ) -> EventEnvelope:
        """Append a durable event using the caller's open transaction.

        This is intentionally a PostgreSQL-adapter capability rather than part
        of ``EventLogPort``. Most producers should use :meth:`append`; the
        release coordinator needs the narrower form so the authorization
        fence, answer event and conversation transition either all commit or
        all roll back.

        ``first_write_wins`` changes what a repeated key means. The default
        treats it as a claim -- the event under this key *is* this event --
        and refuses a repeat that differs, which is the right contract when
        the payload is derived from stored facts and cannot legitimately
        vary. A caller whose payload carries fields only the first attempt
        knew -- the Task submission's ``intent`` block, whose words a retried
        triage would not reproduce (ADR-036) -- passes ``True`` instead: an
        existing event is returned untouched without comparing payloads, so
        the first submission's words stay the record and a retry cannot be
        refused for holding different ones.
        """

        durability = EVENT_DURABILITY[payload.kind]
        if durability != "durable":
            raise ValueError("an in-transaction event append must be durable")
        event_key = validate_event_key(event_key)
        serialized_payload = payload.model_dump(mode="json")

        last_sequence = await self._lock_stream(connection, scope)
        if event_key is not None:
            existing = (
                (
                    await connection.execute(
                        select(events).where(
                            events.c.stream_id == scope.stream_id,
                            events.c.event_key == event_key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if not first_write_wins:
                    _require_same_event(
                        existing,
                        scope=scope,
                        payload=serialized_payload,
                        parent_event_id=parent_event_id,
                    )
                # Decoded through the same path a replay uses. A keyed append
                # can land on a row an older contract wrote, and one that
                # decoded differently here than in `read` would return an
                # envelope no replay of that stream will ever produce.
                return self._decode(existing)

        sequence = last_sequence + 1
        envelope = EventEnvelope(
            event_id=new_event_id(),
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
            update(event_streams)
            .where(event_streams.c.stream_id == scope.stream_id)
            .values(last_sequence=sequence)
        )
        await connection.execute(
            insert(events).values(
                event_id=envelope.event_id,
                stream_id=envelope.stream_id,
                run_id=envelope.run_id,
                sequence=sequence,
                schema_version=envelope.schema_version,
                event_type=envelope.event_type,
                payload=serialized_payload,
                recorded_at=envelope.timestamp,
                task_id=envelope.task_id,
                graph_node_id=envelope.graph_node_id,
                parent_event_id=envelope.parent_event_id,
                event_key=event_key,
            )
        )
        return envelope

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
        run_id: str | None = None,
    ) -> tuple[EventEnvelope, ...]:
        rows = await self._replay_rows(
            stream_id,
            after_sequence=after_sequence,
            limit=limit,
            run_id=run_id,
        )
        # Validated back through the same model that wrote it, so a row from a
        # contract this process does not know fails closed at the boundary
        # rather than arriving half-understood in somebody's replay. A row an
        # upcaster can raise is not such a row: it is understood, one version
        # late. Everything else still stops the replay here, and a caller that
        # would rather continue past it says so by calling `read_isolating`.
        return tuple(self._decode(row) for row in rows)

    async def read_isolating(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
    ) -> ReplayPage:
        """Replay a page, setting aside the rows this process cannot decode.

        The explicit alternative to :meth:`read`, which stops at the first such
        row and takes the rest of the stream with it -- one unreadable event
        from an abandoned experiment is otherwise enough to make a whole Task
        timeline unreachable. Isolation is not deletion: the row stays where it
        is, it is named in the returned page and logged with its stream and
        position, so the skip is something an operator can find and a caller
        can count.

        Kept off ``EventLogPort`` for the reason
        :meth:`append_durable_in_transaction` is: it is a capability of a store
        that can see raw rows, and a port method returning a page of
        "everything except what broke" would make degraded replay the default
        for callers who never chose it.

        An upcaster that raises is deliberately *not* isolated. A failing
        upcaster is a defect in the migration and it fails for every row of its
        type, so quarantining those would turn one broken migration into a
        silent, stream-wide loss.
        """

        rows = await self._replay_rows(
            stream_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        delivered: list[EventEnvelope] = []
        quarantined: list[QuarantinedEvent] = []
        resume_after: int | None = None
        for row in rows:
            # Advanced before the attempt rather than after a success. A cursor
            # that only ever reached the last *delivered* event would re-read a
            # trailing poison row on every page and never get past it.
            resume_after = int(row["sequence"])
            try:
                delivered.append(self._decode(row))
            except ValidationError as exc:
                record = _quarantine_record(row, exc)
                quarantined.append(record)
                # Logged as well as returned. The caller learns the count; the
                # operator who has to repair or delete the row needs to know
                # which row, and a return value nobody printed is not evidence.
                logger.error(
                    "isolated undecodable event %s in stream %s at sequence %d: %s",
                    record.event_id,
                    record.stream_id,
                    record.sequence,
                    record.reason,
                )
        return ReplayPage(
            events=tuple(delivered),
            quarantined=tuple(quarantined),
            resume_after=resume_after,
        )

    async def _replay_rows(
        self,
        stream_id: str,
        *,
        after_sequence: int | None,
        limit: int,
        run_id: str | None = None,
    ) -> Sequence[RowMapping]:
        if limit < 1:
            raise ValueError("limit must be positive")
        # Bounded here as well as by the caller: a replay is a client-supplied
        # request, and one that asked for everything would be a way to make the
        # server hold an entire stream in memory on demand.
        capped = min(limit, MAX_READ_LIMIT)

        query = (
            select(events)
            .where(events.c.stream_id == stream_id)
            .order_by(events.c.sequence)
            .limit(capped)
        )
        if after_sequence is not None:
            query = query.where(events.c.sequence > after_sequence)
        if run_id is not None:
            # Ordered and cursored by `sequence` either way, which is why
            # `ix_events_stream_run_sequence` carries all three columns in that
            # order: without the sequence on the end the planner has the rows
            # but not the order, and sorts a stream to return twelve events.
            query = query.where(events.c.run_id == run_id)

        async with self._engine.connect() as connection:
            return (await connection.execute(query)).mappings().all()

    def _decode(self, row: RowMapping) -> EventEnvelope:
        """Turn one stored row into an envelope, raising an old one first."""

        stored = _stored_envelope(row)
        version = int(row["schema_version"])
        # Only upwards. A row from a *newer* contract cannot be understood by
        # guessing which fields this process would have ignored, so it keeps
        # failing closed the way it always has.
        if version < DOMAIN_SCHEMA_VERSION:
            stored = self._upcasters.raise_to_current(stored, from_version=version)
        return EventEnvelope.model_validate(stored)

    async def _lock_stream(self, connection: AsyncConnection, scope: EventScope) -> int:
        """Return the current position while holding the stream row lock.

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
        return cast(int, row.last_sequence)


def _require_same_event(
    existing: RowMapping,
    *,
    scope: EventScope,
    payload: dict[str, object],
    parent_event_id: str | None,
) -> None:
    if (
        existing["stream_id"] != scope.stream_id
        or existing["run_id"] != scope.run_id
        or existing["task_id"] != scope.task_id
        or existing["graph_node_id"] != scope.graph_node_id
        or existing["payload"] != payload
        or existing["parent_event_id"] != parent_event_id
    ):
        raise EventKeyConflictError(
            "event_key already identifies a different durable event"
        )


def _stored_envelope(row: RowMapping) -> dict[str, object]:
    """The row as an envelope-shaped mapping, not yet validated.

    Separate from validation because an upcaster runs between the two: it has
    to see the envelope the old contract wrote, which is by definition not
    something ``EventEnvelope`` would accept.
    """

    return {
        "event_id": row["event_id"],
        "stream_id": row["stream_id"],
        "run_id": row["run_id"],
        "schema_version": row["schema_version"],
        "event_type": row["event_type"],
        "durability": "durable",
        "payload": row["payload"],
        "sequence": row["sequence"],
        "task_id": row["task_id"],
        "graph_node_id": row["graph_node_id"],
        "parent_event_id": row["parent_event_id"],
        "timestamp": row["recorded_at"],
    }


def _quarantine_record(row: RowMapping, error: ValidationError) -> QuarantinedEvent:
    """Describe why one row was set aside, without quoting the row."""

    # `ValidationError.errors()` is flattened to one line on purpose: this
    # string is a log line and a field of a returned record, and pydantic's
    # own rendering is multi-line with a documentation URL per error. The
    # location and the message are what identify the defect; the input is
    # already withheld by the domain models' `hide_input_in_errors`.
    reason = "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        for item in error.errors()
    )
    if len(reason) > QUARANTINE_REASON_MAX_LENGTH:
        reason = reason[: QUARANTINE_REASON_MAX_LENGTH - 1] + "…"
    return QuarantinedEvent(
        stream_id=str(row["stream_id"]),
        sequence=int(row["sequence"]),
        event_id=str(row["event_id"]),
        # Straight from the column rather than from the payload: a row whose
        # payload is unreadable still has to be findable, and the column is
        # what an operator will search on.
        event_type=str(row["event_type"]),
        schema_version=int(row["schema_version"]),
        reason=reason,
    )


__all__ = [
    "DEFAULT_EVENT_UPCASTERS",
    "MAX_READ_LIMIT",
    "QUARANTINE_REASON_MAX_LENGTH",
    "EventUpcaster",
    "EventUpcasterRegistry",
    "PostgresEventLog",
    "QuarantinedEvent",
    "ReplayPage",
    "StoredEnvelope",
]
