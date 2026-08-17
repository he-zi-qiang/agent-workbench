"""Serialization primitives shared by every domain object.

Domain objects cross process, storage and protocol boundaries: the same value
becomes a PostgreSQL row, an SSE frame and a LangGraph checkpoint entry. Two
properties therefore matter more than convenience.

Every aggregate that is serialized on its own carries an explicit schema
version, so a consumer never has to guess which contract produced a payload.
And no domain object accepts a field it does not know: an unexpected key means
a producer and a consumer disagree, which is a defect to surface at the
boundary rather than data to silently drop.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)

DOMAIN_SCHEMA_VERSION: Final[int] = 1

# Tool arguments, tool output and policy overrides are user- and model-supplied
# structures. They stay JSON values inside the domain; only an adapter is
# allowed to turn them into a vendor object.
JsonObject = dict[str, JsonValue]

# Free text copied into events, errors or model context is always bounded. An
# unbounded string is an unbounded database row, an unbounded SSE frame and an
# unbounded prompt at the same time.
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
BOUNDED_TEXT_LIMIT: Final[int] = 4096
BoundedText = Annotated[str, StringConstraints(max_length=BOUNDED_TEXT_LIMIT)]

# What a model's completed answer may be, which is not the same ceiling as text
# recorded *about* a run. A prompt preview and a proposed tool call are
# summaries a reader consults; an answer is the thing that was asked for -- a
# report the export node exports, a chat reply, a bundle of quoted passages.
# Sharing one type made the preview's ceiling the answer's: an 18,010-character
# report reached its artifact as 4,098 bytes, cut mid-sentence, and nothing
# downstream could tell (ADR-035).
#
# Sized by what one invocation can actually produce rather than by what looks
# tidy: `multi_agent.max_tokens_per_agent_invocation` defaults to 16,000, and
# 16,000 output tokens is roughly 64,000 characters of English. Still bounded,
# for the reason every string here is bounded -- this is a database row and an
# SSE frame. What makes the larger ceiling affordable is that it is one row per
# answer, where a preview is one per step.
ANSWER_TEXT_LIMIT: Final[int] = 65_536
AnswerText = Annotated[str, StringConstraints(max_length=ANSWER_TEXT_LIMIT)]

# What one model call's recorded reasoning may be, which is a third ceiling and
# needs to be: a preview is a summary a reader consults, an answer is the thing
# that was asked for, and a chain of thought is the account of how the answer
# came about. Sharing the preview's ceiling made the account's -- and it is
# `chat.retrieval_shape`'s cousin of a bug, invisible from the outside, because
# a cut chain still reads like a chain.
#
# Sized from measurement rather than rounded to something tidy. Both profiles
# in this repository that enable thinking run `reasoning_effort = "low"`
# (config.code-local.toml, config.demo-local.toml), and one call measured 1,503
# characters there; the same question at `high` measured 5,067. 16,384 leaves
# the high tier roughly threefold headroom while staying a quarter of the answer
# ceiling -- one row per model call is what makes the larger bound affordable,
# and it is also why it must not be as large as an answer's, which is one row
# per run.
THINKING_TEXT_LIMIT: Final[int] = 16_384
ThinkingText = Annotated[str, StringConstraints(max_length=THINKING_TEXT_LIMIT)]

#: Fixed length, so the remaining budget is exactly computable. It deliberately
#: does not name how much was dropped: that number would have to be computed
#: using the length of the marker that carries it, which is self-referential and
#: off by a few characters however it is rounded.
#:
#: No full-width brackets around the phrase, though Chinese typography would
#: normally take them. They would trip RUF001, and the only way to keep them is
#: a per-file ignore -- which on *this* file would switch off a rule whose real
#: job is catching a Cyrillic lookalike inside a domain identifier. The ellipses
#: already do the bracketing work, so the rule keeps doing its job and the
#: reader still gets a Chinese sentence.
_THINKING_ELLIPSIS: Final[str] = "\n…… 中段省略 ……\n"


def bounded(value: str) -> BoundedText:
    """Cut ``value`` to what a ``BoundedText`` field can hold.

    For text that is recorded *about* a run rather than produced by it -- a
    prompt, a proposed tool call -- where being over the limit must not make
    the event impossible to construct. The marker is deliberate: a reader who
    cannot see the cut would take a truncated prompt for the whole one.

    Not for the model's own output. That is bounded at its source, and silently
    trimming an answer here would publish a different answer than the one the
    provider returned.
    """

    if len(value) <= BOUNDED_TEXT_LIMIT:
        return value
    return value[: BOUNDED_TEXT_LIMIT - 1] + "…"


def bounded_thinking(value: str) -> ThinkingText:
    """Cut one chain of reasoning to what a ``ThinkingText`` field holds,
    keeping both ends.

    The whole difference from :func:`bounded` is that last clause, and it is the
    point. Reasoning runs "here is what I saw, therefore here is what I will
    do": cutting from the front keeps the throat-clearing and drops the
    conclusion, reliably, every time. The conclusion is the only part a reader
    scrolling back to a tool call they did not expect actually wants -- they are
    asking *why did it run that*, and the answer is in the last sentence.

    Three quarters to the account, one quarter to the conclusion. Not halves:
    the reasoning that explains the decision is usually longer than the decision,
    and a reader who has the setup can infer more than one who has only the end.
    """

    if len(value) <= THINKING_TEXT_LIMIT:
        return value
    keep = THINKING_TEXT_LIMIT - len(_THINKING_ELLIPSIS)
    tail = keep // 4
    head = keep - tail
    return value[:head] + _THINKING_ELLIPSIS + value[-tail:]


# What a tool hands the model, which is not the same ceiling as what a model
# writes. The remaining uses of BoundedText are a streamed delta -- one slice of
# an answer, not the whole of one -- and text recorded about a run; 4096
# characters is a generous bound on those. A retrieval tool's result is input,
# and its natural size is the evidence it was asked for: this project chunks at
# 512 tokens and knowledge_search's own default top_k is 8, so a result is about
# 16,000 characters. Sharing one type made that result impossible to return --
# not truncated, refused, because the value could not be constructed at all.
#
# Still bounded, and deliberately not by much more than a real result needs: a
# tool result goes into a prompt, so an unbounded one is an unbounded context
# window. The operative limit is each tool's own budget; this is the backstop
# that stops a tool with no budget from being unbounded.
ToolOutputText = Annotated[str, StringConstraints(max_length=65_536)]


class DomainModel(BaseModel):
    """Immutable value object with a closed field set."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        # Rejected input is echoed into ValidationError by default. Domain
        # objects carry document text, tool arguments and model output, so the
        # input stays out of the error surface.
        hide_input_in_errors=True,
    )


class VersionedModel(DomainModel):
    """Aggregate that is persisted or transmitted as a standalone payload."""

    schema_version: int = Field(default=DOMAIN_SCHEMA_VERSION, ge=1)

    @field_validator("schema_version")
    @classmethod
    def reject_unsupported_schema_version(cls, value: int) -> int:
        # Fail closed instead of best-effort parsing: a payload written by a
        # different contract version is a migration decision, not a fallback.
        if value != DOMAIN_SCHEMA_VERSION:
            raise ValueError(
                "unsupported domain schema version: expected "
                f"{DOMAIN_SCHEMA_VERSION}, received {value}"
            )
        return value


__all__ = [
    "ANSWER_TEXT_LIMIT",
    "BOUNDED_TEXT_LIMIT",
    "DOMAIN_SCHEMA_VERSION",
    "AnswerText",
    "BoundedText",
    "DomainModel",
    "JsonObject",
    "ShortText",
    "VersionedModel",
    "bounded",
]
