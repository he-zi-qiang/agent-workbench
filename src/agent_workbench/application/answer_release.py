"""The publication boundary for retrieval-backed answers.

The agent runtime reports provider activity as it happens. Retrieval-backed
chat has a stricter rule: model output is only public after every source has
been checked again. This sink preserves the audit trail while removing the
answer-bearing fields, then exposes one explicit method for either publishing
the checked answer or publishing a safe refusal.

Keeping the wrapper in the application layer matters. The runtime is also used
by CLI and future task nodes, where ``ModelCompleted`` remains the ordinary
provider event; only the use case that owns the final evidence check may decide
when an answer is safe to reveal.

Two fences live here, and reading them together is the point. ``AnswerReleaseSink``
is for a run that will publish an answer and must not do so early.
``ProcessOnlySink`` is for a run that will never publish one at all -- it keeps
the refusal and drops the publishing methods, so "this run has no answer" is a
type rather than a habit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from agent_workbench.domain.context import Citation
from agent_workbench.domain.events import (
    TRANSIENT_EVENT_TYPES,
    AnswerCommitted,
    AnswerWithheld,
    EventEnvelope,
    EventPayload,
    ModelCompleted,
    ModelDelta,
    ModelThinkingDelta,
    UngroundedAnswerCommitted,
)
from agent_workbench.ports.event_log import EventKey, EventSink

#: Whether the text a run is producing may be shown while it is producing it.
#:
#: ``redacted`` is the rule retrieval-backed chat has always had: nothing the
#: model wrote is public until every source behind it has been checked again,
#: so a turn that ends in ``AnswerWithheld`` never leaked the answer it
#: withheld. ``provisional`` is for the shapes where that check has nothing to
#: check -- no evidence was retrieved, so no grant can be withdrawn between the
#: model finishing and the answer shipping, and there is therefore no state in
#: which the text streamed so far becomes something that must not have been
#: shown.
#:
#: Deliberately a closed pair rather than a boolean. The name of the value is
#: the argument for it; ``live_text=True`` would be a switch somebody could
#: flip for a shape whose fence it silently opens.
LiveTextPolicy = Literal["redacted", "provisional"]

#: Which transient event types this sink has an answer for.
#:
#: A whitelist, not a list of things to blank, and the direction is the whole
#: point. Transient events are the ones that reach a live subscriber without
#: passing through the durable log, so a transient type this module has never
#: heard of would sail past the fence by default. Listing them means adding one
#: to ``EVENT_DURABILITY`` without deciding what it may carry stops the process
#: at import instead of shipping a fourth way for model text to escape.
#:
#: ``ToolProgress`` passes through: its ``message`` is written by a tool
#: handler describing its own work, never by the model, so it is not answer
#: text and nothing about it changes when an answer is withheld.
#:
#: ``ModelThinkingDelta`` follows ``ModelDelta``, not ``ToolProgress``: the
#: model reasons *about* the evidence it was shown, so its thinking can quote
#: exactly what a withheld answer must not have shown. Same author, same
#: fence (ADR-061).
_TRANSIENT_HANDLED: Final[frozenset[str]] = frozenset(
    {"ModelDelta", "ModelThinkingDelta", "ToolProgress"}
)

_undecided = TRANSIENT_EVENT_TYPES - _TRANSIENT_HANDLED
if _undecided:  # pragma: no cover - a failure here stops the process at import
    raise RuntimeError(
        "these transient event types would bypass the publication fence "
        f"unexamined: {sorted(_undecided)}. Decide in AnswerReleaseSink.emit "
        "whether each may carry text a withheld answer must not have shown."
    )


@dataclass(slots=True)
class AnswerReleaseSink:
    """Redact pre-commit model text and publish exactly one terminal answer."""

    inner: EventSink
    #: Whether this turn's shape may show its text as it is written. Defaults
    #: to the strict reading, so a caller that does not think about it gets the
    #: behaviour every caller had before the question existed.
    live_text: LiveTextPolicy = "redacted"
    _released: bool = field(default=False, init=False)

    async def emit(
        self,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        """Forward an audit-safe form of a runtime event.

        Deltas are transient, but an observing sink exposes them to a live
        client, so this is where a shape's ``live_text`` policy is applied --
        the only place both kinds of event pass through.

        ``ModelCompleted`` is redacted under *either* policy, and that is not
        an oversight. A delta is what is being written; ``ModelCompleted.text``
        is the finished candidate, and the finished candidate is exactly what
        the publication methods below exist to release deliberately. Letting it
        through for a provisional shape would put an answer in the durable log
        that nothing had decided to publish.
        """

        if isinstance(payload, (ModelDelta, ModelThinkingDelta)):
            # One policy for both texts: the reasoning is written by the same
            # author about the same evidence as the answer it precedes.
            if self.live_text == "redacted":
                payload = payload.model_copy(update={"text": ""})
        elif isinstance(payload, ModelCompleted):
            payload = payload.model_copy(
                update={"text": "", "thinking_preview": "", "output_ref": None}
            )
        elif isinstance(
            payload, (AnswerCommitted, UngroundedAnswerCommitted, AnswerWithheld)
        ):
            # All three, or the new one would be the single answer event a
            # runtime could emit directly, bypassing the release rule that the
            # other two exist to enforce.
            raise RuntimeError(
                "answer events must pass through commit(), commit_ungrounded() "
                "or withhold()"
            )
        return await self.inner.emit(
            payload,
            parent_event_id=parent_event_id,
            event_key=event_key,
        )

    async def commit(
        self,
        *,
        text: str,
        citations: tuple[Citation, ...],
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        """Publish an answer only after its caller completed the final check."""

        self._ensure_unreleased()
        envelope = await self.inner.emit(
            AnswerCommitted(text=text, citations=citations),
            event_key=event_key,
        )
        self._released = True
        return envelope

    async def commit_ungrounded(
        self,
        *,
        text: str,
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        """Publish an answer that never had evidence to check (ADR-018).

        A separate method rather than ``commit(citations=())``, for the same
        reason the event is separate: an empty citation tuple is what a
        retrieval turn produces when the model cited nothing it was shown, and
        that is a very different fact from a turn where nothing was retrieved
        at all. One call site deciding to pass ``()`` would collapse the two
        into a distinction no reader of the log could recover.

        The same single-release rule applies. A turn publishes one terminal
        answer whichever shape produced it, so this cannot follow a commit or a
        withhold, and none of them can follow it.
        """

        self._ensure_unreleased()
        envelope = await self.inner.emit(
            UngroundedAnswerCommitted(text=text),
            event_key=event_key,
        )
        self._released = True
        return envelope

    async def withhold(
        self,
        *,
        text: str,
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        """Publish only the safe replacement, never the rejected answer."""

        self._ensure_unreleased()
        envelope = await self.inner.emit(
            AnswerWithheld(text=text),
            event_key=event_key,
        )
        self._released = True
        return envelope

    def _ensure_unreleased(self) -> None:
        if self._released:
            raise RuntimeError("an answer release sink may publish only once")


@dataclass(slots=True)
class ProcessOnlySink:
    """A run whose product is not an answer, and cannot become one.

    A Code session writes files and finishes with a report. It has no evidence
    to re-check, no turn ledger to move and nothing to publish: its steps are
    the record, and ``ModelCompleted.text`` is that report rather than a
    candidate awaiting release. So this forwards everything unchanged.

    Everything except the three events that mean "an answer was published".
    Those exist to be produced deliberately, by a caller that decided to, and
    this caller has no such decision to make -- an ``AnswerCommitted`` on a
    Code session's stream would tell every reader of that stream, and every
    consumer downstream of it, that something crossed a fence that was never
    there.

    Written as a fence and not as a comment because the alternative was
    measured: removing ``AnswerReleaseSink`` from a run leaves "Code emits no
    answer events" resting entirely on nobody ever writing the line. This type
    is also the reason a service can declare it *needs* one -- a bare
    ``EventSink`` handed where this is required does not type-check, which is
    the check that survives a refactor.
    """

    inner: EventSink

    async def emit(
        self,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        if isinstance(
            payload, (AnswerCommitted, UngroundedAnswerCommitted, AnswerWithheld)
        ):
            # All three, for the same reason the other fence takes all three:
            # a missing one would be the single answer event this run could
            # emit, which is precisely the hole being closed.
            raise RuntimeError(
                "a process-only run has no answer to publish: "
                f"{type(payload).__name__} may not be emitted"
            )
        return await self.inner.emit(
            payload,
            parent_event_id=parent_event_id,
            event_key=event_key,
        )


__all__ = ["AnswerReleaseSink", "LiveTextPolicy", "ProcessOnlySink"]
