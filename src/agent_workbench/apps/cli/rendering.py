"""Rendering one run for a terminal.

The CLI consumes the unified event protocol and the returned outcome, and
nothing else. It never reaches into an executor, a model adapter or a store, so
anything it cannot show is something the event protocol does not carry. That is
the point: this view, an SSE connection and the audit log describe the same run
because they read the same events.

The two views draw from deliberately different sources. Streamed text comes
from live transient deltas; the timeline is replayed from the durable log after
the run. Showing both makes the durability rule concrete -- the answer that
arrived token by token is not what a reconnecting client would replay.

No view prints the prompt, a retrieved document or tool arguments, because no
event carries them: bodies stay out of the log, so they stay out of every
consumer of the log by construction. The assistant's own answer is the
exception, and a deliberate one -- a run that cannot show what it answered is
not auditable.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, TextIO

from agent_workbench.domain.events import (
    ContextBuilt,
    EventEnvelope,
    EventPayload,
    ModelCompleted,
    ModelDelta,
    ModelStarted,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolProposed,
)
from agent_workbench.domain.runs import AgentOutcome

TIMELINE_HEADER = "--- durable timeline ---"


class Renderer(Protocol):
    """How the CLI presents a run."""

    def start(self, prompt: str) -> None: ...

    def on_event(self, envelope: EventEnvelope) -> None: ...

    def finish(
        self,
        outcome: AgentOutcome,
        durable: Sequence[EventEnvelope],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class JsonRenderer:
    """One JSON object per line: every live event, then the outcome.

    Live means durable and transient alike, so a consumer sees the token deltas
    that replay would not return.
    """

    stream: TextIO

    def start(self, prompt: str) -> None:
        # The prompt is input, not an event. Nothing that is not in the
        # protocol enters the machine-readable transcript.
        return None

    def on_event(self, envelope: EventEnvelope) -> None:
        self._write(
            {
                "record": "event",
                "event": json.loads(envelope.model_dump_json()),
            }
        )

    def finish(
        self,
        outcome: AgentOutcome,
        durable: Sequence[EventEnvelope],
    ) -> None:
        self._write(
            {
                "record": "outcome",
                "durable_event_count": len(durable),
                "outcome": json.loads(outcome.model_dump_json()),
            }
        )

    def _write(self, payload: dict[str, object]) -> None:
        json.dump(payload, self.stream, ensure_ascii=False, sort_keys=True)
        self.stream.write("\n")


@dataclass(slots=True)
class TextRenderer:
    """A streamed answer followed by the timeline replay would return."""

    stream: TextIO
    _streamed: bool = field(default=False, init=False)

    def start(self, prompt: str) -> None:
        self.stream.write(f"> {prompt}\n")

    def on_event(self, envelope: EventEnvelope) -> None:
        payload = envelope.payload
        if isinstance(payload, ModelDelta):
            self.stream.write(payload.text)
            self._streamed = True

    def finish(
        self,
        outcome: AgentOutcome,
        durable: Sequence[EventEnvelope],
    ) -> None:
        if self._streamed:
            self.stream.write("\n")
        self.stream.write(f"\n{TIMELINE_HEADER}\n")
        for envelope in durable:
            self.stream.write(f"{format_timeline_row(envelope)}\n")
        self.stream.write(f"\n{format_outcome(outcome)}\n")


def format_timeline_row(envelope: EventEnvelope) -> str:
    """One durable event as a fixed-width row."""

    sequence = "-" if envelope.sequence is None else str(envelope.sequence)
    summary = summarize_payload(envelope.payload)
    row = f"{sequence:>3}  {envelope.event_type:<16}"
    return f"{row}  {summary}" if summary else row


def summarize_payload(payload: EventPayload) -> str:
    """A one-line description of what an event says."""

    if isinstance(payload, RunStarted):
        tools = ", ".join(payload.tool_names) or "none"
        return (
            f"{payload.run_kind} profile={payload.model_profile} "
            f"max_steps={payload.budget.max_steps} tools={tools}"
        )
    if isinstance(payload, ContextBuilt):
        return (
            f"chunks={payload.chunk_count} citations={payload.citation_count} "
            f"tokens~{payload.token_estimate}"
        )
    if isinstance(payload, ModelStarted):
        return f"{payload.model_call_id} model={payload.model_id}"
    if isinstance(payload, ModelCompleted):
        return (
            f"{payload.finish_reason} in={payload.usage.input_tokens} "
            f"out={payload.usage.output_tokens}"
        )
    if isinstance(payload, ToolProposed):
        return (
            f"{payload.tool_name} args={payload.argument_bytes}B "
            f"sha256={payload.argument_sha256[:12]}…"
        )
    if isinstance(payload, RunCompleted):
        return f"{payload.stop_reason} steps={payload.usage.steps}"
    if isinstance(payload, RunFailed):
        return f"{payload.stop_reason} {payload.error.code}: {payload.error.message}"
    if isinstance(payload, RunCancelled):
        return payload.reason_code
    return ""


def format_outcome(outcome: AgentOutcome) -> str:
    """The terminal line: status, why it stopped and what it spent."""

    usage = outcome.usage
    line = (
        f"outcome: {outcome.status} ({outcome.stop_reason}) "
        f"steps={usage.steps} tool_calls={usage.tool_calls} "
        f"tokens={usage.tokens.total}"
    )
    if outcome.error is not None:
        return f"{line}\nerror: {outcome.error.code}: {outcome.error.message}"
    return line


__all__ = [
    "TIMELINE_HEADER",
    "JsonRenderer",
    "Renderer",
    "TextRenderer",
    "format_outcome",
    "format_timeline_row",
    "summarize_payload",
]
