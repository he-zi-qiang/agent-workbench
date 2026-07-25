"""Contract every model adapter must satisfy, checked against the fake."""

from __future__ import annotations

import asyncio

from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.domain.messages import user_message
from agent_workbench.domain.runs import TokenUsage
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.model import (
    ModelEvent,
    ModelRequest,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelToolCallProposed,
    ModelUsageReported,
)

PROVIDER_TOOL_CALL_ID = "toolu_01A09q90qw90lq917835lq9"


def _request() -> ModelRequest:
    return ModelRequest(messages=(user_message("hello"),))


def _drain(model: FakeModel, request: ModelRequest | None = None) -> list[ModelEvent]:
    async def scenario() -> list[ModelEvent]:
        return [event async for event in model.stream(request or _request())]

    return asyncio.run(scenario())


def test_a_stream_always_ends_with_a_completion() -> None:
    model = FakeModel([ScriptedTurn(text="hello there")])

    events = _drain(model)

    assert isinstance(events[-1], ModelStreamCompleted)
    assert events[-1].finish_reason == "stop"


def test_text_arrives_as_deltas_that_reassemble_exactly() -> None:
    model = FakeModel([ScriptedTurn(text="abcdefghij")], delta_size=4)

    events = _drain(model)
    deltas = [event for event in events if isinstance(event, ModelTextDelta)]

    assert [delta.text for delta in deltas] == ["abcd", "efgh", "ij"]
    assert "".join(delta.text for delta in deltas) == "abcdefghij"


def test_tool_calls_arrive_whole_and_keep_their_provider_id() -> None:
    """Partial arguments must never reach validation or policy."""

    call = ToolCall(
        tool_call_id=PROVIDER_TOOL_CALL_ID,
        tool_name="read_document",
        arguments={"document_id": "doc_1"},
    )
    model = FakeModel([ScriptedTurn(text="looking", tool_calls=(call,))])

    proposals = [
        event for event in _drain(model) if isinstance(event, ModelToolCallProposed)
    ]

    assert len(proposals) == 1
    assert proposals[0].call == call
    assert proposals[0].call.tool_call_id == PROVIDER_TOOL_CALL_ID


def test_a_turn_with_tool_calls_finishes_for_tool_use() -> None:
    call = ToolCall(tool_call_id="toolu_1", tool_name="read_document")
    model = FakeModel([ScriptedTurn(tool_calls=(call,))])

    completion = _drain(model)[-1]

    assert isinstance(completion, ModelStreamCompleted)
    assert completion.finish_reason == "tool_use"


def test_usage_is_reported_before_completion() -> None:
    usage = TokenUsage(input_tokens=120, output_tokens=8)
    model = FakeModel([ScriptedTurn(text="hi", usage=usage)])

    events = _drain(model)
    reported = [event for event in events if isinstance(event, ModelUsageReported)]

    assert reported[0].usage == usage
    assert events.index(reported[0]) < len(events) - 1


def test_an_exhausted_script_still_terminates_the_stream() -> None:
    """An adapter failure is a completion event, not a dangling generator."""

    model = FakeModel([ScriptedTurn(text="only turn")])
    _drain(model)

    completion = _drain(model)[-1]

    assert isinstance(completion, ModelStreamCompleted)
    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert completion.error.code == "provider_error"


def test_the_last_turn_can_repeat_for_runaway_loop_tests() -> None:
    call = ToolCall(tool_call_id="toolu_1", tool_name="read_document")
    model = FakeModel([ScriptedTurn(tool_calls=(call,))], repeat_last=True)

    for _ in range(5):
        completion = _drain(model)[-1]
        assert isinstance(completion, ModelStreamCompleted)
        assert completion.finish_reason == "tool_use"

    assert model.call_count == 5


def test_requests_are_recorded_for_assertions() -> None:
    model = FakeModel([ScriptedTurn(text="a"), ScriptedTurn(text="b")])
    first = ModelRequest(messages=(user_message("one"),))
    second = ModelRequest(messages=(user_message("two"),), model_profile="compact")

    _drain(model, first)
    _drain(model, second)

    assert model.requests == (first, second)
    assert model.requests[1].model_profile == "compact"
