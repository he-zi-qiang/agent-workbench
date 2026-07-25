"""Provider-neutral conversation messages.

The core never reads an Anthropic, OpenAI or LangChain message object. Every
adapter converts into the blocks below, which is what makes the round trip
testable: a tool call id issued by a provider must survive conversion into a
message, execution, conversion back, and JSON serialization at both ends. Lose
it anywhere and the model receives a result it cannot attach to its request.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, model_validator

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel, JsonObject, VersionedModel
from agent_workbench.domain.tools import (
    ProposedToolName,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)

MessageRole = Literal["system", "user", "assistant", "tool"]

MessageText = Annotated[str, StringConstraints(max_length=1_048_576)]


class TextBlock(DomainModel):
    """Plain text authored by a user, the system prompt or the model."""

    kind: Literal["text"] = "text"
    text: MessageText


class ToolUseBlock(DomainModel):
    """A model's request to run a tool."""

    kind: Literal["tool_use"] = "tool_use"
    tool_call_id: Identifier
    tool_name: ProposedToolName
    arguments: JsonObject = Field(default_factory=dict)

    def to_tool_call(self, *, model_call_id: str | None = None) -> ToolCall:
        return ToolCall(
            tool_call_id=self.tool_call_id,
            tool_name=self.tool_name,
            arguments=self.arguments,
            model_call_id=model_call_id,
        )


class ToolResultBlock(DomainModel):
    """The model-facing projection of a ``ToolResult``.

    Only what the model needs travels here. Duration, error codes and the full
    execution record stay in the event log and the tool ledger.
    """

    kind: Literal["tool_result"] = "tool_result"
    tool_call_id: Identifier
    tool_name: ProposedToolName
    status: ToolResultStatus
    text: MessageText = ""
    artifact: ArtifactRef | None = None

    @classmethod
    def from_tool_result(cls, result: ToolResult) -> ToolResultBlock:
        text = result.content
        if result.status == "error" and result.error is not None and not text:
            text = f"{result.error.code}: {result.error.message}"
        return cls(
            tool_call_id=result.tool_call_id,
            tool_name=result.tool_name,
            status=result.status,
            text=text,
            artifact=result.artifact,
        )


ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="kind"),
]

_ROLE_BLOCK_KINDS: Final[dict[MessageRole, frozenset[str]]] = {
    "system": frozenset({"text"}),
    "user": frozenset({"text"}),
    "assistant": frozenset({"text", "tool_use"}),
    "tool": frozenset({"tool_result"}),
}


class Message(VersionedModel):
    """One conversation turn."""

    role: MessageRole
    content: tuple[ContentBlock, ...]

    @model_validator(mode="after")
    def validate_blocks(self) -> Message:
        if not self.content:
            raise ValueError("a message must carry at least one content block")

        allowed = _ROLE_BLOCK_KINDS[self.role]
        for block in self.content:
            if block.kind not in allowed:
                raise ValueError(
                    f"role {self.role!r} must not carry a {block.kind!r} block"
                )

        seen: set[str] = set()
        for block in self.content:
            if isinstance(block, ToolUseBlock | ToolResultBlock):
                # A duplicate id inside one message would make the mandatory
                # one-result-per-call pairing ambiguous.
                if block.tool_call_id in seen:
                    raise ValueError(
                        f"duplicate tool_call_id {block.tool_call_id} in one message"
                    )
                seen.add(block.tool_call_id)
        return self

    def tool_calls(self, *, model_call_id: str | None = None) -> tuple[ToolCall, ...]:
        """Tool calls proposed by this message, in model order."""

        return tuple(
            block.to_tool_call(model_call_id=model_call_id)
            for block in self.content
            if isinstance(block, ToolUseBlock)
        )

    def text(self) -> str:
        """Concatenated plain text, ignoring tool blocks."""

        return "".join(
            block.text for block in self.content if isinstance(block, TextBlock)
        )


def user_message(text: str) -> Message:
    return Message(role="user", content=(TextBlock(text=text),))


def assistant_message(
    *,
    text: str = "",
    tool_calls: tuple[ToolCall, ...] = (),
) -> Message:
    blocks: list[ContentBlock] = []
    if text or not tool_calls:
        blocks.append(TextBlock(text=text))
    blocks.extend(
        ToolUseBlock(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            arguments=call.arguments,
        )
        for call in tool_calls
    )
    return Message(role="assistant", content=tuple(blocks))


def tool_message(results: tuple[ToolResult, ...]) -> Message:
    """Build the tool turn that answers a set of calls.

    Callers pass results already aligned with the original call order, so the
    conversation preserves the model's own ordering.
    """

    return Message(
        role="tool",
        content=tuple(ToolResultBlock.from_tool_result(result) for result in results),
    )


__all__ = [
    "ContentBlock",
    "Message",
    "MessageRole",
    "MessageText",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "assistant_message",
    "tool_message",
    "user_message",
]
