"""Shortening a conversation without lying about what happened (ADR-081).

The vocabulary for this has been in the domain since the baseline was written:
``ContextCompacted`` is a durable event, ``compacting`` is a legal run state
with a live edge out of ``recording_results``, and ``compaction_summary`` is an
artifact kind. Nothing emitted any of it -- ``docs/known-gaps.md`` D-06 called
that "未接线", not refused, and it was right: a protocol landing before its
implementation is fine, but the existence of an event type is not a capability.

This module is the part of the implementation that can be reasoned about
without a model: *which* messages go, what the summariser is shown, and what
the shortened list looks like afterwards. The model call itself belongs to the
runtime, because it must be the same `ModelPort` call every other turn makes --
metered on the same ledger, priced, cancellable, and visible as
``ModelStarted``/``ModelCompleted``. A private "just call the provider" helper
would put a request outside the run's own accounting, which is how a run's
recorded cost stops matching its bill.

Three rules decide the cut, and each of them exists because the obvious version
is wrong:

**The head survives.** A conversation whose first message is gone is a
conversation missing the thing it is about. It is also the message a provider
is most likely to have opinions about -- several reject a conversation that
does not begin with a user turn -- so keeping it means compaction never
produces a shape that was not already legal.

**The cut lands on a protocol boundary.** Runtime invariant 1 gives every
exposed ``tool_call_id`` exactly one result, and a naive "keep the last N"
cuts straight between an assistant's ``tool_use`` and the ``tool`` message
answering it. The kept tail is therefore advanced forward until it does not
begin with a ``tool`` message; the removed span keeps its own pairs whole for
the same reason.

**Nothing is dropped silently.** If the summary call fails, the messages stay.
A shortened conversation with no account of what was removed is worse than a
turn that stops and says it could not continue: the model would carry on from a
history with a hole in it, and neither it nor the reader could tell.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from agent_workbench.domain.messages import Message, assistant_message

#: How many recent messages survive verbatim.
#:
#: A constant rather than a setting. The number that matters is the one this
#: has to be *large enough* for -- the model must still see the tool results it
#: is mid-way through using -- and that is a property of how the loop works,
#: not of a deployment. A knob here would be one more number nobody has a
#: reason to change, which this repository keeps deleting (ADR-059).
#:
#: Six is three assistant/tool pairs: the turn that triggered compaction, the
#: one before it, and one more for whatever those two were following up on.
KEEP_LAST_MESSAGES: Final[int] = 6

#: What the summariser may be shown in one call.
#:
#: Bounded because the thing being summarised is, by construction, the largest
#: conversation this run has had -- feeding it back whole is how a compaction
#: call becomes the request that overruns the window it was invoked to fix.
#: The oldest text is dropped first and the rendering says so, because a
#: summary of a silently truncated history is a summary that reads complete.
MAX_SUMMARY_INPUT_CHARS: Final[int] = 24_000

#: The marker that opens the message the summary is carried in.
#:
#: The summary re-enters the conversation as an *assistant* message, and this
#: line is what keeps that honest. It is the agent's own account of its own
#: earlier work, so the assistant role is the true one -- but without a marker
#: the model would read a paragraph it does not remember writing as something
#: it wrote verbatim, and quote it back as though it were a tool result.
#:
#: Not a `user` message, which was the obvious alternative and is the one shape
#: this must not take: it would put words in the user's mouth, and the audit
#: record would say a person said something nobody said.
SUMMARY_MARKER: Final[str] = "[earlier turns of this run, summarised]"


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    """Which messages survive verbatim, and which are summarised away."""

    #: The first message, always kept. See the module docstring.
    head: tuple[Message, ...]
    #: What the summariser is asked to account for.
    removed: tuple[Message, ...]
    #: The recent tail, kept exactly as it was.
    kept: tuple[Message, ...]

    def rebuilt(self, summary: str) -> list[Message]:
        """The shortened conversation, with the summary standing in.

        The summary sits between the head and the tail rather than at the
        front, because that is where the messages it replaces were: a reader --
        or a model -- following the list in order meets the task, then an
        account of what was done about it, then the work in progress.
        """

        return [
            *self.head,
            assistant_message(text=f"{SUMMARY_MARKER}\n{summary}"),
            *self.kept,
        ]


def plan_compaction(
    messages: Sequence[Message], *, keep_last: int = KEEP_LAST_MESSAGES
) -> CompactionPlan | None:
    """Decide what to shorten, or answer ``None`` when there is nothing to gain.

    ``None`` is a real answer and the caller must treat it as one: a
    conversation of four long messages cannot be made shorter by this
    mechanism, and pretending otherwise would spend a compaction call and leave
    the run in exactly the state that provoked it.
    """

    if keep_last < 1:
        raise ValueError("keep_last must be at least 1")
    if len(messages) <= keep_last + 1:
        # Head plus tail is already the whole list; there is no middle.
        return None

    cut = len(messages) - keep_last
    # Forward, never backward. Advancing keeps *fewer* messages and can only
    # shorten the tail; retreating would enlarge it and could walk the cut back
    # past the head, producing a plan that removes nothing while claiming to.
    while cut < len(messages) and messages[cut].role == "tool":
        cut += 1
    if cut <= 1 or cut >= len(messages):
        # Everything after the head is one unbreakable span, or the boundary
        # walked off the end. Either way there is nothing safe to remove.
        return None

    return CompactionPlan(
        head=(messages[0],),
        removed=tuple(messages[1:cut]),
        kept=tuple(messages[cut:]),
    )


def render_for_summary(
    messages: Sequence[Message], *, max_chars: int = MAX_SUMMARY_INPUT_CHARS
) -> str:
    """The removed span, as text a summariser can read.

    Rendered rather than replayed as a conversation. Handing a model a partial
    message list to "summarise" asks it to continue a dialogue that has tool
    calls in it; handing it a transcript asks it to describe one, which is the
    job. It also means the summary call cannot accidentally propose a tool.

    Oldest-first truncation, announced. The alternative -- dropping the newest
    -- would summarise a history that stops before the part that made the
    conversation too long in the first place.
    """

    rendered: list[str] = []
    for message in messages:
        lines: list[str] = []
        text = message.text().strip()
        if text:
            lines.append(text)
        for call in message.tool_calls():
            lines.append(f"(called {call.tool_name})")
        for block in message.content:
            status = getattr(block, "status", None)
            if status is None:
                continue
            name = getattr(block, "tool_name", "tool")
            result = str(getattr(block, "text", "")).strip()
            lines.append(f"({name} -> {status})" + (f" {result}" if result else ""))
        if lines:
            rendered.append(f"{message.role}: " + "\n".join(lines))

    body = "\n\n".join(rendered)
    if len(body) <= max_chars:
        return body
    # The count is the dropped characters, not the kept ones: what the reader
    # of the summary needs to know is how much is missing from it.
    dropped = len(body) - max_chars
    return (
        f"[the first {dropped} characters of this excerpt are not shown]\n\n"
        + body[dropped:]
    )


def scaled_tokens_after(
    tokens_before: int, *, chars_before: int, chars_after: int
) -> int:
    """What the shortened conversation costs, in the units of the measured one.

    ``ContextCompacted.tokens_before`` is a real measurement -- the provider's
    ``input_tokens`` for the request that was too large. There is no matching
    measurement for *after*: the next request has not been sent, and the only
    thing that could produce one is the call this compaction exists to make
    survivable.

    So ``after`` is the measured ``before`` scaled by the character ratio of
    the two message lists. That is an estimate, and naming it here is the
    point: the two numbers are in the same units and their ratio is exactly the
    character ratio, which is the honest thing to publish. Estimating *both*
    would have thrown away the one figure that was measured, and an event whose
    ``tokens_before`` disagreed with the ``ModelCompleted`` beside it would
    make a reader doubt both.
    """

    if chars_before <= 0:
        return 0
    scaled = tokens_before * chars_after // chars_before
    return max(0, min(scaled, tokens_before))


def conversation_chars(messages: Sequence[Message]) -> int:
    """How long a message list is, for the scaling above.

    Counts the text a model reads -- prose, tool names, tool result bodies --
    rather than the serialized JSON, because the ratio is meant to track what
    the model is charged for and not what the wire format costs to frame.
    """

    total = 0
    for message in messages:
        total += len(message.text())
        for call in message.tool_calls():
            total += len(call.tool_name)
        for block in message.content:
            total += len(str(getattr(block, "text", "")))
    return total


__all__ = [
    "KEEP_LAST_MESSAGES",
    "MAX_SUMMARY_INPUT_CHARS",
    "SUMMARY_MARKER",
    "CompactionPlan",
    "conversation_chars",
    "plan_compaction",
    "render_for_summary",
    "scaled_tokens_after",
]
