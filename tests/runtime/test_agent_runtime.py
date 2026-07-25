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

from agent_workbench.adapters.events import ObservingEventSink, ScopedEventSink
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry, read_document_tool
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.events import EventEnvelope, ToolFailed
from agent_workbench.domain.messages import ToolResultBlock, user_message
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    RunBudget,
    TokenUsage,
    TraceContext,
)
from agent_workbench.domain.tools import ToolCall, ToolName, ToolResult, ToolSpec
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.cancellation import CancellationSource
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.model import ModelEvent, ModelPort, ModelRequest
from agent_workbench.ports.model import ModelTextDelta as TextDelta
from agent_workbench.ports.tools import ToolBinding, ToolInvocation
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolExecutor, ToolGateway

CLOCK = datetime(2026, 7, 25, 3, 14, 15, tzinfo=UTC)
SCOPE = EventScope(stream_id="stream_1", run_id="run_1")
CORPUS = {"doc_1": "Qdrant performs one dense and sparse fusion per query."}
USAGE = TokenUsage(input_tokens=100, output_tokens=20)
POLICY_IDENTITY = "policy-test:0000000000000000"

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


def _request(
    *,
    budget: RunBudget | None = None,
    tool_names: Sequence[ToolName] = ("read_document",),
    allowed_tools: Sequence[ToolName] | None = None,
    max_tool_risk: str = "read",
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
            ),
            policy_identity=POLICY_IDENTITY,
            model_timeout_seconds=model_timeout_seconds,
            clock=_clock,
            model_call_ids=_ids("mc"),
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


def test_the_tool_call_ceiling_also_stops_the_loop() -> None:
    """Two calls per turn exhaust the tool budget before the step budget."""

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
    assert run.outcome.usage.steps == 2
    assert run.outcome.usage.tool_calls == 4


def test_an_already_cancelled_run_never_calls_the_model() -> None:
    model = _tool_round()
    cancellation = CancellationSource()
    cancellation.cancel("operator stopped the task")

    run = _execute(model, cancellation=cancellation)

    assert isinstance(model, FakeModel)
    assert model.call_count == 0
    assert run.outcome.status == "cancelled"
    assert run.durable_types == ["RunStarted", "RunCancelled"]


def test_cancellation_mid_batch_still_answers_every_call() -> None:
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
    )

    assert len(first.calls) == 1
    assert second.calls == []
    assert run.outcome.status == "cancelled"
    assert run.durable_types.count("ToolProposed") == 2
    assert run.durable_types[-1] == "RunCancelled"


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


def test_a_long_answer_is_clipped_to_the_domain_ceiling() -> None:
    run = _execute(FakeModel([ScriptedTurn(text="x" * 9000)]))

    assert run.outcome.status == "completed"
    assert len(run.outcome.output_text) == 4096
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
