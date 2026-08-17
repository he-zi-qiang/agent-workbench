"""The serial loop, and the invariants that must survive every path through it.

The happy path is one test. The rest of this file is about what happens when
something refuses to cooperate -- an unknown tool, a denied call, a handler
that raises, a model that never stops asking for tools -- because the loop's
job is to keep the conversation answerable in exactly those cases.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from agent_workbench.adapters.events import ObservingEventSink, ScopedEventSink
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry, read_document_tool
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.events import (
    EventEnvelope,
    ModelStarted,
    RunFailed,
    ToolFailed,
    ToolProposed,
)
from agent_workbench.domain.messages import ToolResultBlock, user_message
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.pricing import ModelPrices
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    RunBudget,
    TokenUsage,
    TraceContext,
)
from agent_workbench.domain.schema import ANSWER_TEXT_LIMIT, BOUNDED_TEXT_LIMIT
from agent_workbench.domain.tools import ToolCall, ToolName, ToolResult, ToolSpec
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.cancellation import CancellationSource
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.model import (
    ModelEvent,
    ModelPort,
    ModelRequest,
    ModelStreamCompleted,
    ModelUsageReported,
)
from agent_workbench.ports.model import ModelTextDelta as TextDelta
from agent_workbench.ports.tools import ToolBinding, ToolInvocation
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolExecutor, ToolGateway
from agent_workbench.runtime.agent_runtime import render_prompt
from agent_workbench.runtime.tool_gateway import PreparedCall

CLOCK = datetime(2026, 7, 25, 3, 14, 15, tzinfo=UTC)
SCOPE = EventScope(stream_id="stream_1", run_id="run_1")
CORPUS = {"doc_1": "Qdrant performs one dense and sparse fusion per query."}
USAGE = TokenUsage(input_tokens=100, output_tokens=20)
POLICY_IDENTITY = "policy-test:0000000000000000"

#: DeepSeek's published rates at the time of writing, in micro-USD per million
#: tokens. Real numbers rather than round ones, so a test that happened to pass
#: on 1-per-million arithmetic does not read as evidence.
PRICES = ModelPrices(
    input_micro_usd_per_mtok=270_000,
    output_micro_usd_per_mtok=1_100_000,
    cache_read_micro_usd_per_mtok=27_000,
    cache_write_micro_usd_per_mtok=0,
)

READ_CALL = ToolCall(
    tool_call_id="toolu_01A09q90qw90lq917835lq9",
    tool_name="read_document",
    arguments={"document_id": "doc_1"},
)


def _clock() -> datetime:
    return CLOCK


def _ids(prefix: str) -> Callable[[], str]:
    counter = count(1)

    def next_id() -> str:
        return f"{prefix}_{next(counter)}"

    return next_id


def _ticking(step: float = 0.004) -> Callable[[], float]:
    counter = count()

    def reading() -> float:
        return next(counter) * step

    return reading


def _spec(name: str, *, risk: str = "read") -> ToolSpec:
    if risk == "read":
        return ToolSpec(
            name=name,
            description="A tool used by the runtime tests.",
            input_schema={"type": "object"},
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=5,
        )
    return ToolSpec(
        name=name,
        description="A tool with an effect.",
        input_schema={"type": "object"},
        concurrency="exclusive",
        risk="write",
        idempotency="keyed",
        timeout_seconds=5,
        permission_scopes=("artifact:write",),
    )


class _Recorder:
    """A tool that records every call it receives."""

    def __init__(
        self,
        name: str,
        *,
        content: str = "ok",
        risk: str = "read",
        on_call: Callable[[], None] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.name = name
        self.calls: list[ToolCall] = []
        self._content = content
        self._on_call = on_call
        self._raises = raises
        self.binding = ToolBinding(spec=_spec(name, risk=risk), handler=self._handler)

    async def _handler(self, invocation: ToolInvocation) -> ToolResult:
        self.calls.append(invocation.call)
        if self._on_call is not None:
            self._on_call()
        if self._raises is not None:
            raise self._raises
        return ToolResult.succeeded(invocation.call, content=self._content)


class _Execution:
    def __init__(
        self,
        outcome: AgentOutcome,
        live: list[EventEnvelope],
        durable: tuple[EventEnvelope, ...],
        model: FakeModel | ModelPort,
    ) -> None:
        self.outcome = outcome
        self.live = live
        self.durable = durable
        self.model = model

    @property
    def durable_types(self) -> list[str]:
        return [envelope.event_type for envelope in self.durable]

    @property
    def live_types(self) -> list[str]:
        return [envelope.event_type for envelope in self.live]


def _payloads[PayloadT](run: _Execution, kind: type[PayloadT]) -> list[PayloadT]:
    """Every durable payload of one type, typed rather than indexed.

    ``envelope.payload`` is a union of domain models, so reading a field off it
    needs the narrowing this does once instead of at every assertion.
    """

    return [e.payload for e in run.durable if isinstance(e.payload, kind)]


def _request(
    *,
    budget: RunBudget | None = None,
    tool_names: Sequence[ToolName] = ("read_document",),
    allowed_tools: Sequence[ToolName] | None = None,
    max_tool_risk: str = "read",
    approval_required_risks: Sequence[str] = ("write", "external", "destructive"),
) -> AgentRunRequest:
    permitted = tuple(tool_names) if allowed_tools is None else tuple(allowed_tools)
    return AgentRunRequest.model_validate(
        {
            "trace": TraceContext(agent_run_id="run_1"),
            "run_kind": "chat",
            "stream_id": "stream_1",
            "principal": PrincipalContext(
                principal_id="user_1",
                tenant_id="tenant_a",
                scopes=("artifact:write",),
            ),
            "envelope": AuthorizationEnvelope(
                allowed_tools=permitted,
                max_tool_risk=max_tool_risk,
                approval_required_risks=tuple(approval_required_risks),
            ),
            "budget": budget
            if budget is not None
            else RunBudget(max_steps=6, max_tool_calls=12),
            "messages": (user_message("Who owns hybrid fusion?"),),
            "tool_names": tuple(tool_names),
        }
    )


def _execute(
    model: ModelPort,
    *,
    request: AgentRunRequest | None = None,
    bindings: Sequence[ToolBinding] | None = None,
    cancellation: CancellationSource | None = None,
    model_timeout_seconds: float | None = None,
    max_parallel_read_tools: int = 4,
    record_step_inputs: bool = False,
    prices: ModelPrices | None = None,
    approvals: object | None = None,
    approval_timeout_seconds: float = 100.0,
) -> _Execution:
    registry = StaticToolRegistry(
        bindings if bindings is not None else [read_document_tool(CORPUS)]
    )

    async def scenario() -> _Execution:
        log = InMemoryEventLog(clock=_clock, event_ids=_ids("evt"))
        live: list[EventEnvelope] = []
        sink = ObservingEventSink(
            inner=ScopedEventSink(log=log, scope=SCOPE),
            observer=live.append,
        )
        runtime = ClaudeLikeAgentRuntime(
            model=model,
            gateway=ToolGateway(
                registry=registry,
                policy=EnvelopePolicyEngine(registry=registry),
                executor=ToolExecutor(monotonic=_ticking()),
                record_step_inputs=record_step_inputs,
                approvals=approvals,  # pyright: ignore[reportArgumentType]
                approval_timeout_seconds=approval_timeout_seconds,
            ),
            policy_identity=POLICY_IDENTITY,
            model_timeout_seconds=model_timeout_seconds,
            max_parallel_read_tools=max_parallel_read_tools,
            clock=_clock,
            model_call_ids=_ids("mc"),
            record_step_inputs=record_step_inputs,
            prices=prices,
        )
        outcome = await runtime.run(
            request if request is not None else _request(),
            sink,
            cancellation if cancellation is not None else CancellationSource(),
        )
        return _Execution(outcome, live, await log.read(SCOPE.stream_id), model)

    return asyncio.run(scenario())


def _tool_round(call: ToolCall = READ_CALL) -> FakeModel:
    return FakeModel(
        [
            ScriptedTurn(text="Let me look.", tool_calls=(call,), usage=USAGE),
            ScriptedTurn(text="Qdrant owns fusion.", usage=USAGE),
        ]
    )


class _RaisingModel:
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        raise RuntimeError("connection reset: sk-ant-canary-must-not-leak")
        yield TextDelta(text="unreachable")  # pragma: no cover


class _TruncatedModel:
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        yield TextDelta(text="half a sentence")


def test_the_runtime_satisfies_the_agent_executor_protocol() -> None:
    registry = StaticToolRegistry([])
    runtime: AgentExecutor = ClaudeLikeAgentRuntime(
        model=FakeModel(()),
        gateway=ToolGateway(
            registry=registry,
            policy=EnvelopePolicyEngine(registry=registry),
        ),
        policy_identity=POLICY_IDENTITY,
    )

    assert isinstance(runtime, AgentExecutor)


def test_a_tool_round_completes_the_loop() -> None:
    """Input, model, tool, result, model, answer."""

    run = _execute(_tool_round())

    assert run.outcome.status == "completed"
    assert run.outcome.output_text == "Qdrant owns fusion."
    assert run.outcome.usage.steps == 2
    assert run.outcome.usage.tool_calls == 1


def test_a_run_records_no_prompt_or_arguments_unless_asked_to() -> None:
    """ADR-019's default. The digest and the byte count are emitted regardless."""

    run = _execute(_tool_round())

    started = _payloads(run, ModelStarted)
    proposed = _payloads(run, ToolProposed)
    assert started and proposed
    assert all(payload.prompt_preview == "" for payload in started)
    assert all(payload.argument_preview == "" for payload in proposed)
    # Without these the reader would have nothing at all, which is not the
    # default this ADR chose.
    assert all(payload.argument_sha256 for payload in proposed)
    assert all(payload.argument_bytes for payload in proposed)


def test_recording_step_inputs_puts_the_prompt_and_the_call_on_the_timeline() -> None:
    run = _execute(_tool_round(), record_step_inputs=True)

    started = _payloads(run, ModelStarted)
    proposed = _payloads(run, ToolProposed)

    assert "Who owns hybrid fusion?" in started[0].prompt_preview

    # The second call has to show the tool result, because that -- not the
    # original question -- is what the model was actually looking at when it
    # produced the answer.
    assert "read_document" in started[1].prompt_preview

    assert "document_id" in proposed[0].argument_preview


def test_a_recorded_prompt_is_cut_to_what_the_event_field_can_hold() -> None:
    """An over-long prompt must truncate, not make the event unconstructable."""

    huge = "x" * (BOUNDED_TEXT_LIMIT * 3)
    rendered = render_prompt(huge, ())

    assert len(rendered) == BOUNDED_TEXT_LIMIT
    assert rendered.endswith("…")


def test_the_durable_timeline_records_the_whole_round() -> None:
    run = _execute(_tool_round())

    assert run.durable_types == [
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
    assert "ModelDelta" in run.live_types
    assert "ModelDelta" not in run.durable_types
    assert "ContextCompacted" not in run.durable_types


def test_reasoning_streams_live_and_is_excerpted_into_the_durable_record() -> None:
    """The two halves of ADR-061's double track.

    The chain streams as transient deltas, and what survives in the log is a
    bounded excerpt on the event that closes the call -- which is the only
    thing a Task timeline, having no live channel at all, can ever show.
    """

    run = _execute(
        FakeModel([ScriptedTurn(reasoning="Fusion lives in Qdrant.", text="Qdrant.")])
    )
    completed = next(
        envelope.payload
        for envelope in run.durable
        if envelope.event_type == "ModelCompleted"
    )

    assert "ModelThinkingDelta" in run.live_types
    assert "ModelThinkingDelta" not in run.durable_types
    assert completed.thinking_preview == "Fusion lives in Qdrant."
    assert completed.text == "Qdrant."


def test_reasoning_never_re_enters_the_conversation_the_next_turn_is_built_from() -> (
    None
):
    """The provider requires it withheld, and the ledger is where it would leak.

    A second turn exists here precisely so the first turn's reasoning has
    somewhere to leak *into*: the assistant message the runtime writes back.
    """

    model = FakeModel(
        [
            ScriptedTurn(
                reasoning="I should read the corpus first.",
                text="Let me look.",
                tool_calls=(READ_CALL,),
                usage=USAGE,
            ),
            ScriptedTurn(text="Qdrant owns fusion.", usage=USAGE),
        ]
    )
    run = _execute(model)
    replayed = run.model
    assert isinstance(replayed, FakeModel)
    second = replayed.requests[1]
    serialized = "\n".join(message.model_dump_json() for message in second.messages)

    assert run.outcome.status == "completed"
    assert "I should read the corpus first." not in serialized


def test_the_tool_result_reaches_the_next_model_request() -> None:
    run = _execute(_tool_round())
    model = run.model
    assert isinstance(model, FakeModel)
    tool_turn = model.requests[1].messages[-1]
    block = tool_turn.content[0]

    assert tool_turn.role == "tool"
    assert isinstance(block, ToolResultBlock)
    assert block.tool_call_id == READ_CALL.tool_call_id
    assert block.text == CORPUS["doc_1"]


def test_an_unknown_tool_is_answered_and_the_loop_continues() -> None:
    call = ToolCall(tool_call_id="toolu_1", tool_name="not_registered")
    recorder = _Recorder("read_document")

    run = _execute(_tool_round(call), bindings=[recorder.binding])

    assert recorder.calls == []
    assert run.outcome.status == "completed"
    assert run.durable_types == [
        "RunStarted",
        "ModelStarted",
        "ModelCompleted",
        "ToolProposed",
        "ToolFailed",
        "ModelStarted",
        "ModelCompleted",
        "RunCompleted",
    ]


def test_a_denied_call_never_reaches_its_handler() -> None:
    recorder = _Recorder("read_document")

    run = _execute(
        _tool_round(),
        request=_request(allowed_tools=()),
        bindings=[recorder.binding],
    )

    assert recorder.calls == []
    assert "ToolStarted" not in run.durable_types
    assert run.durable_types.count("PermissionResolved") == 1
    assert run.outcome.status == "completed"


def test_a_tool_above_the_risk_ceiling_is_denied() -> None:
    """A tool inside the allowlist can still sit outside the risk ceiling."""

    recorder = _Recorder("export_artifact", risk="write")
    call = ToolCall(tool_call_id="toolu_1", tool_name="export_artifact")

    run = _execute(
        _tool_round(call),
        request=_request(
            tool_names=("export_artifact",),
            allowed_tools=("export_artifact",),
            max_tool_risk="read",
        ),
        bindings=[recorder.binding],
    )

    assert recorder.calls == []
    assert run.outcome.status == "completed"


def test_a_raising_handler_is_answered_without_leaking_its_message() -> None:
    recorder = _Recorder(
        "read_document",
        raises=RuntimeError("token sk-ant-canary-must-not-leak expired"),
    )

    run = _execute(_tool_round(), bindings=[recorder.binding])

    assert run.outcome.status == "completed"
    assert "ToolFailed" in run.durable_types
    assert "sk-ant-canary" not in str(run.durable)


def test_every_proposed_call_is_answered_exactly_once() -> None:
    calls = (
        ToolCall(tool_call_id="toolu_1", tool_name="read_document"),
        ToolCall(tool_call_id="toolu_2", tool_name="not_registered"),
        ToolCall(tool_call_id="toolu_3", tool_name="read_document"),
    )
    model = FakeModel(
        [
            ScriptedTurn(text="Working.", tool_calls=calls, usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(model)
    fake = run.model
    assert isinstance(fake, FakeModel)
    tool_turn = fake.requests[1].messages[-1]
    answered = [
        block.tool_call_id
        for block in tool_turn.content
        if isinstance(block, ToolResultBlock)
    ]

    assert answered == ["toolu_1", "toolu_2", "toolu_3"]
    assert run.outcome.usage.tool_calls == 3


def test_a_model_that_never_stops_asking_is_stopped_by_the_step_ceiling() -> None:
    model = FakeModel(
        [ScriptedTurn(text="again", tool_calls=(READ_CALL,), usage=USAGE)],
        repeat_last=True,
    )

    run = _execute(
        model, request=_request(budget=RunBudget(max_steps=3, max_tool_calls=99))
    )

    assert run.outcome.status == "failed"
    assert run.outcome.stop_reason == "max_steps"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "budget_exceeded"
    assert run.outcome.usage.steps == 3


def test_the_tool_call_ceiling_stops_a_model_that_will_not_take_the_hint() -> None:
    """The tool ceiling still binds when the model keeps proposing anyway.

    The allowance running out takes the tools off the request rather than
    ending the run, so the model gets one more turn to answer from what it
    already fetched. This scripted model refuses that turn -- it proposes two
    more calls with nothing advertised, which a real model does not do and this
    fake does because it never reads `request.tools`. The proposal is turned
    away and the run fails, which is the right end for a model that will not
    answer.

    Note `steps == 3`, one more than before this behaviour existed: the third
    step is the answering turn being offered and declined. The tool ledger is
    unmoved at 4, which is the part that was ever a guarantee.
    """

    pair = (
        ToolCall(tool_call_id="toolu_1", tool_name="read_document"),
        ToolCall(tool_call_id="toolu_2", tool_name="read_document"),
    )
    model = FakeModel(
        [ScriptedTurn(text="again", tool_calls=pair, usage=USAGE)],
        repeat_last=True,
    )

    run = _execute(
        model,
        request=_request(budget=RunBudget(max_steps=4, max_tool_calls=4)),
    )

    assert run.outcome.stop_reason == "max_tool_calls"
    assert run.outcome.usage.steps == 3
    assert run.outcome.usage.tool_calls == 4
    # The turn it was offered came with nothing to call.
    assert model.requests[-1].tools == ()


def test_an_already_cancelled_run_never_calls_the_model() -> None:
    model = _tool_round()
    cancellation = CancellationSource()
    cancellation.cancel("operator stopped the task")

    run = _execute(model, cancellation=cancellation)

    assert isinstance(model, FakeModel)
    assert model.call_count == 0
    assert run.outcome.status == "cancelled"
    assert run.durable_types == ["RunStarted", "RunCancelled"]


def test_cancellation_between_groups_still_answers_every_call() -> None:
    """The ids were already shown to the model; the run owes them answers."""

    cancellation = CancellationSource()
    first = _Recorder("read_document", on_call=lambda: cancellation.cancel("stopped"))
    second = _Recorder("text_statistics")
    calls = (
        ToolCall(tool_call_id="toolu_1", tool_name="read_document"),
        ToolCall(tool_call_id="toolu_2", tool_name="text_statistics"),
    )
    model = FakeModel(
        [ScriptedTurn(text="Working.", tool_calls=calls, usage=USAGE)],
    )

    run = _execute(
        model,
        request=_request(tool_names=("read_document", "text_statistics")),
        bindings=[first.binding, second.binding],
        cancellation=cancellation,
        # One call per group, so the cancel lands between them.
        max_parallel_read_tools=1,
    )

    assert len(first.calls) == 1
    assert second.calls == []
    assert run.outcome.status == "cancelled"
    assert run.durable_types.count("ToolProposed") == 2
    assert run.durable_types.count("ToolFailed") == 1
    assert run.durable_types[-1] == "RunCancelled"


def test_a_call_already_in_flight_keeps_its_real_result() -> None:
    """Cancellation stops what has not started; it does not rewrite history."""

    cancellation = CancellationSource()
    first = _Recorder("read_document", on_call=lambda: cancellation.cancel("stopped"))
    second = _Recorder("text_statistics", content="counted")
    calls = (
        ToolCall(tool_call_id="toolu_1", tool_name="read_document"),
        ToolCall(tool_call_id="toolu_2", tool_name="text_statistics"),
    )
    model = FakeModel(
        [ScriptedTurn(text="Working.", tool_calls=calls, usage=USAGE)],
    )

    run = _execute(
        model,
        request=_request(tool_names=("read_document", "text_statistics")),
        bindings=[first.binding, second.binding],
        cancellation=cancellation,
    )

    # Both were in the same group and both ran; the run still ends cancelled.
    assert len(first.calls) == 1
    assert len(second.calls) == 1
    assert run.durable_types.count("ToolCompleted") == 2
    assert run.outcome.status == "cancelled"


def test_an_expired_deadline_stops_before_the_model_call() -> None:
    model = _tool_round()
    budget = RunBudget(
        max_steps=4,
        max_tool_calls=8,
        deadline=CLOCK - timedelta(seconds=1),
    )

    run = _execute(model, request=_request(budget=budget))

    assert isinstance(model, FakeModel)
    assert model.call_count == 0
    assert run.outcome.stop_reason == "deadline"


def test_a_run_requesting_an_unregistered_tool_fails_before_the_model() -> None:
    model = _tool_round()

    run = _execute(model, request=_request(tool_names=("text_statistics",)))

    assert isinstance(model, FakeModel)
    assert model.call_count == 0
    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "unknown_tool"


def test_a_model_error_becomes_a_failed_outcome() -> None:
    model = FakeModel(
        [ScriptedTurn(error=ErrorInfo(code="provider_error", message="upstream 503"))]
    )

    run = _execute(model)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "provider_error"


def test_an_exhausted_script_fails_instead_of_hanging() -> None:
    run = _execute(FakeModel(()))

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "provider_error"


def test_an_adapter_exception_never_leaks_its_message() -> None:
    run = _execute(_RaisingModel())

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert "sk-ant-canary" not in run.outcome.error.message


def test_a_stream_that_never_completes_is_a_provider_error() -> None:
    run = _execute(_TruncatedModel())

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert "without a completion event" in run.outcome.error.message


def test_an_answer_cut_off_by_the_token_ceiling_is_a_failure() -> None:
    model = FakeModel([ScriptedTurn(text="half an ans", finish_reason="max_tokens")])

    run = _execute(model)

    assert run.outcome.status == "failed"
    assert run.outcome.stop_reason == "token_budget"


def test_an_answer_longer_than_a_preview_is_not_cut_down_to_one() -> None:
    """An answer is the product of the run, not text recorded about it.

    9000 characters is an ordinary report and a short one for a bundle of
    quoted passages. Cutting it to what an event *preview* holds published a
    different answer than the provider returned, and did it silently enough
    that a truncated report reached the export node looking whole (ADR-035 §1).
    """

    answer = "x" * 9000
    run = _execute(FakeModel([ScriptedTurn(text=answer)]))

    assert run.outcome.status == "completed"
    assert run.outcome.output_text == answer


def test_an_answer_past_its_own_ceiling_is_still_clipped_and_says_so() -> None:
    """The control. The bound moved; it did not stop existing."""

    run = _execute(FakeModel([ScriptedTurn(text="x" * (ANSWER_TEXT_LIMIT + 1000))]))

    assert run.outcome.status == "completed"
    assert len(run.outcome.output_text) == ANSWER_TEXT_LIMIT
    assert run.outcome.output_text.endswith("[truncated]")


def test_usage_accumulates_across_turns() -> None:
    run = _execute(_tool_round())

    assert run.outcome.usage.tokens.input_tokens == 200
    assert run.outcome.usage.tokens.output_tokens == 40


def test_invalid_arguments_are_answered_without_reaching_the_policy() -> None:
    """Schema validation precedes authorization, so the order is observable."""

    call = ToolCall(
        tool_call_id="toolu_1",
        tool_name="read_document",
        arguments={"document_id": 42},
    )

    run = _execute(_tool_round(call))

    assert run.outcome.status == "completed"
    assert run.durable_types == [
        "RunStarted",
        "ModelStarted",
        "ModelCompleted",
        "ToolProposed",
        "ToolFailed",
        "ModelStarted",
        "ModelCompleted",
        "RunCompleted",
    ]
    assert "PermissionResolved" not in run.durable_types


def test_the_model_is_told_why_its_arguments_were_rejected() -> None:
    call = ToolCall(
        tool_call_id="toolu_1",
        tool_name="read_document",
        arguments={"wrong_key": "doc_1"},
    )

    run = _execute(_tool_round(call))
    model = run.model
    assert isinstance(model, FakeModel)
    block = model.requests[1].messages[-1].content[0]

    assert isinstance(block, ToolResultBlock)
    assert block.status == "error"
    assert "invalid_tool_input" in block.text


class _StallingModel:
    """A model that starts a stream and then never produces anything."""

    def __init__(self) -> None:
        self.closed = False

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        try:
            await asyncio.sleep(30)
            yield TextDelta(text="unreachable")  # pragma: no cover
        finally:
            # Runs when the runtime closes the generator, which is how a
            # deadline or a cancellation reaches the adapter.
            self.closed = True


class _ChattyModel:
    """A model that keeps streaming text and never finishes on its own."""

    def __init__(self) -> None:
        self.closed = False
        self.emitted = 0

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        try:
            while True:
                self.emitted += 1
                yield TextDelta(text=f"chunk {self.emitted} ")
        finally:
            self.closed = True


def test_a_stalled_model_is_stopped_by_the_runtime_envelope() -> None:
    model = _StallingModel()

    run = _execute(model, model_timeout_seconds=0.05)

    assert run.outcome.status == "failed"
    assert run.outcome.stop_reason == "error"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "provider_error"
    assert run.outcome.error.retryable is True
    assert model.closed is True


def test_a_stalled_model_is_stopped_by_the_run_deadline() -> None:
    """The same stall, reported as a budget outcome rather than a provider one."""

    model = _StallingModel()
    budget = RunBudget(
        max_steps=4,
        max_tool_calls=8,
        deadline=CLOCK + timedelta(seconds=0.05),
    )

    run = _execute(
        model,
        request=_request(budget=budget),
        model_timeout_seconds=600,
    )

    assert run.outcome.status == "failed"
    assert run.outcome.stop_reason == "deadline"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "budget_exceeded"
    assert model.closed is True


def test_a_stalled_model_never_records_a_completion() -> None:
    run = _execute(_StallingModel(), model_timeout_seconds=0.05)

    assert run.durable_types == ["RunStarted", "ModelStarted", "RunFailed"]
    assert "ModelCompleted" not in run.durable_types


def test_cancellation_stops_the_stream_at_the_next_event_boundary() -> None:
    """No sleeping: the cancel is requested from the first delta it observes."""

    model = _ChattyModel()
    cancellation = CancellationSource()
    registry = StaticToolRegistry([read_document_tool(CORPUS)])

    async def scenario() -> tuple[AgentOutcome, list[EventEnvelope]]:
        log = InMemoryEventLog(clock=_clock, event_ids=_ids("evt"))
        live: list[EventEnvelope] = []

        def observe(envelope: EventEnvelope) -> None:
            live.append(envelope)
            if envelope.event_type == "ModelDelta":
                cancellation.cancel("operator stopped the task")

        runtime = ClaudeLikeAgentRuntime(
            model=model,
            gateway=ToolGateway(
                registry=registry,
                policy=EnvelopePolicyEngine(registry=registry),
            ),
            policy_identity=POLICY_IDENTITY,
            clock=_clock,
            model_call_ids=_ids("mc"),
        )
        outcome = await runtime.run(
            _request(),
            ObservingEventSink(
                inner=ScopedEventSink(log=log, scope=SCOPE),
                observer=observe,
            ),
            cancellation,
        )
        return outcome, list(await log.read(SCOPE.stream_id))

    outcome, durable = asyncio.run(scenario())

    assert outcome.status == "cancelled"
    assert outcome.stop_reason == "cancelled"
    # One delta was emitted, the cancel was observed, and the stream stopped.
    assert model.emitted == 2
    assert model.closed is True
    assert [envelope.event_type for envelope in durable][-1] == "RunCancelled"


def test_a_tool_cannot_outlive_the_run_deadline() -> None:
    """The tool declares 5s; the run has 50ms left, and the run wins."""

    async def handler(invocation: ToolInvocation) -> ToolResult:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    slow = ToolBinding(spec=_spec("read_document"), handler=handler)
    budget = RunBudget(
        max_steps=4,
        max_tool_calls=8,
        deadline=CLOCK + timedelta(seconds=0.05),
    )

    run = _execute(
        _tool_round(),
        request=_request(budget=budget),
        bindings=[slow],
        model_timeout_seconds=600,
    )
    failure = next(
        envelope.payload
        for envelope in run.durable
        if envelope.event_type == "ToolFailed"
    )

    assert isinstance(failure, ToolFailed)
    assert failure.error.code == "tool_timeout"
    assert "run's remaining" in failure.error.message


def _binding(
    name: str,
    handler: object,
    *,
    exclusive: bool = False,
    timeout_seconds: int = 1,
) -> ToolBinding:
    spec = (
        ToolSpec(
            name=name,
            description="A side-effecting tool.",
            input_schema={"type": "object"},
            concurrency="exclusive",
            risk="write",
            idempotency="keyed",
            timeout_seconds=timeout_seconds,
            permission_scopes=("artifact:write",),
        )
        if exclusive
        else ToolSpec(
            name=name,
            description="A read-only tool.",
            input_schema={"type": "object"},
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=timeout_seconds,
        )
    )
    return ToolBinding(spec=spec, handler=handler)  # pyright: ignore[reportArgumentType]


class _Handshake:
    """Handlers that can only finish if they are in flight at the same time.

    A serial scheduler makes the waiting handler hit its timeout instead, so
    this proves concurrency without sleeping on a hope.
    """

    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None
        self.finished: list[str] = []

    def _event(self) -> asyncio.Event:
        if self.gate is None:
            self.gate = asyncio.Event()
        return self.gate

    async def waiter(self, invocation: ToolInvocation) -> ToolResult:
        await self._event().wait()
        self.finished.append(invocation.call.tool_call_id)
        return ToolResult.succeeded(invocation.call, content="waited")

    async def opener(self, invocation: ToolInvocation) -> ToolResult:
        self._event().set()
        self.finished.append(invocation.call.tool_call_id)
        return ToolResult.succeeded(invocation.call, content="opened")


class _Probe:
    """Records how many handlers are inside at once."""

    def __init__(self) -> None:
        self.inflight = 0
        self.max_inflight = 0
        self.max_inflight_beside_exclusive = 0

    def handler(self, *, exclusive: bool = False) -> object:
        async def run(invocation: ToolInvocation) -> ToolResult:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            if exclusive:
                self.max_inflight_beside_exclusive = max(
                    self.max_inflight_beside_exclusive,
                    self.inflight,
                )
            # Yield once so anything running concurrently gets in.
            await asyncio.sleep(0)
            self.inflight -= 1
            return ToolResult.succeeded(invocation.call, content="ok")

        return run


def test_read_only_tools_in_one_batch_run_concurrently() -> None:
    handshake = _Handshake()
    calls = (
        ToolCall(tool_call_id="toolu_slow", tool_name="waits"),
        ToolCall(tool_call_id="toolu_fast", tool_name="opens"),
    )
    model = FakeModel(
        [
            ScriptedTurn(text="Working.", tool_calls=calls, usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(
        model,
        request=_request(tool_names=("waits", "opens")),
        bindings=[
            _binding("waits", handshake.waiter),
            _binding("opens", handshake.opener),
        ],
    )

    assert run.outcome.status == "completed"
    # The first call could only finish because the second was already running.
    assert handshake.finished == ["toolu_fast", "toolu_slow"]


def test_results_are_submitted_in_call_order_not_completion_order() -> None:
    """The scheduler may reorder execution; it may not reorder the answer."""

    handshake = _Handshake()
    calls = (
        ToolCall(tool_call_id="toolu_slow", tool_name="waits"),
        ToolCall(tool_call_id="toolu_fast", tool_name="opens"),
    )
    model = FakeModel(
        [
            ScriptedTurn(text="Working.", tool_calls=calls, usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(
        model,
        request=_request(tool_names=("waits", "opens")),
        bindings=[
            _binding("waits", handshake.waiter),
            _binding("opens", handshake.opener),
        ],
    )
    fake = run.model
    assert isinstance(fake, FakeModel)
    submitted = [
        block.tool_call_id
        for block in fake.requests[1].messages[-1].content
        if isinstance(block, ToolResultBlock)
    ]

    assert handshake.finished == ["toolu_fast", "toolu_slow"]
    assert submitted == ["toolu_slow", "toolu_fast"]


def test_an_exclusive_tool_never_runs_beside_another() -> None:
    probe = _Probe()
    calls = (
        ToolCall(tool_call_id="toolu_1", tool_name="read_a"),
        ToolCall(tool_call_id="toolu_2", tool_name="read_b"),
        ToolCall(tool_call_id="toolu_3", tool_name="write_c"),
        ToolCall(tool_call_id="toolu_4", tool_name="read_d"),
    )
    model = FakeModel(
        [
            ScriptedTurn(text="Working.", tool_calls=calls, usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(
        model,
        request=_request(
            tool_names=("read_a", "read_b", "write_c", "read_d"),
            max_tool_risk="write",
            # This test is about the exclusive barrier, so the envelope does
            # not put the write tool behind an approval it would never get.
            approval_required_risks=(),
        ),
        bindings=[
            _binding("read_a", probe.handler()),
            _binding("read_b", probe.handler()),
            _binding("write_c", probe.handler(exclusive=True), exclusive=True),
            _binding("read_d", probe.handler()),
        ],
    )

    assert run.outcome.status == "completed"
    assert probe.max_inflight == 2
    assert probe.max_inflight_beside_exclusive == 1


def test_the_parallel_ceiling_bounds_how_many_run_at_once() -> None:
    probe = _Probe()
    calls = tuple(
        ToolCall(tool_call_id=f"toolu_{index}", tool_name=f"read_{index}")
        for index in range(4)
    )
    model = FakeModel(
        [
            ScriptedTurn(text="Working.", tool_calls=calls, usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(
        model,
        request=_request(tool_names=tuple(f"read_{index}" for index in range(4))),
        bindings=[_binding(f"read_{index}", probe.handler()) for index in range(4)],
        max_parallel_read_tools=2,
    )

    assert run.outcome.status == "completed"
    assert probe.max_inflight == 2


def test_a_tool_needing_approval_never_reaches_its_handler() -> None:
    """P0-2. "Allow, pending approval" is not permission to run.

    The envelope puts write tools behind approval by default, and no approval
    facility exists, so the effect must not happen. An irreversible write that
    nobody agreed to cannot be undone by building the approval machinery later.
    """

    recorder = _Recorder("export_artifact", risk="write")
    call = ToolCall(tool_call_id="toolu_1", tool_name="export_artifact")
    model = FakeModel(
        [
            ScriptedTurn(text="Exporting.", tool_calls=(call,), usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(
        model,
        request=_request(
            tool_names=("export_artifact",),
            max_tool_risk="write",
        ),
        bindings=[recorder.binding],
    )
    failure = next(
        envelope.payload
        for envelope in run.durable
        if envelope.event_type == "ToolFailed"
    )

    assert recorder.calls == []
    assert isinstance(failure, ToolFailed)
    assert failure.error.code == "approval_required"


def test_the_audit_trail_says_a_human_was_needed_not_that_it_was_denied() -> None:
    """A refusal for want of a decision is not the same as a decision to refuse."""

    recorder = _Recorder("export_artifact", risk="write")
    call = ToolCall(tool_call_id="toolu_1", tool_name="export_artifact")
    model = FakeModel(
        [
            ScriptedTurn(text="Exporting.", tool_calls=(call,), usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(
        model,
        request=_request(tool_names=("export_artifact",), max_tool_risk="write"),
        bindings=[recorder.binding],
    )

    assert run.durable_types == [
        "RunStarted",
        "ModelStarted",
        "ModelCompleted",
        "ToolProposed",
        "PermissionResolved",
        "PermissionRequested",
        "ToolFailed",
        "ModelStarted",
        "ModelCompleted",
        "RunCompleted",
    ]


class _StallingGate:
    """Never answers, and cancels the run the first time it is asked.

    Standing in for the operator who sees the first question appear and stops
    the run instead of answering it -- which is deterministic, where sleeping
    for a while and hoping would not be.
    """

    def __init__(self, cancellation: CancellationSource) -> None:
        self._cancellation = cancellation
        self.requests: list[str] = []

    async def request(self, **kwargs: object) -> tuple[str, str]:
        self.requests.append(str(kwargs["tool_call_id"]))
        self._cancellation.cancel("operator stopped the run")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_cancelling_during_one_approval_does_not_queue_up_the_next() -> None:
    """Authorization is serial, so each held call would spend the bound in turn.

    Two calls, each needing a human, and a bound of a hundred seconds. The
    second assertion is the one that matters: the gate is asked once. Without
    a cancellation check inside the authorize loop, the run would go on to ask
    about the second call and wait out its bound as well -- a cancelled run
    still holding everything it holds, for as long as there are calls left.

    Both ids still get exactly one result. They were shown to the model, and
    ``align_results`` fails a run that leaves one unanswered.
    """

    cancellation = CancellationSource()
    gate = _StallingGate(cancellation)
    recorder = _Recorder("export_artifact", risk="write")
    calls = (
        ToolCall(tool_call_id="toolu_1", tool_name="export_artifact"),
        ToolCall(tool_call_id="toolu_2", tool_name="export_artifact"),
    )
    model = FakeModel([ScriptedTurn(text="Exporting.", tool_calls=calls, usage=USAGE)])

    run = _execute(
        model,
        request=_request(tool_names=("export_artifact",), max_tool_risk="write"),
        bindings=[recorder.binding],
        cancellation=cancellation,
        approvals=gate,
    )

    assert gate.requests == ["toolu_1"]
    assert recorder.calls == []
    assert run.outcome.status == "cancelled"
    assert run.durable_types.count("ToolProposed") == 2
    assert run.durable_types.count("ToolFailed") == 2
    assert run.durable_types[-1] == "RunCancelled"


def test_the_model_is_told_the_call_awaits_approval() -> None:
    """The model has to be able to tell "not allowed" from "not yet decided"."""

    recorder = _Recorder("export_artifact", risk="write")
    call = ToolCall(tool_call_id="toolu_1", tool_name="export_artifact")
    model = FakeModel(
        [
            ScriptedTurn(text="Exporting.", tool_calls=(call,), usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(
        model,
        request=_request(tool_names=("export_artifact",), max_tool_risk="write"),
        bindings=[recorder.binding],
    )
    fake = run.model
    assert isinstance(fake, FakeModel)
    block = fake.requests[1].messages[-1].content[0]

    assert isinstance(block, ToolResultBlock)
    assert block.status == "error"
    assert "approval_required" in block.text


def test_an_envelope_without_an_approval_requirement_still_runs() -> None:
    """The refusal follows the requirement, not the risk class by itself."""

    recorder = _Recorder("export_artifact", risk="write")
    call = ToolCall(tool_call_id="toolu_1", tool_name="export_artifact")
    model = FakeModel(
        [
            ScriptedTurn(text="Exporting.", tool_calls=(call,), usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(
        model,
        request=_request(
            tool_names=("export_artifact",),
            max_tool_risk="write",
            approval_required_risks=(),
        ),
        bindings=[recorder.binding],
    )

    assert len(recorder.calls) == 1
    assert run.outcome.status == "completed"


# --- P1-5: ceilings that bind before the spend, not after it ------------------


def _three_reads() -> tuple[_Recorder, _Recorder, _Recorder, FakeModel]:
    """One turn proposing three read calls, then a final answer."""

    recorders = (_Recorder("read_a"), _Recorder("read_b"), _Recorder("read_c"))
    model = FakeModel(
        [
            ScriptedTurn(
                text="Reading three.",
                tool_calls=(
                    ToolCall(tool_call_id="toolu_1", tool_name="read_a"),
                    ToolCall(tool_call_id="toolu_2", tool_name="read_b"),
                    ToolCall(tool_call_id="toolu_3", tool_name="read_c"),
                ),
                usage=USAGE,
            ),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )
    return (*recorders, model)


def test_a_batch_over_the_tool_ceiling_does_not_dispatch_the_excess() -> None:
    """P1-5. The ceiling is spent before the batch, not counted after it.

    A turn proposing more calls than remain used to run every one of them and
    report the overrun afterwards. Side effects that have already happened are
    an accounting entry, not a limit.
    """

    first, second, third, model = _three_reads()

    _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2),
            tool_names=("read_a", "read_b", "read_c"),
        ),
        bindings=[first.binding, second.binding, third.binding],
    )

    assert len(first.calls) + len(second.calls) + len(third.calls) == 2
    assert third.calls == []


def test_the_ledger_never_reports_more_tool_calls_than_the_ceiling() -> None:
    """A budget the usage report can exceed is not a budget."""

    first, second, third, model = _three_reads()

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2),
            tool_names=("read_a", "read_b", "read_c"),
        ),
        bindings=[first.binding, second.binding, third.binding],
    )

    assert run.outcome.usage.tool_calls == 2


def test_a_run_that_spent_its_tool_allowance_answers_from_what_it_got() -> None:
    """The tool ceiling closes the toolbox; it does not end the run.

    This is the same script as the test above, and it used to die at
    `max_tool_calls` with an empty `output_text` -- two reads dispatched, their
    results in the transcript, and nothing written from them. The run had spent
    its allowance and was stopped one turn short of the answer that allowance
    was bought for.

    Measured on the real thing before this changed: the chat web fallback
    searched twice, got 5.5KB of results, proposed a third search, was refused,
    and the turn ended up answering "I have no ability to search". Every
    ceiling except this one leaves a run with nothing further it can do. This
    one leaves it with an answer to write, so the loop lets it write one.
    """

    first, second, third, model = _three_reads()

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2),
            tool_names=("read_a", "read_b", "read_c"),
        ),
        bindings=[first.binding, second.binding, third.binding],
    )

    assert run.outcome.status == "completed"
    assert run.outcome.output_text == "Done."
    # The ceiling still held: the third call never ran.
    assert run.outcome.usage.tool_calls == 2
    assert third.calls == []


def test_the_answering_turn_is_offered_no_tools_at_all() -> None:
    """Taken off the request, not left on it to be refused.

    Leaving them advertised is what produced the defect: a model that can still
    see the tool proposes the tool, and the only thing left to do with the
    proposal is turn it away and kill the run. The mechanism is the absence.
    """

    first, second, third, model = _three_reads()

    _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2),
            tool_names=("read_a", "read_b", "read_c"),
        ),
        bindings=[first.binding, second.binding, third.binding],
    )

    assert len(model.requests) == 2
    # Offered while the allowance lasted...
    assert {spec.name for spec in model.requests[0].tools} == {
        "read_a",
        "read_b",
        "read_c",
    }
    # ...and gone once it was spent.
    assert model.requests[1].tools == ()


def test_tools_stay_on_the_request_while_the_allowance_lasts() -> None:
    """The control. Removing them a turn early is a silently crippled run.

    Same script, one more call of headroom: nothing is taken away, because
    nothing was spent. A implementation that dropped tools whenever any call
    had been made would pass every test above and this is what would catch it.
    """

    first, second, third, model = _three_reads()

    _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=4, max_tool_calls=4),
            tool_names=("read_a", "read_b", "read_c"),
        ),
        bindings=[first.binding, second.binding, third.binding],
    )

    assert len(model.requests) == 2
    assert model.requests[1].tools != ()


def test_a_call_refused_for_budget_still_answers_its_id() -> None:
    """Every proposed id owes a result, whatever the reason it did not run."""

    first, second, third, model = _three_reads()

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2),
            tool_names=("read_a", "read_b", "read_c"),
        ),
        bindings=[first.binding, second.binding, third.binding],
    )
    failure = next(
        envelope.payload
        for envelope in run.durable
        if envelope.event_type == "ToolFailed"
    )

    assert isinstance(failure, ToolFailed)
    assert failure.tool_call_id == "toolu_3"
    assert failure.error.code == "budget_exceeded"


def test_a_batch_inside_the_ceiling_runs_in_full() -> None:
    """The control: the refusal is the ceiling, not the batch size.

    The budget leaves headroom on purpose. A ceiling consumed exactly stops the
    next turn, which is pre-existing behaviour and would hide what this asserts.
    """

    first, second, third, model = _three_reads()

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=4, max_tool_calls=4),
            tool_names=("read_a", "read_b", "read_c"),
        ),
        bindings=[first.binding, second.binding, third.binding],
    )

    assert len(first.calls) + len(second.calls) + len(third.calls) == 3
    assert run.outcome.status == "completed"


def test_a_run_that_passes_its_token_ceiling_is_not_completed() -> None:
    """P1-5. The top-of-loop check cannot see what the turn it precedes spent."""

    model = FakeModel([ScriptedTurn(text="Done.", usage=USAGE)])

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2, max_total_tokens=1)
        ),
    )

    assert run.outcome.status == "failed"
    assert run.outcome.stop_reason == "token_budget"
    assert run.outcome.usage.tokens.total == USAGE.total


def test_tools_do_not_run_after_the_token_ceiling_is_passed() -> None:
    """A turn that blew the ceiling must not go on to spend side effects."""

    recorder = _Recorder("read_a")
    model = FakeModel(
        [
            ScriptedTurn(
                text="Reading.",
                tool_calls=(ToolCall(tool_call_id="toolu_1", tool_name="read_a"),),
                usage=USAGE,
            ),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2, max_total_tokens=1),
            tool_names=("read_a",),
        ),
        bindings=[recorder.binding],
    )

    assert recorder.calls == []
    assert run.outcome.stop_reason == "token_budget"


def test_a_run_inside_its_token_ceiling_still_completes() -> None:
    """The control."""

    model = FakeModel([ScriptedTurn(text="Done.", usage=USAGE)])

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2, max_total_tokens=10_000)
        ),
    )

    assert run.outcome.status == "completed"


def test_the_step_ceiling_stops_before_tools_nothing_can_read() -> None:
    """The last step went to the model, so a tool result has no reader left.

    Running them anyway spends real side effects to produce output that is
    discarded, which is the same defect as the tool ceiling in a different
    place.
    """

    recorder = _Recorder("read_a")
    model = FakeModel(
        [
            ScriptedTurn(
                text="Reading.",
                tool_calls=(ToolCall(tool_call_id="toolu_1", tool_name="read_a"),),
                usage=USAGE,
            )
        ],
        repeat_last=True,
    )

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=1, max_tool_calls=1),
            tool_names=("read_a",),
        ),
        bindings=[recorder.binding],
    )

    assert recorder.calls == []
    assert run.outcome.stop_reason == "max_steps"


def test_a_cost_ceiling_this_process_cannot_price_is_refused() -> None:
    """P1-5. Unpriced, cost_micro_usd stays zero and the ceiling never fires.

    A ceiling that cannot be enforced must not be accepted as one: the caller
    asked for a guarantee, and silently not providing it is worse than saying
    so. What ADR-030 changed is the scope of "cannot" -- it used to be every
    deployment, because no pricer existed anywhere, and it is now the ones that
    did not configure prices. The refusal has to survive that narrowing, or the
    guarantee quietly becomes "unless you forgot to configure it".
    """

    recorder = _Recorder("read_a")
    model = FakeModel([ScriptedTurn(text="Done.", usage=USAGE)])

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2, max_cost_micro_usd=1),
            tool_names=("read_a",),
        ),
        bindings=[recorder.binding],
    )

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    # Names the profile, so the reader is sent to the config rather than left
    # to conclude the runtime cannot do this at all.
    assert "no prices are configured" in run.outcome.error.message
    assert run.durable_types == ["RunStarted", "RunFailed"]


def test_the_same_ceiling_is_accepted_once_the_profile_has_prices() -> None:
    """The control, and the whole point of the change.

    Without it, a runtime that refused *every* cost ceiling -- the behaviour
    being replaced -- would still satisfy the refusal test above.
    """

    model = FakeModel([ScriptedTurn(text="Done.", usage=USAGE)])

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2, max_cost_micro_usd=10**9),
            tool_names=(),
        ),
        prices=PRICES,
    )

    assert run.outcome.status == "completed"
    assert run.outcome.error is None


def test_a_priced_run_reports_what_it_spent() -> None:
    """A ceiling is only a ceiling if something climbs towards it.

    The assertion spells the arithmetic rather than the answer. A total copied
    from a previous run would agree with any pricer, including one that
    returned a constant.
    """

    model = FakeModel([ScriptedTurn(text="Done.", usage=USAGE)])

    run = _execute(
        model,
        request=_request(budget=RunBudget(max_steps=2, max_tool_calls=2)),
        prices=PRICES,
    )

    expected = (
        (USAGE.input_tokens - USAGE.cache_read_tokens) * 270_000 // 1_000_000
        + USAGE.cache_read_tokens * 27_000 // 1_000_000
        + USAGE.output_tokens * 1_100_000 // 1_000_000
    )
    assert expected > 0
    assert run.outcome.usage.cost_micro_usd == expected


def test_an_unpriced_run_reports_no_spend_rather_than_a_guess() -> None:
    """The control for the one above: zero is what "we were not told" looks like.

    A runtime that estimated would put a number on the event stream that reads
    exactly like a measured one.
    """

    model = FakeModel([ScriptedTurn(text="Done.", usage=USAGE)])

    run = _execute(
        model,
        request=_request(budget=RunBudget(max_steps=2, max_tool_calls=2)),
    )

    assert run.outcome.usage.tokens.total > 0
    assert run.outcome.usage.cost_micro_usd == 0


def test_a_run_is_stopped_by_cost_before_it_runs_out_of_steps() -> None:
    """The ceiling ADR-030 exists to make usable: spend, not turns.

    A model that keeps asking for tools, twenty steps allowed, and a cost
    ceiling just above one turn's spend. ``max_steps`` cannot be what ends this
    -- which is the point, because before ADR-030 it was the only thing that
    could.
    """

    model = FakeModel(
        [ScriptedTurn(text="again", tool_calls=(READ_CALL,), usage=USAGE)],
        repeat_last=True,
    )
    one_turn = PRICES.cost_micro_usd(USAGE)

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(
                max_steps=20,
                max_tool_calls=20,
                max_cost_micro_usd=one_turn + 1,
            )
        ),
        prices=PRICES,
    )

    assert run.outcome.stop_reason == "cost_budget"
    assert run.outcome.usage.steps < 20


def test_the_same_run_under_a_generous_cost_ceiling_is_stopped_by_steps() -> None:
    """The control: the cost ceiling stopped it, not the pricer's existence.

    Identical model, identical step ceiling, a cost ceiling far out of reach.
    Without this, a pricer that reported an enormous number on every turn --
    or a runtime that ended any priced run early -- would satisfy the test
    above.

    Each turn reads a *different* document. The turns used to be one identical
    call repeated, which the repeat guard now stops well before step 20 -- and
    a run stopped for repeating itself would not be a control for the cost
    ceiling at all. Varying the argument keeps the subject and drops the
    coincidence.
    """

    model = FakeModel(
        [
            ScriptedTurn(
                text="again",
                tool_calls=(
                    READ_CALL.model_copy(
                        update={
                            "tool_call_id": f"toolu_step_{step}",
                            "arguments": {"document_id": f"doc_{step}"},
                        }
                    ),
                ),
                usage=USAGE,
            )
            for step in range(20)
        ]
    )

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(
                max_steps=20,
                max_tool_calls=20,
                max_cost_micro_usd=10**9,
            )
        ),
        prices=PRICES,
    )

    assert run.outcome.stop_reason == "max_steps"
    assert run.outcome.usage.steps == 20


def test_a_run_without_a_cost_ceiling_is_unaffected() -> None:
    """The control: the refusal is the unenforceable ceiling, not the budget."""

    model = FakeModel([ScriptedTurn(text="Done.", usage=USAGE)])

    run = _execute(
        model, request=_request(budget=RunBudget(max_steps=2, max_tool_calls=2))
    )

    assert run.outcome.status == "completed"


def test_a_repeated_tool_call_id_runs_nothing() -> None:
    """P1-8. A tool_call_id is what a result answers to.

    Two calls sharing one leave no way to say which result belongs to which.
    The pairing rule caught it, but only after the handlers had run -- so a
    model repeating an id got its tool executed once per repetition, and the
    run then died on the bookkeeping.
    """

    recorder = _Recorder("read_document", risk="read")
    duplicate = ToolCall(tool_call_id="toolu_same", tool_name="read_document")
    model = FakeModel(
        [
            ScriptedTurn(text="Twice.", tool_calls=(duplicate, duplicate), usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(model, bindings=[recorder.binding])

    assert recorder.calls == []
    assert run.outcome.status == "failed"


def test_the_repeated_id_run_ends_in_an_outcome_not_an_exception() -> None:
    """``AgentExecutor`` promises a terminal outcome for predictable failures.

    It used to raise ``ToolPairingError`` straight through ``run()``, so a
    graph node got a traceback rather than something it could record or route
    on. That is a contract violation, not merely bad ordering.
    """

    duplicate = ToolCall(tool_call_id="toolu_same", tool_name="read_document")
    model = FakeModel(
        [
            ScriptedTurn(text="Twice.", tool_calls=(duplicate, duplicate), usage=USAGE),
        ]
    )

    run = _execute(model)
    failure = next(
        envelope.payload
        for envelope in run.durable
        if envelope.event_type == "RunFailed"
    )

    assert isinstance(failure, RunFailed)
    assert failure.stop_reason == "error"
    assert failure.error.code == "provider_error"
    assert "toolu_same" in failure.error.message


def test_distinct_ids_in_one_turn_are_unaffected() -> None:
    """The control: the refusal is about repetition, not about several calls."""

    recorder = _Recorder("read_document", risk="read")
    model = FakeModel(
        [
            ScriptedTurn(
                text="Two reads.",
                tool_calls=(
                    ToolCall(tool_call_id="toolu_1", tool_name="read_document"),
                    ToolCall(tool_call_id="toolu_2", tool_name="read_document"),
                ),
                usage=USAGE,
            ),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(model, bindings=[recorder.binding])

    assert len(recorder.calls) == 2
    assert run.outcome.status == "completed"


def test_the_same_id_across_two_turns_is_not_a_repetition() -> None:
    """Uniqueness is per turn. Ids only have to be distinguishable among peers."""

    recorder = _Recorder("read_document", risk="read")
    call = ToolCall(tool_call_id="toolu_1", tool_name="read_document")
    model = FakeModel(
        [
            ScriptedTurn(text="Once.", tool_calls=(call,), usage=USAGE),
            ScriptedTurn(text="Again.", tool_calls=(call,), usage=USAGE),
            ScriptedTurn(text="Done.", usage=USAGE),
        ]
    )

    run = _execute(model, bindings=[recorder.binding])

    assert len(recorder.calls) == 2
    assert run.outcome.status == "completed"


class _MisPairingGateway(ToolGateway):
    """A gateway that answers a call with somebody else's id.

    Nothing in the shipped code does this. It exists because the pairing
    backstop is otherwise unreachable -- duplicate ids are refused before
    dispatch -- and an unreachable branch that nobody has run is a claim, not
    a guarantee.
    """

    async def invoke(self, prepared: PreparedCall, **kwargs: Any) -> ToolResult:
        result = await super().invoke(prepared, **kwargs)
        return result.model_copy(update={"tool_call_id": "toolu_not_the_one_asked"})


def test_a_broken_pairing_invariant_is_reported_not_raised() -> None:
    """Whatever this runtime got wrong, it is still this runtime's to report.

    A graph node needs something it can record and route on. A traceback out
    of ``run()`` is neither, and the executor contract says so.
    """

    registry = StaticToolRegistry([read_document_tool(CORPUS)])
    call = ToolCall(
        tool_call_id="toolu_1",
        tool_name="read_document",
        arguments={"document_id": "doc_1"},
    )
    model = FakeModel([ScriptedTurn(text="Reading.", tool_calls=(call,), usage=USAGE)])

    async def scenario() -> AgentOutcome:
        log = InMemoryEventLog(clock=_clock, event_ids=_ids("evt"))
        runtime = ClaudeLikeAgentRuntime(
            model=model,
            gateway=_MisPairingGateway(
                registry=registry,
                policy=EnvelopePolicyEngine(registry=registry),
                executor=ToolExecutor(monotonic=_ticking()),
            ),
            policy_identity=POLICY_IDENTITY,
            clock=_clock,
            model_call_ids=_ids("mc"),
        )
        return await runtime.run(
            _request(), ScopedEventSink(log=log, scope=SCOPE), CancellationSource()
        )

    outcome = asyncio.run(scenario())

    assert outcome.status == "failed"
    assert outcome.stop_reason == "error"


class _ClosableStream:
    """A closable ``AsyncIterator`` that is not an ``AsyncGenerator``.

    Exactly what ``ModelPort`` permits and what the runtime used to skip. A
    real adapter shaped like this -- one wrapping a connection it has to
    release -- leaked it, and a leak of that shape surfaces as exhausted
    connections far away from the line that caused it.
    """

    def __init__(self, events: Sequence[ModelEvent]) -> None:
        self._events = list(events)
        self.closed = False

    def __aiter__(self) -> _ClosableStream:
        return self

    async def __anext__(self) -> ModelEvent:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class _ClosableModel:
    """A model whose stream is closable but not a generator."""

    def __init__(self) -> None:
        self.stream_object = _ClosableStream(
            [
                ModelUsageReported(usage=USAGE),
                ModelStreamCompleted(finish_reason="stop"),
            ]
        )

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self.stream_object


def test_a_closable_stream_that_is_not_a_generator_is_closed() -> None:
    """P2-2. ``aclose`` is the protocol; ``AsyncGenerator`` is one satisfier."""

    model = _ClosableModel()

    run = _execute(model)

    assert model.stream_object.closed is True
    assert run.outcome.status == "completed"


def test_a_stream_with_no_aclose_does_not_break_the_run() -> None:
    """The control: closing is offered where possible, never required."""

    class _Bare:
        def __init__(self) -> None:
            self._events = [ModelStreamCompleted(finish_reason="stop")]

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> ModelEvent:
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

    class _BareModel:
        def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            return _Bare()  # pyright: ignore[reportReturnType]

    run = _execute(_BareModel())  # pyright: ignore[reportArgumentType]

    assert run.outcome.status == "completed"


def test_a_one_step_budget_completes_a_run_the_model_finished_in_one_step() -> None:
    """Spending an allowance exactly is not overspending it.

    A defect I introduced with the P1-5 hard ceilings: the post-turn check
    reused ``stop_reason_for``, which answers "may I start more work?" and is
    documented as being evaluated before a turn, never after. Asked afterwards
    it made ``max_steps=N`` behave as ``N-1`` on the completion path -- the
    model answered, and the answer was thrown away.
    """

    model = FakeModel([ScriptedTurn(text="Done in one step.", usage=USAGE)])

    run = _execute(
        model, request=_request(budget=RunBudget(max_steps=1, max_tool_calls=1))
    )

    assert run.outcome.status == "completed"
    assert run.outcome.output_text == "Done in one step."


def test_a_token_ceiling_still_fails_a_run_that_passed_it() -> None:
    """The control the fix above must not weaken: an overrun is still a failure.

    Tokens are different in kind from steps. What a model call costs is
    unknowable before making it, so passing the ceiling is only ever
    observable afterwards -- which is why the post-turn check exists at all.
    """

    model = FakeModel([ScriptedTurn(text="Expensive.", usage=USAGE)])

    run = _execute(
        model,
        request=_request(
            budget=RunBudget(max_steps=2, max_tool_calls=2, max_total_tokens=1)
        ),
    )

    assert run.outcome.status == "failed"
    assert run.outcome.stop_reason == "token_budget"


def _repeating(times: int) -> FakeModel:
    """A model that asks the identical question `times` turns in a row."""

    return FakeModel(
        [
            ScriptedTurn(
                text="The sub-pages all redirect to the main page. Let me try again.",
                tool_calls=(
                    READ_CALL.model_copy(update={"tool_call_id": f"toolu_{turn}"}),
                ),
                usage=USAGE,
            )
            for turn in range(times)
        ]
        + [ScriptedTurn(text="Done.", usage=USAGE)]
    )


def test_reading_the_same_document_twice_is_still_allowed() -> None:
    """The floor. Asking again is ordinary; only the fourth time is not."""

    recorder = _Recorder("read_document", risk="read")

    run = _execute(_repeating(2), bindings=[recorder.binding])

    assert len(recorder.calls) == 2
    assert run.outcome.status == "completed"


def test_a_fourth_identical_call_is_refused_without_running() -> None:
    """The observed loop fetched one URL eight times. The tool runs three."""

    recorder = _Recorder("read_document", risk="read")

    _execute(_repeating(6), bindings=[recorder.binding])

    assert len(recorder.calls) == 3, "the tool must not do the same work again"


def test_a_run_that_keeps_repeating_is_stopped_before_its_token_ceiling() -> None:
    """What this exists for.

    The research node burned a 120k token budget re-fetching one page it had
    already read. Ending on the repeats names the cause; ending on the ceiling
    reports only that the money ran out.
    """

    run = _execute(
        _repeating(8),
        request=_request(budget=RunBudget(max_steps=20, max_tool_calls=20)),
        bindings=[_Recorder("read_document").binding],
    )

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert "already made" in run.outcome.error.message
    assert run.outcome.stop_reason != "token_budget"


def test_the_same_tool_with_different_arguments_is_not_a_repeat() -> None:
    """The control: the guard is about the question, not about the tool."""

    recorder = _Recorder("read_document", risk="read")
    model = FakeModel(
        [
            ScriptedTurn(
                text="next",
                tool_calls=(
                    READ_CALL.model_copy(
                        update={
                            "tool_call_id": f"toolu_{turn}",
                            "arguments": {"document_id": f"doc_{turn}"},
                        }
                    ),
                ),
                usage=USAGE,
            )
            for turn in range(6)
        ]
        + [ScriptedTurn(text="Done.", usage=USAGE)]
    )

    run = _execute(
        model,
        request=_request(budget=RunBudget(max_steps=20, max_tool_calls=20)),
        bindings=[recorder.binding],
    )

    assert len(recorder.calls) == 6
    assert run.outcome.status == "completed"


def test_argument_key_order_does_not_hide_a_repeat() -> None:
    """Two spellings of one question are one question."""

    recorder = _Recorder("read_document", risk="read")
    first = {"document_id": "doc_1", "page": 2}
    second = {"page": 2, "document_id": "doc_1"}
    model = FakeModel(
        [
            ScriptedTurn(
                text="again",
                tool_calls=(
                    READ_CALL.model_copy(
                        update={"tool_call_id": f"toolu_{turn}", "arguments": args}
                    ),
                ),
                usage=USAGE,
            )
            for turn, args in enumerate((first, second, first, second, first, second))
        ]
        + [ScriptedTurn(text="Done.", usage=USAGE)]
    )

    _execute(model, bindings=[recorder.binding])

    assert len(recorder.calls) == 3, "key order must not buy extra attempts"
