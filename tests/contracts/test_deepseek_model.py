"""The DeepSeek adapter, driven by a scripted transport.

No network: a mock transport serves the exact bytes the provider would, so the
suite stays offline and every wire-format edge case is reproducible. What these
tests assert is that nothing provider-shaped escapes -- the same four
``ModelEvent`` kinds leave this adapter as leave the scripted model.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx
import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.adapters.models.deepseek import DeepSeekModel, DeepSeekProfile
from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry, read_document_tool
from agent_workbench.domain.messages import (
    assistant_message,
    tool_message,
    user_message,
)
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    ModelProfileName,
    RunBudget,
    TraceContext,
)
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.cancellation import CancellationSource
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.model import (
    ModelEvent,
    ModelPort,
    ModelRequest,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelToolCallProposed,
    ModelUsageReported,
)
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway

API_KEY = "sk-deepseek-canary-must-not-leak"
BASE_URL = "https://api.deepseek.test"
PROFILES: Mapping[ModelProfileName, DeepSeekProfile] = {
    "main": DeepSeekProfile(model_id="deepseek-chat", temperature=0.0),
    "compact": DeepSeekProfile(model_id="deepseek-chat", max_output_tokens=512),
}

SEARCH_SPEC = ToolSpec(
    name="read_document",
    description="Return one document.",
    input_schema={
        "type": "object",
        "properties": {"document_id": {"type": "string"}},
        "required": ["document_id"],
    },
    concurrency="parallel",
    risk="read",
    idempotency="safe",
    timeout_seconds=5,
)


def _sse(*chunks: dict[str, Any], done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode("utf-8")


def _text_chunk(text: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def _finish_chunk(reason: str = "stop") -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def _usage_chunk(**counts: int) -> dict[str, Any]:
    return {"choices": [], "usage": counts}


def _tool_fragment(
    index: int = 0,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> dict[str, Any]:
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    fragment: dict[str, Any] = {"index": index, "function": function}
    if call_id is not None:
        fragment["id"] = call_id
    return {"choices": [{"index": 0, "delta": {"tool_calls": [fragment]}}]}


class _Wire:
    """A scripted transport that records what the adapter sent."""

    def __init__(
        self,
        responder: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)

    @property
    def payload(self) -> dict[str, Any]:
        decoded = json.loads(self.requests[0].content)
        assert isinstance(decoded, dict)
        return decoded


def _serve(body: bytes, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return responder


def _run(
    responder: Callable[[httpx.Request], httpx.Response],
    request: ModelRequest | None = None,
    *,
    profiles: Mapping[ModelProfileName, DeepSeekProfile] = PROFILES,
) -> tuple[list[ModelEvent], _Wire]:
    wire = _Wire(responder)

    async def scenario() -> list[ModelEvent]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
            model = DeepSeekModel(
                client=client,
                api_key=API_KEY,
                base_url=BASE_URL,
                profiles=profiles,
            )
            asked = (
                request
                if request is not None
                else ModelRequest(messages=(user_message("Who owns hybrid fusion?"),))
            )
            return [event async for event in model.stream(asked)]

    return asyncio.run(scenario()), wire


def _run_bounded(
    responder: Callable[[httpx.Request], httpx.Response],
    *,
    max_argument_chars: int,
) -> tuple[list[ModelEvent], _Wire]:
    """Same as ``_run``, with the adapter's argument ceiling made small."""

    wire = _Wire(responder)

    async def scenario() -> list[ModelEvent]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
            model = DeepSeekModel(
                client=client,
                api_key=API_KEY,
                base_url=BASE_URL,
                profiles=PROFILES,
                max_argument_chars=max_argument_chars,
            )
            return [
                event
                async for event in model.stream(
                    ModelRequest(messages=(user_message("hi"),))
                )
            ]

    return asyncio.run(scenario()), wire


def _completion(events: list[ModelEvent]) -> ModelStreamCompleted:
    last = events[-1]
    assert isinstance(last, ModelStreamCompleted)
    return last


def test_a_text_answer_streams_as_deltas_and_completes() -> None:
    events, _ = _run(
        _serve(
            _sse(
                _text_chunk("Qdrant "),
                _text_chunk("owns fusion."),
                _finish_chunk("stop"),
                _usage_chunk(
                    prompt_tokens=118,
                    completion_tokens=24,
                    prompt_cache_hit_tokens=64,
                ),
            )
        )
    )

    deltas = [event.text for event in events if isinstance(event, ModelTextDelta)]
    usage = next(event for event in events if isinstance(event, ModelUsageReported))
    completion = _completion(events)

    assert deltas == ["Qdrant ", "owns fusion."]
    assert usage.usage.input_tokens == 118
    assert usage.usage.output_tokens == 24
    assert usage.usage.cache_read_tokens == 64
    assert completion.finish_reason == "stop"
    assert completion.error is None


def test_a_tool_call_split_across_chunks_arrives_whole() -> None:
    """Partial JSON must never reach schema validation or policy."""

    events, _ = _run(
        _serve(
            _sse(
                _tool_fragment(
                    call_id="call_1", name="read_document", arguments='{"do'
                ),
                _tool_fragment(arguments='cument_id": "d'),
                _tool_fragment(arguments='oc_1"}'),
                _finish_chunk("tool_calls"),
                _usage_chunk(prompt_tokens=10, completion_tokens=5),
            )
        )
    )

    proposals = [
        event.call for event in events if isinstance(event, ModelToolCallProposed)
    ]

    assert len(proposals) == 1
    assert proposals[0].tool_call_id == "call_1"
    assert proposals[0].tool_name == "read_document"
    assert proposals[0].arguments == {"document_id": "doc_1"}
    assert _completion(events).finish_reason == "tool_use"


def test_two_tool_calls_keep_the_order_the_provider_indexed_them_in() -> None:
    events, _ = _run(
        _serve(
            _sse(
                _tool_fragment(
                    1, call_id="call_b", name="read_document", arguments="{}"
                ),
                _tool_fragment(
                    0, call_id="call_a", name="read_document", arguments="{}"
                ),
                _finish_chunk("tool_calls"),
            )
        )
    )

    proposals = [
        event.call.tool_call_id
        for event in events
        if isinstance(event, ModelToolCallProposed)
    ]

    assert proposals == ["call_a", "call_b"]


def test_a_tool_call_without_arguments_is_an_empty_object() -> None:
    events, _ = _run(
        _serve(
            _sse(
                _tool_fragment(call_id="call_1", name="read_document"),
                _finish_chunk("tool_calls"),
            )
        )
    )

    proposal = next(
        event for event in events if isinstance(event, ModelToolCallProposed)
    )

    assert proposal.call.arguments == {}


def test_unparsable_tool_arguments_fail_the_stream() -> None:
    """Guessing would put something the model never asked for before a handler."""

    events, _ = _run(
        _serve(
            _sse(
                _tool_fragment(
                    call_id="call_1",
                    name="read_document",
                    arguments='{"document_id": ',
                ),
                _finish_chunk("tool_calls"),
            )
        )
    )
    completion = _completion(events)

    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert "unparsable arguments" in completion.error.message
    assert not any(isinstance(event, ModelToolCallProposed) for event in events)


def test_a_tool_call_without_an_id_fails_the_stream() -> None:
    events, _ = _run(
        _serve(
            _sse(
                _tool_fragment(name="read_document", arguments="{}"),
                _finish_chunk("tool_calls"),
            )
        )
    )
    completion = _completion(events)

    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert "without an id or name" in completion.error.message


def test_an_http_error_reports_its_status_and_nothing_else() -> None:
    """A chat completion error body can echo the prompt that was sent."""

    secret_body = (
        b'{"error":{"message":"invalid api key sk-deepseek-canary-must-not-leak"}}'
    )
    events, _ = _run(_serve(secret_body, status=401))
    completion = _completion(events)

    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert completion.error.code == "provider_error"
    assert "401" in completion.error.message
    assert "canary" not in completion.error.message
    assert completion.error.retryable is False


def test_server_errors_and_rate_limits_are_retryable() -> None:
    for status, retryable in ((500, True), (429, True), (400, False)):
        events, _ = _run(_serve(b"{}", status=status))
        completion = _completion(events)

        assert completion.error is not None
        assert completion.error.retryable is retryable


def test_a_transport_failure_becomes_a_completion_event() -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    events, _ = _run(responder)
    completion = _completion(events)

    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert "ConnectError" in completion.error.message
    assert completion.error.retryable is True


def test_a_stream_without_a_finish_reason_is_an_error() -> None:
    events, _ = _run(_serve(_sse(_text_chunk("half an answer"))))
    completion = _completion(events)

    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert "without a finish reason" in completion.error.message


def test_an_unsupported_finish_reason_is_reported_rather_than_guessed() -> None:
    events, _ = _run(
        _serve(_sse(_text_chunk("blocked"), _finish_chunk("content_filter")))
    )
    completion = _completion(events)

    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert "content_filter" in completion.error.message


def test_comments_and_blank_lines_are_skipped() -> None:
    """Framing that carries no data is not data, and never was."""

    body = b": keep-alive comment\n\n\n" + _sse(
        _text_chunk("still here"), _finish_chunk("stop")
    )

    events, _ = _run(_serve(body))

    assert [event.text for event in events if isinstance(event, ModelTextDelta)] == [
        "still here"
    ]
    assert _completion(events).finish_reason == "stop"


def test_an_unreadable_data_frame_ends_the_stream() -> None:
    """P1-9. This test replaced one asserting the opposite.

    The old ``test_unreadable_frames_are_skipped_rather_than_fatal`` wrote the
    defect down as the intended behaviour, and passed for it. Skipping a frame
    is only harmless if frames are independent, and tool-argument fragments are
    not: see the test below.
    """

    body = b"data: {not json}\n\n" + _sse(
        _text_chunk("still here"), _finish_chunk("stop")
    )

    events, _ = _run(_serve(body))
    completion = _completion(events)

    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert completion.error.code == "provider_error"


def test_a_frame_that_is_not_an_object_ends_the_stream() -> None:
    """Valid JSON is not the same as a chunk."""

    body = b'data: ["not", "a", "chunk"]\n\n' + _sse(_finish_chunk("stop"))

    completion = _completion(_run(_serve(body))[0])

    assert completion.finish_reason == "error"


def test_a_dropped_fragment_cannot_become_a_different_tool_call() -> None:
    """The reason skipping is not harmless.

    Tool arguments arrive as fragments and are concatenated. Drop one from the
    middle and the rest can still form a perfectly valid JSON object -- a call
    the model never made, with arguments nobody chose, proposed as though it
    had. Reproduced before the fix: the handler was offered
    ``{"document_id": "doc_SAFE"}``.
    """

    body = (
        _sse(
            _tool_fragment(
                index=0,
                call_id="call_1",
                name="read_document",
                arguments='{"document_id": "doc_SAFE',
            ),
            done=False,
        )
        + b'data: {"choices": [BROKEN\n\n'
        + _sse(
            _tool_fragment(index=0, arguments='_BUT_TRUNCATED"}'),
            _finish_chunk("tool_calls"),
        )
    )

    events, _ = _run(_serve(body))

    assert [event for event in events if isinstance(event, ModelToolCallProposed)] == []
    assert _completion(events).finish_reason == "error"


def test_an_over_long_text_delta_is_reported_not_raised() -> None:
    """A provider's limits are not this process's contract.

    ``BoundedText`` caps a delta at 4096 characters, and constructing one over
    that raised a Pydantic ``ValidationError`` straight out of ``ModelPort``.
    """

    body = _sse(_text_chunk("x" * 5000), _finish_chunk("stop"))

    completion = _completion(_run(_serve(body))[0])

    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert completion.error.code == "provider_error"


def test_accumulated_tool_arguments_are_bounded() -> None:
    """The one thing here that grows with whatever the provider sends."""

    body = _sse(
        _tool_fragment(
            index=0, call_id="call_1", name="read_document", arguments="x" * 400
        ),
        _finish_chunk("tool_calls"),
    )

    events, _ = _run_bounded(_serve(body), max_argument_chars=64)
    completion = _completion(events)

    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert "ceiling" in completion.error.message


def test_arguments_inside_the_ceiling_still_assemble() -> None:
    """The control: the bound is a bound, not a refusal to accept tool calls."""

    body = _sse(
        _tool_fragment(
            index=0,
            call_id="call_1",
            name="read_document",
            arguments='{"document_id": "doc_1"}',
        ),
        _finish_chunk("tool_calls"),
    )

    events, _ = _run_bounded(_serve(body), max_argument_chars=64)
    proposed = [event for event in events if isinstance(event, ModelToolCallProposed)]

    assert len(proposed) == 1
    assert proposed[0].call.arguments == {"document_id": "doc_1"}


def test_an_unknown_profile_fails_without_calling_the_provider() -> None:
    events, wire = _run(
        _serve(_sse(_finish_chunk("stop"))),
        ModelRequest(model_profile="compact", messages=(user_message("hi"),)),
        profiles={"main": PROFILES["main"]},
    )
    completion = _completion(events)

    assert wire.requests == []
    assert completion.error is not None
    assert "no model configured for profile compact" in completion.error.message


def test_the_request_carries_the_key_the_model_and_usage_accounting() -> None:
    _, wire = _run(_serve(_sse(_finish_chunk("stop"))))
    request = wire.requests[0]

    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert wire.payload["model"] == "deepseek-chat"
    assert wire.payload["stream"] is True
    assert wire.payload["stream_options"] == {"include_usage": True}
    assert "tools" not in wire.payload


def test_the_system_prompt_leads_the_conversation() -> None:
    _, wire = _run(
        _serve(_sse(_finish_chunk("stop"))),
        ModelRequest(
            system_prompt="Answer from the corpus only.",
            messages=(user_message("Who owns fusion?"),),
        ),
    )

    assert wire.payload["messages"] == [
        {"role": "system", "content": "Answer from the corpus only."},
        {"role": "user", "content": "Who owns fusion?"},
    ]


def test_a_tool_round_trip_keeps_the_provider_call_id() -> None:
    call = ToolCall(
        tool_call_id="call_1",
        tool_name="read_document",
        arguments={"document_id": "doc_1"},
    )
    _, wire = _run(
        _serve(_sse(_finish_chunk("stop"))),
        ModelRequest(
            messages=(
                user_message("Read doc_1"),
                assistant_message(text="Reading.", tool_calls=(call,)),
                tool_message((ToolResult.succeeded(call, content="the text"),)),
            ),
            tools=(SEARCH_SPEC,),
        ),
    )
    messages = wire.payload["messages"]

    assert messages[1]["tool_calls"][0]["id"] == "call_1"
    assert json.loads(messages[1]["tool_calls"][0]["function"]["arguments"]) == {
        "document_id": "doc_1"
    }
    assert messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "the text",
    }


def test_tool_specifications_are_sent_as_function_definitions() -> None:
    _, wire = _run(
        _serve(_sse(_finish_chunk("stop"))),
        ModelRequest(messages=(user_message("hi"),), tools=(SEARCH_SPEC,)),
    )
    tools = wire.payload["tools"]

    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "read_document",
                "description": "Return one document.",
                "parameters": SEARCH_SPEC.input_schema,
            },
        }
    ]


def test_the_profile_decides_the_output_ceiling() -> None:
    _, wire = _run(
        _serve(_sse(_finish_chunk("stop"))),
        ModelRequest(model_profile="compact", messages=(user_message("hi"),)),
    )

    assert wire.payload["max_tokens"] == 512


def test_a_request_may_lower_the_output_ceiling() -> None:
    _, wire = _run(
        _serve(_sse(_finish_chunk("stop"))),
        ModelRequest(
            model_profile="compact",
            messages=(user_message("hi"),),
            max_output_tokens=64,
        ),
    )

    assert wire.payload["max_tokens"] == 64


def test_the_adapter_satisfies_the_model_port() -> None:
    async def scenario() -> bool:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_serve(b""))
        ) as client:
            model: ModelPort = DeepSeekModel(
                client=client,
                api_key=API_KEY,
                base_url=BASE_URL,
                profiles=PROFILES,
            )
            return isinstance(model, ModelPort)

    assert asyncio.run(scenario()) is True


def test_the_runtime_cannot_tell_this_adapter_from_the_scripted_one() -> None:
    """The whole point of the port, asserted end to end.

    The same run over a real HTTP adapter produces the same durable timeline
    the scripted model produces in the runtime suite.
    """

    turns = [
        _sse(
            _text_chunk("Let me look."),
            _tool_fragment(
                call_id="call_1",
                name="read_document",
                arguments='{"document_id": "doc_1"}',
            ),
            _finish_chunk("tool_calls"),
            _usage_chunk(prompt_tokens=100, completion_tokens=20),
        ),
        _sse(
            _text_chunk("Qdrant owns fusion."),
            _finish_chunk("stop"),
            _usage_chunk(prompt_tokens=204, completion_tokens=24),
        ),
    ]
    served: list[bytes] = []
    payloads: list[dict[str, Any]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        body = turns[len(served)]
        served.append(body)
        payload = json.loads(request.content)
        assert isinstance(payload, dict)
        payloads.append(payload)
        return httpx.Response(200, content=body)

    registry = StaticToolRegistry(
        [read_document_tool({"doc_1": "Qdrant performs one fusion per query."})]
    )
    scope = EventScope(stream_id="stream_1", run_id="run_1")

    async def scenario() -> tuple[AgentOutcome, list[str]]:
        log = InMemoryEventLog()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(responder)
        ) as client:
            runtime = ClaudeLikeAgentRuntime(
                model=DeepSeekModel(
                    client=client,
                    api_key=API_KEY,
                    base_url=BASE_URL,
                    profiles={
                        "main": DeepSeekProfile(
                            model_id="deepseek-chat",
                            tool_calling_required=True,
                        )
                    },
                ),
                gateway=ToolGateway(
                    registry=registry,
                    policy=EnvelopePolicyEngine(registry=registry),
                ),
                policy_identity="policy-test:0000000000000000",
            )
            outcome = await runtime.run(
                AgentRunRequest(
                    trace=TraceContext(agent_run_id="run_1"),
                    run_kind="chat",
                    stream_id="stream_1",
                    principal=PrincipalContext(
                        principal_id="user_1",
                        tenant_id="tenant_a",
                    ),
                    envelope=AuthorizationEnvelope(allowed_tools=("read_document",)),
                    budget=RunBudget(max_steps=4, max_tool_calls=8),
                    messages=(user_message("Who owns hybrid fusion?"),),
                    tool_names=("read_document",),
                ),
                ScopedEventSink(log=log, scope=scope),
                CancellationSource(),
            )
            replayed = await log.read(scope.stream_id)
            return outcome, [envelope.event_type for envelope in replayed]

    outcome, timeline = asyncio.run(scenario())

    assert outcome.status == "completed"
    assert outcome.output_text == "Qdrant owns fusion."
    assert outcome.usage.steps == 2
    assert outcome.usage.tool_calls == 1
    assert outcome.usage.tokens.input_tokens == 304
    assert payloads[0]["tool_choice"] == "required"
    assert "tools" in payloads[1]
    assert "tool_choice" not in payloads[1]
    assert timeline == [
        "RunStarted",
        "ModelStarted",
        "ModelCompleted",
        "ToolProposed",
        "PermissionResolved",
        "ToolStarted",
        "ToolCompleted",
        "ModelStarted",
        "ModelCompleted",
        "RunCompleted",
    ]


# --- P2-1: reliability settings the adapter had no consumer for ---------------


def _run_with(
    responder: Callable[[httpx.Request], httpx.Response],
    *,
    profile: DeepSeekProfile,
    request: ModelRequest | None = None,
) -> tuple[list[ModelEvent], _Wire, list[float]]:
    """Run one stream, recording the backoff delays instead of sleeping them."""

    wire = _Wire(responder)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    async def scenario() -> list[ModelEvent]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
            model = DeepSeekModel(
                client=client,
                api_key=API_KEY,
                base_url=BASE_URL,
                profiles={"main": profile},
                sleep=sleep,
            )
            return [
                event
                async for event in model.stream(
                    request
                    if request is not None
                    else ModelRequest(messages=(user_message("hi"),))
                )
            ]

    return asyncio.run(scenario()), wire, slept


def test_the_profile_timeout_reaches_the_request() -> None:
    """It was configurable and ignored, which is worse than not offering it."""

    events, wire, _ = _run_with(
        _serve(_sse(_finish_chunk("stop"))),
        profile=DeepSeekProfile(model_id="deepseek-chat", timeout_seconds=7.5),
    )

    assert _completion(events).finish_reason == "stop"
    assert wire.requests[0].extensions["timeout"]["read"] == 7.5


def test_tool_calling_required_is_sent_with_the_tools() -> None:
    _, wire, _ = _run_with(
        _serve(_sse(_finish_chunk("stop"))),
        profile=DeepSeekProfile(model_id="deepseek-chat", tool_calling_required=True),
        request=ModelRequest(messages=(user_message("hi"),), tools=(SEARCH_SPEC,)),
    )

    assert wire.payload["tool_choice"] == "required"


def test_tool_calling_required_is_omitted_when_there_are_no_tools() -> None:
    """Requiring a choice from an empty set is not a request anyone can serve."""

    _, wire, _ = _run_with(
        _serve(_sse(_finish_chunk("stop"))),
        profile=DeepSeekProfile(model_id="deepseek-chat", tool_calling_required=True),
    )

    assert "tool_choice" not in wire.payload


def test_a_tool_result_returns_required_mode_to_auto() -> None:
    call = ToolCall(
        tool_call_id="call_1",
        tool_name="read_document",
        arguments={"document_id": "doc_1"},
    )
    result = ToolResult(
        tool_call_id="call_1",
        tool_name="read_document",
        status="ok",
        content="document text",
    )

    _, wire, _ = _run_with(
        _serve(_sse(_finish_chunk("stop"))),
        profile=DeepSeekProfile(model_id="deepseek-chat", tool_calling_required=True),
        request=ModelRequest(
            messages=(
                user_message("read it"),
                assistant_message(tool_calls=(call,)),
                tool_message((result,)),
            ),
            tools=(SEARCH_SPEC,),
        ),
    )

    assert "tools" in wire.payload
    assert "tool_choice" not in wire.payload


def test_a_retryable_status_is_retried_up_to_the_configured_count() -> None:
    attempts: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) <= 2:
            return httpx.Response(503, content=b"")
        return httpx.Response(200, content=_sse(_finish_chunk("stop")))

    events, _, slept = _run_with(
        responder, profile=DeepSeekProfile(model_id="deepseek-chat", max_retries=2)
    )

    assert len(attempts) == 3
    assert _completion(events).finish_reason == "stop"
    # Doubling, so a rate limit is not asked about harder each time.
    assert slept == [0.5, 1.0]


def test_a_non_retryable_status_is_not_retried() -> None:
    """A 400 means the request was wrong, and it will be wrong again."""

    attempts: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, content=b"")

    events, _, _ = _run_with(
        responder, profile=DeepSeekProfile(model_id="deepseek-chat", max_retries=3)
    )

    assert len(attempts) == 1
    assert _completion(events).finish_reason == "error"


class _CutStream(httpx.AsyncByteStream):
    """A response body that delivers some bytes and then drops the connection.

    A real mid-stream transport fault, which is the only thing that reaches
    the guard being tested. A merely malformed body ends the stream for a
    different reason and would leave that guard unexercised.
    """

    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._prefix
        raise httpx.ReadError("the connection dropped mid-stream")


def test_a_failure_after_the_first_event_is_never_retried() -> None:
    """The whole reason retrying a stream needs a rule.

    Bytes the caller has already seen cannot be un-seen, so a second attempt
    would repeat them. Only a failure from before the first event is safe to
    retry.
    """

    attempts: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(
            200, stream=_CutStream(_sse(_text_chunk("half an answer"), done=False))
        )

    events, _, _ = _run_with(
        responder, profile=DeepSeekProfile(model_id="deepseek-chat", max_retries=3)
    )
    deltas = [event for event in events if isinstance(event, ModelTextDelta)]

    assert len(attempts) == 1
    assert [delta.text for delta in deltas] == ["half an answer"]
    assert _completion(events).finish_reason == "error"


def test_exhausting_the_retries_reports_the_last_failure() -> None:
    attempts: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, content=b"")

    events, _, slept = _run_with(
        responder, profile=DeepSeekProfile(model_id="deepseek-chat", max_retries=1)
    )
    completion = _completion(events)

    assert len(attempts) == 2
    assert len(slept) == 1
    assert completion.finish_reason == "error"
    assert completion.error is not None
    assert "503" in completion.error.message


def test_the_default_profile_does_not_retry() -> None:
    """The control: retrying is what a deployment asks for, not the default."""

    attempts: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, content=b"")

    _run_with(responder, profile=DeepSeekProfile(model_id="deepseek-chat"))

    assert len(attempts) == 1


def test_an_impossible_profile_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        DeepSeekProfile(model_id="deepseek-chat", timeout_seconds=0)
    with pytest.raises(ValueError, match="max_retries"):
        DeepSeekProfile(model_id="deepseek-chat", max_retries=-1)
