"""The event-stream transport, independent of what a stream belongs to.

A subscription is a replay that has not finished yet. The client sends the last
id it saw, the server sends everything after it, and then keeps sending. That
is the same operation twice, which is why there is no separate "catch up" path:
a reconnect is just a subscription starting further along.

The cursor is the SSE event id, so a browser resumes with ``Last-Event-ID``
without the client writing any resumption logic of its own. It only means
anything because sequences are gap-free -- a subscriber cannot tell a hole from
an event that has not arrived, so a log with holes would make "everything after
n" unanswerable.

Only durable events are replayable, and only durable events are sent here. A
transient one has no position, so a client that reconnected would have no way
to ask for it again and no way to know it had missed it.

One stored row this process cannot decode used to end the subscription: the
strict replay raises, and the whole session becomes unreachable behind a single
damaged event. Where the log offers the isolating replay this stream uses it
instead -- and says so, in a frame of its own. A subscription that quietly
handed back an incomplete history would be worse than the block it replaces,
because a block is something somebody notices.

**Why this is not in a route module.** A stream is addressed by ``stream_id``
and nothing else; which resource that id was derived from -- a chat session, a
Task's workflow thread -- is the route's business and never the transport's.
Two routes producing frames from two copies of this code would be two frame
formats that agree until one of them is edited, and a client cannot tell those
apart from a server that changed its mind. The authorization stays with the
routes for the same reason it is theirs alone: only the route knows what the id
in its URL names, and therefore who may read it.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fastapi import Request

from agent_workbench.domain.errors import IncompatibleSchemaError
from agent_workbench.domain.events import EventEnvelope, ModelDelta
from agent_workbench.domain.schema import BOUNDED_TEXT_LIMIT
from agent_workbench.ports.event_log import EventCursor, EventLogPort

LAST_EVENT_ID_HEADER = "last-event-id"

# Sent when nothing has happened, so an idle connection is not mistaken for a
# dead one by a proxy that times out silent sockets.
HEARTBEAT = ": heartbeat\n\n"

# The SSE event name for a position that was examined and not delivered.
# Deliberately not shaped like a domain event type: those are the payload class
# names, ``RunStarted`` and friends, so a dotted lower-case name cannot collide
# with one now or after any event is added. A client dispatching on ``event:``
# therefore cannot mistake this for something a workflow emitted.
QUARANTINE_EVENT = "stream.quarantined"

# The SSE event name for live events this subscriber was too slow to be given.
# Same naming rule, and the same reason: a client that dispatches on ``event:``
# has to be able to tell "you missed some of the live text" from any event a
# run emitted.
DEGRADED_EVENT = "stream.degraded"


class _QuarantinedEvent(Protocol):
    """The part of a quarantine record a subscriber is told about."""

    @property
    def stream_id(self) -> str: ...
    @property
    def sequence(self) -> int: ...
    @property
    def event_id(self) -> str: ...
    @property
    def event_type(self) -> str: ...
    @property
    def schema_version(self) -> int: ...


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

    Structural, and declared here rather than added to ``EventLogPort``,
    because the capability is deliberately not part of the port: a port method
    returning "everything except what broke" would make degraded replay the
    contract for every implementation, including the ones that have no way to
    see a damaged row. Asking for the shape instead of for a concrete class
    also keeps this transport off the PostgreSQL adapter, so the in-memory log
    -- which cannot hold an undecodable row and does not offer this -- still
    serves the stream through the strict path below.
    """

    async def read_isolating(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
    ) -> _ReplayPage: ...


@dataclass(frozen=True, slots=True)
class _StrictPage:
    """A strict ``read`` in the shape the isolating one returns.

    So the loop below has one page shape to reason about instead of two
    branches that have to be kept in step. ``quarantined`` is always empty
    here, and that is the whole difference: the strict read raises on a row it
    cannot decode rather than skipping it, which is the contract every caller
    had before isolation existed.
    """

    events: tuple[EventEnvelope, ...]
    quarantined: tuple[_QuarantinedEvent, ...]
    resume_after: int | None


class TooManyLiveSubscribersError(RuntimeError):
    """One stream already has as many live subscribers as it may have.

    A refusal rather than a queue, and it happens before any streaming response
    exists: the cost this bounds is paid per subscriber for the whole life of
    the connection, so admitting one and starving it would spend the memory
    anyway and hide the reason.
    """


@dataclass(slots=True)
class LiveSubscription:
    """One subscriber's share of the transient events on one stream.

    Bounded, and the bound is the point. Transient events arrive at whatever
    rate a provider streams tokens, and nothing downstream applies back
    pressure -- the producer is a synchronous callback on the emit path, which
    must not wait for a browser. An unbounded buffer therefore turns one slow
    tab into unbounded memory inside the API process.

    Overflow drops the **oldest** pending event and counts it. The newest delta
    is the one that still describes what the run is doing; dropping it instead
    would keep a stale prefix and leave the subscriber further behind on every
    overflow. The count is not swallowed: it becomes a ``stream.degraded``
    frame, because a live view missing some of its text and a live view that is
    complete must not look the same.

    It deliberately holds no event log and no cursor. Transient events never
    reach the database and have no position, so a subscription that could reach
    a log would be a subscription that could turn a token into a query.
    """

    _limit: int
    _release: Callable[[LiveSubscription], None]
    _pending: deque[EventEnvelope]
    _dropped: int = 0
    _arrival: asyncio.Event | None = None

    @classmethod
    def opened(
        cls,
        *,
        buffer_events: int,
        release: Callable[[LiveSubscription], None],
    ) -> LiveSubscription:
        return cls(_limit=buffer_events, _release=release, _pending=deque())

    def offer(self, envelope: EventEnvelope) -> None:
        """Take one live event, evicting the oldest if the buffer is full.

        Synchronous on purpose: it is called from the sink every producer
        writes through, and a producer that awaited a subscriber would let a
        reader's pace decide a run's pace.
        """

        if len(self._pending) >= self._limit:
            self._pending.popleft()
            self._dropped += 1
        self._pending.append(envelope)
        if self._arrival is not None:
            self._arrival.set()

    async def wait(self, timeout: float) -> bool:
        """Whether something arrived within ``timeout`` seconds."""

        if self._pending:
            return True
        if self._arrival is None:
            # Created here rather than in the constructor: an ``asyncio.Event``
            # binds to the loop that first awaits it, and this object is built
            # by a route before the streaming generator runs.
            self._arrival = asyncio.Event()
        self._arrival.clear()
        try:
            await asyncio.wait_for(self._arrival.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def drain(self) -> tuple[tuple[EventEnvelope, ...], int]:
        """Everything buffered, and how many were dropped to make room."""

        taken = tuple(self._pending)
        dropped = self._dropped
        self._pending.clear()
        self._dropped = 0
        if self._arrival is not None:
            self._arrival.clear()
        return taken, dropped

    def close(self) -> None:
        self._release(self)


class LiveEventChannel:
    """Transient events, fanned out to whoever is watching that stream.

    In-process only, and that is a statement about what this can honestly
    offer rather than a stage of construction. A transient event is never
    written anywhere, so the only subscribers it can reach are the ones inside
    the process that produced it. A deployment whose runs happen in a worker
    -- every Task -- gets no live text here, and gets it silently: the durable
    replay beneath is unchanged and complete, so such a stream is not degraded,
    it is simply not live. Saying otherwise would require this channel to
    invent events it never saw.
    """

    def __init__(self, *, buffer_events: int, max_subscribers_per_stream: int) -> None:
        self._buffer_events = buffer_events
        self._max_per_stream = max_subscribers_per_stream
        self._streams: dict[str, list[LiveSubscription]] = {}

    def observe(self, envelope: EventEnvelope) -> None:
        """Offer one emitted event to that stream's live subscribers.

        Durable events are dropped here rather than forwarded, and the reason
        is not deduplication: they are already delivered by the replay below,
        with the position a reconnecting client resumes from. A durable event
        arriving twice by two routes would arrive once with an id and once
        without, and no client can be asked to reconcile that.
        """

        if envelope.durability != "transient":
            return
        for subscriber in self._streams.get(envelope.stream_id, ()):
            subscriber.offer(envelope)

    def subscribe(self, stream_id: str) -> LiveSubscription:
        """Open one subscription, or refuse because this stream is full."""

        subscribers = self._streams.setdefault(stream_id, [])
        if len(subscribers) >= self._max_per_stream:
            raise TooManyLiveSubscribersError(
                f"this stream already has {self._max_per_stream} live subscribers"
            )
        subscription = LiveSubscription.opened(
            buffer_events=self._buffer_events,
            release=lambda closing: self._release(stream_id, closing),
        )
        subscribers.append(subscription)
        return subscription

    def _release(self, stream_id: str, subscription: LiveSubscription) -> None:
        subscribers = self._streams.get(stream_id)
        if subscribers is None:
            return
        if subscription in subscribers:
            subscribers.remove(subscription)
        if not subscribers:
            # Otherwise the map grows by one empty list per session ever
            # subscribed to, for the life of the process.
            del self._streams[stream_id]

    def subscriber_count(self, stream_id: str) -> int:
        """How many live subscribers one stream has. For tests and metrics."""

        return len(self._streams.get(stream_id, ()))


def resume_from(request: Request, stream_id: str) -> int | None:
    """Where this subscriber left off, or ``None`` to start at the beginning.

    A malformed cursor starts from the beginning rather than failing the
    request. It arrives from a browser that may have stored it across a
    deploy, and refusing the connection would leave a client unable to
    reconnect at all -- with no way to discover that clearing it would help.
    """

    raw = request.headers.get(LAST_EVENT_ID_HEADER)
    if not raw:
        return None
    try:
        cursor = EventCursor.decode(raw)
    except IncompatibleSchemaError:
        return None
    if cursor.stream_id != stream_id:
        # A cursor for a different stream says nothing about this one, and
        # honouring its number would silently skip events.
        return None
    return cursor.sequence


async def stream_events(
    events: EventLogPort,
    *,
    stream_id: str,
    after_sequence: int | None,
    poll_seconds: int,
    page_size: int,
    heartbeat_seconds: int,
    disconnected: Callable[[], Awaitable[bool]] | None,
    live: LiveSubscription | None = None,
    coalesce_seconds: float = 0.05,
) -> AsyncIterator[str]:
    """Replay, then keep replaying from wherever the last batch ended.

    Durable latency is bounded by the poll interval. The configuration also
    names a LISTEN/NOTIFY wakeup backend, which nothing consumes yet: until it
    does, this is the honest behaviour rather than a claim about it.

    ``live`` adds transient events to the same connection *without* adding a
    second reason to query the log. They are delivered in the gap this loop
    would otherwise spend asleep, so a burst of a thousand token deltas costs
    the database nothing -- the alternative, waking the loop per delta, would
    have made a fast model a source of load on PostgreSQL.

    With ``live`` absent the bytes are exactly what they were before this
    parameter existed, which is what lets a second subscriber route reuse this
    and produce frames a client cannot distinguish from the first one's.
    """

    cursor = after_sequence
    idle = 0.0
    # Resolved once, before the loop: whether a log can isolate is a property
    # of the object, not of a page, and re-deciding it on every poll would let
    # one subscription serve part of a stream strictly and part of it
    # isolating -- two different meanings for one connection's frames.
    isolating = events if isinstance(events, IsolatingEventLog) else None
    try:
        async for frame in _replay_forever(
            events,
            isolating,
            stream_id=stream_id,
            cursor=cursor,
            idle=idle,
            poll_seconds=poll_seconds,
            page_size=page_size,
            heartbeat_seconds=heartbeat_seconds,
            disconnected=disconnected,
            live=live,
            coalesce_seconds=coalesce_seconds,
        ):
            yield frame
    finally:
        # Every way out lands here -- the client closing the tab, the generator
        # being garbage collected, an exception on the socket. A subscription
        # released anywhere else would leak one buffer per dropped connection,
        # and the per-stream ceiling would then refuse the reconnect.
        if live is not None:
            live.close()


async def _replay_forever(
    events: EventLogPort,
    isolating: IsolatingEventLog | None,
    *,
    stream_id: str,
    cursor: int | None,
    idle: float,
    poll_seconds: int,
    page_size: int,
    heartbeat_seconds: int,
    disconnected: Callable[[], Awaitable[bool]] | None,
    live: LiveSubscription | None,
    coalesce_seconds: float,
) -> AsyncIterator[str]:
    """The loop itself, so its caller can own what happens when it ends."""

    while True:
        if disconnected is not None and await disconnected():
            return

        page = await _read_page(
            events,
            isolating,
            stream_id=stream_id,
            after_sequence=cursor,
            page_size=page_size,
        )
        for frame in _frames(page, stream_id):
            yield frame

        if page.resume_after is not None:
            # ``resume_after``, not "the page delivered something". A page can
            # be entirely quarantined -- ``events`` empty while the log looked
            # at, and named, several rows -- and stopping there would leave the
            # subscription parked in front of a run of unreadable rows it has
            # already skipped past.
            cursor = page.resume_after
            idle = 0.0
            # Straight back around: a full page means there is probably more,
            # and waiting a poll interval to find out would make a burst of
            # events arrive one page per interval.
            continue

        # ``None`` means nothing was examined, and then the caller's own cursor
        # is still the truth. Assigning it anyway would reset the subscription
        # to the head of the stream on the first idle poll and replay the whole
        # session, forever.
        idle += poll_seconds
        if idle >= heartbeat_seconds:
            idle = 0.0
            yield HEARTBEAT
        if live is None:
            await asyncio.sleep(poll_seconds)
            continue
        # The same wait, spent watching the live buffer instead of nothing.
        async for frame in _live_window(live, poll_seconds, coalesce_seconds):
            yield frame


async def _live_window(
    live: LiveSubscription,
    seconds: float,
    coalesce_seconds: float,
) -> AsyncIterator[str]:
    """Transient frames arriving within ``seconds``, then return.

    Each burst is given ``coalesce_seconds`` to finish arriving before it is
    drained. A model streams tokens far faster than a frame per token is worth
    sending, and the window is what turns "one frame per token" into "one frame
    per readable chunk" without holding anything back for longer than that.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        if not await live.wait(remaining):
            return
        await asyncio.sleep(min(coalesce_seconds, max(0.0, deadline - loop.time())))
        envelopes, dropped = live.drain()
        if dropped > 0:
            # Before the events it made room for: the gap is in front of them.
            yield degraded_frame(dropped)
        for frame in live_frames(envelopes):
            yield frame


async def _read_page(
    events: EventLogPort,
    isolating: IsolatingEventLog | None,
    *,
    stream_id: str,
    after_sequence: int | None,
    page_size: int,
) -> _ReplayPage:
    """One page, from whichever replay this log supports."""

    if isolating is not None:
        return await isolating.read_isolating(
            stream_id, after_sequence=after_sequence, limit=page_size
        )
    batch = await events.read(stream_id, after_sequence=after_sequence, limit=page_size)
    return _StrictPage(
        events=batch,
        quarantined=(),
        # The last position this page reached, which for a strict read is the
        # last event it delivered: it raises rather than passing over anything.
        resume_after=next(
            (event.sequence for event in reversed(batch) if event.sequence is not None),
            None,
        ),
    )


def _frames(page: _ReplayPage, stream_id: str) -> Iterator[str]:
    """The page's frames, in stream order.

    Delivered events and quarantine notices are merged by sequence rather than
    sent as two blocks, because a notice's only job is to mark *where* the
    history is missing something. Appended at the end of a page it would say
    "one of the rows you just received is not all of them" and leave the client
    to guess which gap it belonged to -- and on a page boundary it would land
    after events that come later in the stream than the row it describes.

    Both inputs arrive in ascending sequence order, so one pass with a single
    look-ahead is enough.
    """

    notices = iter(page.quarantined)
    pending = next(notices, None)
    for envelope in page.events:
        sequence = envelope.sequence
        if sequence is None:
            # Unreachable: a replay returns durable events, which always carry
            # a position. Kept because the type says it may be None and
            # dropping the frame is the only safe reading if that ever
            # changes; no test covers it, and none can while the contract
            # holds.
            continue  # pragma: no cover
        while pending is not None and pending.sequence < sequence:
            yield _quarantine_frame(pending, stream_id)
            pending = next(notices, None)
        yield frame_for(envelope, stream_id, sequence)
    while pending is not None:
        yield _quarantine_frame(pending, stream_id)
        pending = next(notices, None)


def frame_for(envelope: EventEnvelope, stream_id: str, sequence: int) -> str:
    """One SSE frame, with the cursor as its id."""

    cursor = EventCursor(stream_id=stream_id, sequence=sequence).encode()
    return (
        f"id: {cursor}\n"
        f"event: {envelope.event_type}\n"
        f"data: {envelope.model_dump_json()}\n\n"
    )


def live_frames(envelopes: tuple[EventEnvelope, ...]) -> Iterator[str]:
    """One burst of transient events, as the frames a subscriber should see.

    Adjacent deltas of the same model call are merged, because the unit a
    reader cares about is a chunk of text and the unit a provider emits is
    whichever bytes arrived together. Two rules keep the merge honest:

    * only *adjacent* events merge, and only within one ``model_call_id``, so
      nothing is reordered and a tool round between two calls stays visible;
    * a merge never truncates. ``ModelDelta.text`` is bounded, so the merge
      flushes and starts a new frame rather than trimming -- text is what this
      frame exists to carry, and a silently shortened one would be a lie the
      subscriber has no way to detect.
    """

    pending: list[EventEnvelope] = []
    merged = ""

    def flush() -> Iterator[str]:
        nonlocal pending, merged
        if not pending:
            return
        last = pending[-1]
        # The last envelope's identity, not the first: its timestamp is when
        # the text in this frame stopped arriving, which is what a reader
        # comparing it against the step beside it means by "when".
        yield _transient_frame(
            last.model_copy(
                update={
                    "payload": ModelDelta(
                        model_call_id=_call_of(last),
                        text=merged,
                    )
                }
            )
        )
        pending = []
        merged = ""

    for envelope in envelopes:
        payload = envelope.payload
        if not isinstance(payload, ModelDelta):
            # Anything else transient -- tool progress -- passes through, after
            # whatever text preceded it, so the order a reader sees is the
            # order things happened.
            yield from flush()
            yield _transient_frame(envelope)
            continue
        call = payload.model_call_id
        if pending and _call_of(pending[-1]) != call:
            yield from flush()
        if len(merged) + len(payload.text) > BOUNDED_TEXT_LIMIT:
            yield from flush()
        pending.append(envelope)
        merged += payload.text
    yield from flush()


def _call_of(envelope: EventEnvelope) -> str:
    payload = envelope.payload
    # Only ever called on envelopes this module has already narrowed.
    assert isinstance(payload, ModelDelta)
    return payload.model_call_id


def _transient_frame(envelope: EventEnvelope) -> str:
    """One live event, as a frame that cannot move the client's cursor.

    There is no ``id:`` line and no parameter that could add one, and that is
    the whole design rather than an omission. ``Last-Event-ID`` is what a
    browser sends back to resume, and a transient event has no position to
    resume from; an id here would set the client's cursor to a place the log
    cannot be asked about. Per the SSE specification a frame without ``id``
    leaves the last event id untouched, so durable resumption keeps working
    across any number of these.

    Building it through a function that takes no cursor is what makes that
    structural. A shared builder with an optional id would put the rule in a
    caller's argument list, where the next caller has to remember it.
    """

    return f"event: {envelope.event_type}\ndata: {envelope.model_dump_json()}\n\n"


def degraded_frame(dropped: int) -> str:
    """The frame that says live text was skipped, and how much.

    Deliberately shaped like the quarantine notice and deliberately unlike it
    in the one way that matters: that one names a durable position and carries
    its cursor, this one names no position at all. What was dropped was never
    addressable, so there is nothing for a client to go and fetch -- the only
    honest thing to report is that the live view is no longer complete.

    The durable replay underneath is unaffected: nothing that was dropped here
    was ever going to be replayed, and everything that will be replayed is
    still on its way. A client should say the live text has a gap, not that the
    history does.
    """

    body = json.dumps({"dropped_events": dropped}, sort_keys=True)
    return f"event: {DEGRADED_EVENT}\ndata: {body}\n\n"


def _quarantine_frame(record: _QuarantinedEvent, stream_id: str) -> str:
    """The frame that says one durable position was skipped, and which.

    A frame of its own, rather than a count carried on the next event. An
    envelope is the log's record of one thing that happened; a field added to
    it would make one event's frame assert something about a different
    position, and a client that dispatches or filters by event type -- this
    project's own web reducer does -- would drop the notice while keeping the
    events on either side of the hole. That is the silent partial replay this
    path exists to prevent, arrived at by a different route.

    Not an SSE comment (``: skipped 1``) for the same reason, only worse: every
    conforming parser, including this repository's, discards comment lines
    before a frame is assembled. The skip would be in the bytes and invisible
    to the program reading them.

    It carries an ``id`` because the id is what a browser sends back as
    ``Last-Event-ID``. Set to the skipped position, it is what stops a
    reconnect from resuming *before* the unreadable row and meeting it again on
    every attempt -- the same reason the page's ``resume_after`` counts
    examined rows rather than delivered ones. The consequence is stated rather
    than hidden: the client's cursor moves past a position it never received,
    and this frame is the record that it did.

    The decode failure's reason is deliberately absent. It describes this
    process's envelope contract to whoever holds the socket, and the operator
    who has to repair the row already has it in the log line the isolating read
    wrote against the same ``event_id``.
    """

    cursor = EventCursor(stream_id=stream_id, sequence=record.sequence).encode()
    body = json.dumps(
        {
            "event_id": record.event_id,
            "event_type": record.event_type,
            "schema_version": record.schema_version,
            "sequence": record.sequence,
            "stream_id": record.stream_id,
        },
        # Sorted and ASCII-escaped, so the frame is one line and byte-stable
        # across processes -- a client may hash frames, and a dict ordering is
        # not a wire format.
        sort_keys=True,
    )
    return f"id: {cursor}\nevent: {QUARANTINE_EVENT}\ndata: {body}\n\n"


__all__ = [
    "DEGRADED_EVENT",
    "HEARTBEAT",
    "LAST_EVENT_ID_HEADER",
    "QUARANTINE_EVENT",
    "IsolatingEventLog",
    "LiveEventChannel",
    "LiveSubscription",
    "TooManyLiveSubscribersError",
    "degraded_frame",
    "frame_for",
    "live_frames",
    "resume_from",
    "stream_events",
]
