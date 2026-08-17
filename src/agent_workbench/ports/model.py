"""The model boundary.

The core never touches an Anthropic, OpenAI or LangChain message object. A
model adapter converts a provider stream into the events below, which is what
lets the runtime treat every provider identically and lets a contract test
prove a provider swap changed nothing observable.

Cancellation is not a parameter here. A stream is consumed by an asyncio task,
and cancelling that task must close the underlying request; a second, parallel
cancellation channel would make it ambiguous which one actually stopped the
call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field

from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.messages import Message
from agent_workbench.domain.runs import ModelProfileName, SystemPrompt, TokenUsage
from agent_workbench.domain.schema import BoundedText, DomainModel, VersionedModel
from agent_workbench.domain.tools import ToolCall, ToolSpec

ModelFinishReason = Literal["stop", "tool_use", "max_tokens", "cancelled", "error"]


class ModelRequest(VersionedModel):
    """One model call.

    The request names a profile, never a provider or a model id: mapping a
    profile onto a concrete model, temperature and retry policy is settings'
    job, so the runtime cannot select an unreviewed model at request time.
    """

    model_profile: ModelProfileName = "main"
    system_prompt: SystemPrompt = ""
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int | None = Field(default=None, ge=1)
    # Whether this call wants the model to think before answering. ``None``
    # defers to the profile's configured default; True/False override it for
    # one call. Orthogonal to the profile the same way visibility is
    # orthogonal to depth: the profile says what the model *can* do, the
    # request says what this turn wants. A profile that declared thinking
    # unsupported ignores the override -- there is no parameter to send.
    thinking: bool | None = None

    def tool_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.tools)


class ModelTextDelta(DomainModel):
    """Incremental assistant text."""

    kind: Literal["text_delta"] = "text_delta"
    text: BoundedText


class ModelThinkingDelta(DomainModel):
    """Incremental reasoning, distinct from the answer being written.

    A separate kind rather than a flag on ``ModelTextDelta`` because the two
    texts have different fates everywhere downstream: thinking never re-enters
    the conversation ledger, never becomes the answer, and is shown -- when it
    is shown -- as process rather than product.

    The parenthesis that used to sit on that first fate said "providers require
    it withheld from the next request". That is not true of the provider this
    repository actually calls. Probed against ``api.deepseek.com`` on
    2026-08-17 with ``deepseek-v4-flash``, thinking enabled and tools declared:
    the second round returns 200 with the assistant message carrying
    ``reasoning_content``, without it, and with a deliberately truncated copy of
    it. Nothing refuses, and nothing reports that the copy was altered.

    So withholding is **this repository's choice**, not a protocol constraint --
    the reasons are in ADR-064: the benefit is unmeasured, every replayed token
    is charged to our own input budget, and a chain we clipped for the event log
    would go back as a record of reasoning the model never had.
    """

    kind: Literal["thinking_delta"] = "thinking_delta"
    text: BoundedText


class ModelToolCallProposed(DomainModel):
    """A complete, parsed tool call.

    Adapters emit this only once the provider has finished the call: partial
    JSON never reaches the runtime, so schema validation and policy always see
    the arguments the model actually meant.
    """

    kind: Literal["tool_call"] = "tool_call"
    call: ToolCall


class ModelUsageReported(DomainModel):
    """Token accounting for the call so far."""

    kind: Literal["usage"] = "usage"
    usage: TokenUsage


class ModelStreamCompleted(DomainModel):
    """Terminal event of a stream, successful or not."""

    kind: Literal["completed"] = "completed"
    finish_reason: ModelFinishReason
    usage: TokenUsage = TokenUsage()
    error: ErrorInfo | None = None


ModelEvent = Annotated[
    ModelTextDelta
    | ModelThinkingDelta
    | ModelToolCallProposed
    | ModelUsageReported
    | ModelStreamCompleted,
    Field(discriminator="kind"),
]


class ModelCall(DomainModel):
    """Identity of one physical model invocation, for tracing and events."""

    model_call_id: Identifier
    model_profile: ModelProfileName


@runtime_checkable
class ModelPort(Protocol):
    """Streaming, provider-neutral model interface."""

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream one model call.

        Implementations must end every stream with ``ModelStreamCompleted``,
        including on failure, so the runtime never has to distinguish "the
        provider stopped" from "the adapter crashed".
        """
        ...


__all__ = [
    "ModelCall",
    "ModelEvent",
    "ModelFinishReason",
    "ModelPort",
    "ModelRequest",
    "ModelStreamCompleted",
    "ModelTextDelta",
    "ModelThinkingDelta",
    "ModelToolCallProposed",
    "ModelUsageReported",
]
