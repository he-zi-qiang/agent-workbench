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
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_workbench.domain.context import Citation
from agent_workbench.domain.events import (
    AnswerCommitted,
    AnswerWithheld,
    EventEnvelope,
    EventPayload,
    ModelCompleted,
    ModelDelta,
    UngroundedAnswerCommitted,
)
from agent_workbench.ports.event_log import EventKey, EventSink


@dataclass(slots=True)
class AnswerReleaseSink:
    """Redact pre-commit model text and publish exactly one terminal answer."""

    inner: EventSink
    _released: bool = field(default=False, init=False)

    async def emit(
        self,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        """Forward an audit-safe form of a runtime event.

        Deltas are transient today, but an observing sink can still expose
        them to a live client. Redacting both deltas and completed turns keeps
        the rule true independently of whether an event is persisted.
        """

        if isinstance(payload, ModelDelta):
            payload = payload.model_copy(update={"text": ""})
        elif isinstance(payload, ModelCompleted):
            payload = payload.model_copy(update={"text": "", "output_ref": None})
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


__all__ = ["AnswerReleaseSink"]
