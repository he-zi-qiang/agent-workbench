"""Message shape rules and the tool-call id round trip."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.messages import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    assistant_message,
    tool_message,
    user_message,
)
from agent_workbench.domain.tools import ToolCall, ToolResult, align_results

PROVIDER_TOOL_CALL_ID = "toolu_01A09q90qw90lq917835lq9"


def test_a_message_needs_at_least_one_block() -> None:
    with pytest.raises(ValidationError, match="at least one content block"):
        Message(role="user", content=())


def test_a_user_message_cannot_carry_tool_blocks() -> None:
    with pytest.raises(ValidationError, match="must not carry"):
        Message(
            role="user",
            content=(
                ToolResultBlock(
                    tool_call_id="toolu_1",
                    tool_name="read_document",
                    status="ok",
                ),
            ),
        )


def test_a_tool_message_cannot_carry_free_text() -> None:
    with pytest.raises(ValidationError, match="must not carry"):
        Message(role="tool", content=(TextBlock(text="done"),))


def test_an_assistant_message_may_mix_text_and_tool_use() -> None:
    message = assistant_message(
        text="Checking the index.",
        tool_calls=(ToolCall(tool_call_id="toolu_1", tool_name="read_document"),),
    )

    assert message.text() == "Checking the index."
    assert len(message.tool_calls()) == 1


def test_one_message_cannot_repeat_a_tool_call_id() -> None:
    block = ToolUseBlock(tool_call_id="toolu_1", tool_name="read_document")

    with pytest.raises(ValidationError, match="duplicate tool_call_id"):
        Message(role="assistant", content=(block, block))


def test_provider_tool_call_ids_survive_the_full_round_trip() -> None:
    """Proposal, execution, answer and JSON must all preserve the id."""

    call = ToolCall(
        tool_call_id=PROVIDER_TOOL_CALL_ID,
        tool_name="knowledge_search",
        arguments={"query": "fusion owner"},
    )
    proposal = assistant_message(text="", tool_calls=(call,))

    recovered = proposal.tool_calls(model_call_id="mc_1")[0]
    assert recovered.tool_call_id == PROVIDER_TOOL_CALL_ID
    assert recovered.arguments == call.arguments
    assert recovered.model_call_id == "mc_1"

    answer = tool_message(
        align_results(
            proposal.tool_calls(),
            [ToolResult.succeeded(recovered, content="1 passage")],
        )
    )
    restored = Message.model_validate(json.loads(answer.model_dump_json()))

    assert restored == answer
    block = restored.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.tool_call_id == PROVIDER_TOOL_CALL_ID


def test_a_failed_result_explains_itself_to_the_model() -> None:
    call = ToolCall(tool_call_id="toolu_1", tool_name="read_document")
    failure = ToolResult.failed(
        call,
        ErrorInfo(code="policy_denied", message="write tools require approval"),
    )
    block = ToolResultBlock.from_tool_result(failure)

    assert block.status == "error"
    assert "policy_denied" in block.text
    assert "write tools require approval" in block.text


def test_helpers_build_the_expected_roles() -> None:
    assert user_message("hello").role == "user"
    assert assistant_message(text="hi").role == "assistant"

    call = ToolCall(tool_call_id="toolu_1", tool_name="read_document")
    assert tool_message((ToolResult.succeeded(call),)).role == "tool"


def test_an_empty_tool_turn_is_rejected() -> None:
    """A tool turn answers calls; an empty one would answer nothing."""

    with pytest.raises(ValidationError, match="at least one content block"):
        tool_message(())
