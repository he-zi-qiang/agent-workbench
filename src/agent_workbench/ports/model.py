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

    def tool_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.tools)


class ModelTextDelta(DomainModel):
    """Incremental assistant text."""

    kind: Literal["text_delta"] = "text_delta"
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
    ModelTextDelta | ModelToolCallProposed | ModelUsageReported | ModelStreamCompleted,
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
    "ModelToolCallProposed",
    "ModelUsageReported",
]
