"""The pieces composed: model proposes, policy allows, tools answer, log records.

There is no runtime yet, so this test plays the loop by hand. That is the
point: it proves the ports fit together before any component owns them, and it
is the shape the runtime's first vertical slice has to reproduce.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.adapters.testing import FakeStack, fake_stack
from agent_workbench.domain.events import (
    ModelDelta,
    PermissionResolved,
    RunCompleted,
    RunStarted,
    ToolCompleted,
    ToolProposed,
    ToolStarted,
)
from agent_workbench.domain.messages import (
    ToolResultBlock,
    assistant_message,
    tool_message,
    user_message,
)
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.runs import RunBudget
from agent_workbench.domain.tools import ToolCall, ToolResult, align_results
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.model import (
    ModelRequest,
    ModelTextDelta,
    ModelToolCallProposed,
)
from agent_workbench.ports.tools import ToolInvocation

CORPUS = {"doc_1": "Qdrant owns hybrid fusion."}
SCOPE = EventScope(stream_id="stream_1", run_id="run_1")
BUDGET = RunBudget(max_steps=4, max_tool_calls=8)
CONTEXT = ExecutionContext(
    principal=PrincipalContext(principal_id="user_1", tenant_id="tenant_a"),
    envelope=AuthorizationEnvelope(allowed_tools=("read_document",)),
    agent_run_id="run_1",
    policy_identity="policy-v1:0e67f8dd84919551",
)
PROVIDER_CALL = ToolCall(
    tool_call_id="toolu_01A09q90qw90lq917835lq9",
    tool_name="read_document",
    arguments={"document_id": "doc_1"},
)


def _clock() -> datetime:
    return datetime(2026, 7, 25, 3, 14, 15, tzinfo=UTC)


def _stack() -> FakeStack:
    return fake_stack(
        turns=[
            ScriptedTurn(text="Let me read that.", tool_calls=(PROVIDER_CALL,)),
            ScriptedTurn(text="Qdrant owns fusion."),
        ],
        corpus=CORPUS,
        clock=_clock,
    )


async def _one_pass(stack: FakeStack) -> tuple[str, tuple[ToolResult, ...]]:
    """Drive one model turn, one tool round and one closing turn."""

    sink = stack.sink(SCOPE)
    await sink.emit(RunStarted(run_kind="chat", model_profile="main", budget=BUDGET))

    messages = (user_message("Who owns hybrid fusion?"),)
    text_parts: list[str] = []
    proposed: list[ToolCall] = []

    request = ModelRequest(messages=messages, tools=stack.registry.specs())
    async for event in stack.model.stream(request):
        if isinstance(event, ModelTextDelta):
            text_parts.append(event.text)
            await sink.emit(ModelDelta(model_call_id="mc_1", text=event.text))
        elif isinstance(event, ModelToolCallProposed):
            proposed.append(event.call)
            await sink.emit(
                ToolProposed(
                    tool_call_id=event.call.tool_call_id,
                    tool_name=event.call.tool_name,
                    argument_bytes=len(str(event.call.arguments)),
                    argument_sha256="c" * 64,
                )
            )

    results: list[ToolResult] = []
    for call in proposed:
        decision = await stack.policy.decide(call, CONTEXT)
        await sink.emit(
            PermissionResolved(
                tool_call_id=call.tool_call_id,
                effect=decision.effect,
                reason_code=decision.reason_code,
            )
        )
        binding = stack.registry.get(call.tool_name)
        assert binding is not None
        await sink.emit(
            ToolStarted(tool_call_id=call.tool_call_id, tool_name=call.tool_name)
        )
        result = await binding.handler(
            ToolInvocation(
                call=call,
                context=CONTEXT,
                cancellation=stack.cancellation,
                timeout_seconds=binding.spec.timeout_seconds,
            )
        )
        results.append(result)
        await sink.emit(
            ToolCompleted(
                tool_call_id=call.tool_call_id,
                duration_ms=1,
                output_bytes=len(result.content),
            )
        )

    aligned = align_results(proposed, results)
    conversation = (
        *messages,
        assistant_message(text="".join(text_parts), tool_calls=tuple(proposed)),
        tool_message(aligned),
    )

    final_parts: list[str] = []
    async for event in stack.model.stream(
        ModelRequest(messages=conversation, tools=stack.registry.specs())
    ):
        if isinstance(event, ModelTextDelta):
            final_parts.append(event.text)

    await sink.emit(RunCompleted(stop_reason="completed"))
    return "".join(final_parts), aligned


def test_the_ports_compose_into_one_tool_round() -> None:
    answer, results = asyncio.run(_one_pass(_stack()))

    assert answer == "Qdrant owns fusion."
    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].content == CORPUS["doc_1"]
    assert results[0].tool_call_id == PROVIDER_CALL.tool_call_id


def test_the_durable_timeline_omits_the_token_deltas() -> None:
    async def scenario() -> list[str]:
        stack = _stack()
        await _one_pass(stack)
        replayed = await stack.events.read(SCOPE.stream_id)
        return [envelope.event_type for envelope in replayed]

    timeline = asyncio.run(scenario())

    assert timeline == [
        "RunStarted",
        "ToolProposed",
        "PermissionResolved",
        "ToolStarted",
        "ToolCompleted",
        "RunCompleted",
    ]
    assert "ModelDelta" not in timeline


def test_the_durable_timeline_is_sequentially_replayable() -> None:
    async def scenario() -> list[int | None]:
        stack = _stack()
        await _one_pass(stack)
        replayed = await stack.events.read(SCOPE.stream_id)
        return [envelope.sequence for envelope in replayed]

    assert asyncio.run(scenario()) == [1, 2, 3, 4, 5, 6]


def test_the_second_model_request_carries_the_tool_result() -> None:
    """The provider's call id has to survive into the answer turn."""

    async def scenario() -> ModelRequest:
        stack = _stack()
        await _one_pass(stack)
        model = stack.model
        assert isinstance(model, FakeModel)
        return model.requests[1]

    second = asyncio.run(scenario())
    tool_turn = second.messages[-1]
    block = tool_turn.content[0]

    assert tool_turn.role == "tool"
    assert isinstance(block, ToolResultBlock)
    assert block.tool_call_id == PROVIDER_CALL.tool_call_id
    assert block.text == CORPUS["doc_1"]


def test_the_model_request_advertises_the_registered_tools() -> None:
    async def scenario() -> tuple[str, ...]:
        stack = _stack()
        await _one_pass(stack)
        return ModelRequest(
            messages=(user_message("x"),),
            tools=stack.registry.specs(),
        ).tool_names()

    assert asyncio.run(scenario()) == ("read_document", "text_statistics")
