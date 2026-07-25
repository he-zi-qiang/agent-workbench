"""The walking-skeleton executor: what it does, and what it refuses.

The refusals matter more than the happy path here. This executor exists to
carry the contract until the custom runtime owns the model-tool loop, so every
case it cannot honour has to end in a structured outcome that says so, never in
a dropped call, a raised exception or a quiet success.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import count

from agent_workbench.adapters.agents import SingleTurnAgentExecutor
from agent_workbench.adapters.events import ObservingEventSink, ScopedEventSink
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.adapters.tools import StaticToolRegistry, read_document_tool
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.events import EventEnvelope, ModelCompleted, ToolProposed
from agent_workbench.domain.messages import user_message
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    RunBudget,
    TokenUsage,
    TraceContext,
)
from agent_workbench.domain.tools import ToolCall, ToolName
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.cancellation import CancellationSource, NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.model import (
    ModelEvent,
    ModelPort,
    ModelRequest,
    ModelStreamCompleted,
    ModelTextDelta,
)

CLOCK = datetime(2026, 7, 25, 3, 14, 15, tzinfo=UTC)
SCOPE = EventScope(stream_id="stream_1", run_id="run_1")
USAGE = TokenUsage(input_tokens=100, output_tokens=20)
CORPUS = {"doc_1": "Qdrant performs one dense and sparse fusion per query."}


def _clock() -> datetime:
    return CLOCK


def _ids(prefix: str) -> Callable[[], str]:
    counter = count(1)

    def next_id() -> str:
        return f"{prefix}_{next(counter)}"

    return next_id


def _request(
    *,
    budget: RunBudget | None = None,
    tool_names: Sequence[ToolName] = (),
) -> AgentRunRequest:
    return AgentRunRequest(
        trace=TraceContext(agent_run_id="run_1"),
        run_kind="chat",
        stream_id="stream_1",
        principal=PrincipalContext(principal_id="user_1", tenant_id="tenant_a"),
        envelope=AuthorizationEnvelope(allowed_tools=("read_document",)),
        budget=budget
        if budget is not None
        else RunBudget(max_steps=4, max_tool_calls=8),
        messages=(user_message("Who owns hybrid fusion?"),),
        tool_names=tuple(tool_names),
    )


class _Execution:
    """One completed run, with everything it emitted."""

    def __init__(
        self,
        outcome: AgentOutcome,
        live: list[EventEnvelope],
        durable: tuple[EventEnvelope, ...],
    ) -> None:
        self.outcome = outcome
        self.live = live
        self.durable = durable

    @property
    def live_types(self) -> list[str]:
        return [envelope.event_type for envelope in self.live]

    @property
    def durable_types(self) -> list[str]:
        return [envelope.event_type for envelope in self.durable]


def _execute(
    model: ModelPort,
    *,
    request: AgentRunRequest | None = None,
    registry: StaticToolRegistry | None = None,
    cancellation: CancellationSource | None = None,
    clock: Callable[[], datetime] | None = None,
) -> _Execution:
    async def scenario() -> _Execution:
        log = InMemoryEventLog(clock=_clock, event_ids=_ids("evt"))
        live: list[EventEnvelope] = []
        sink = ObservingEventSink(
            inner=ScopedEventSink(log=log, scope=SCOPE),
            observer=live.append,
        )
        executor = SingleTurnAgentExecutor(
            model=model,
            registry=registry,
            clock=clock if clock is not None else _clock,
            model_call_ids=_ids("mc"),
        )
        outcome = await executor.run(
            request if request is not None else _request(),
            sink,
            cancellation if cancellation is not None else NullCancellationToken(),
        )
        return _Execution(outcome, live, await log.read(SCOPE.stream_id))

    return asyncio.run(scenario())


class _RaisingModel:
    """An adapter that fails mid-stream, with a secret in its exception."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        raise RuntimeError("connection reset: sk-ant-canary-must-not-leak")
        yield ModelTextDelta(text="unreachable")  # pragma: no cover


class _TruncatedModel:
    """An adapter that violates the port by never completing its stream."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        yield ModelTextDelta(text="half a sentence")


def test_the_executor_satisfies_the_agent_executor_protocol() -> None:
    executor: AgentExecutor = SingleTurnAgentExecutor(model=FakeModel(()))

    assert isinstance(executor, AgentExecutor)


def test_a_text_turn_completes_and_reports_its_usage() -> None:
    model = FakeModel([ScriptedTurn(text="Qdrant does.", usage=USAGE)])

    run = _execute(model)

    assert run.outcome.status == "completed"
    assert run.outcome.stop_reason == "completed"
    assert run.outcome.output_text == "Qdrant does."
    assert run.outcome.usage.steps == 1
    assert run.outcome.usage.tokens == USAGE


def test_the_durable_timeline_omits_the_token_deltas() -> None:
    model = FakeModel([ScriptedTurn(text="Qdrant does.", usage=USAGE)])

    run = _execute(model)

    assert run.durable_types == [
        "RunStarted",
        "ModelStarted",
        "ModelCompleted",
        "RunCompleted",
    ]
    assert "ModelDelta" in run.live_types
    assert "ModelDelta" not in run.durable_types


def test_the_durable_timeline_is_gap_free() -> None:
    model = FakeModel([ScriptedTurn(text="Qdrant does.")])

    run = _execute(model)

    assert [envelope.sequence for envelope in run.durable] == [1, 2, 3, 4]


def test_a_proposed_tool_call_is_recorded_before_the_run_fails() -> None:
    """A dropped call would leave the model waiting for an answer forever."""

    call = ToolCall(
        tool_call_id="toolu_1",
        tool_name="read_document",
        arguments={"document_id": "doc_1"},
    )
    model = FakeModel([ScriptedTurn(text="Reading.", tool_calls=(call,))])

    run = _execute(model, registry=StaticToolRegistry([read_document_tool(CORPUS)]))

    assert run.durable_types == [
        "RunStarted",
        "ModelStarted",
        "ModelCompleted",
        "ToolProposed",
        "RunFailed",
    ]
    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert "owns no tool loop" in run.outcome.error.message


def test_a_recorded_proposal_carries_a_digest_not_its_arguments() -> None:
    call = ToolCall(
        tool_call_id="toolu_1",
        tool_name="read_document",
        arguments={"document_id": "doc_1"},
    )
    model = FakeModel([ScriptedTurn(tool_calls=(call,))])

    run = _execute(model)
    proposed = next(
        envelope.payload
        for envelope in run.durable
        if isinstance(envelope.payload, ToolProposed)
    )
    completed = next(
        envelope.payload
        for envelope in run.durable
        if isinstance(envelope.payload, ModelCompleted)
    )

    assert proposed.argument_sha256 != ""
    assert "doc_1" not in proposed.model_dump_json()
    assert completed.tool_call_ids == ("toolu_1",)


def test_a_model_error_becomes_a_failed_outcome() -> None:
    model = FakeModel(
        [
            ScriptedTurn(
                error=ErrorInfo(code="provider_error", message="upstream 503"),
            )
        ]
    )

    run = _execute(model)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "provider_error"
    assert run.durable_types[-1] == "RunFailed"


def test_an_exhausted_script_fails_instead_of_hanging() -> None:
    run = _execute(FakeModel(()))

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "provider_error"


def test_an_adapter_exception_becomes_an_outcome_without_leaking_its_message() -> None:
    run = _execute(_RaisingModel())

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "provider_error"
    assert "sk-ant-canary" not in run.outcome.error.message
    assert "sk-ant-canary" not in str(run.durable)


def test_a_stream_that_never_completes_is_a_provider_error() -> None:
    run = _execute(_TruncatedModel())

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert "without a completion event" in run.outcome.error.message


def test_an_already_cancelled_run_never_calls_the_model() -> None:
    model = FakeModel([ScriptedTurn(text="never reached")])
    cancellation = CancellationSource()
    cancellation.cancel("operator stopped the task")

    run = _execute(model, cancellation=cancellation)

    assert model.call_count == 0
    assert run.outcome.status == "cancelled"
    assert run.outcome.stop_reason == "cancelled"
    assert run.durable_types == ["RunStarted", "RunCancelled"]


def test_an_expired_deadline_stops_before_the_model_call() -> None:
    """A ceiling consulted after the spend is not a ceiling."""

    model = FakeModel([ScriptedTurn(text="never reached")])
    budget = RunBudget(
        max_steps=4,
        max_tool_calls=8,
        deadline=CLOCK - timedelta(seconds=1),
    )

    run = _execute(model, request=_request(budget=budget))

    assert model.call_count == 0
    assert run.outcome.status == "failed"
    assert run.outcome.stop_reason == "deadline"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "budget_exceeded"


def test_a_tool_the_process_does_not_register_fails_closed() -> None:
    model = FakeModel([ScriptedTurn(text="never reached")])

    run = _execute(
        model,
        request=_request(tool_names=("read_document",)),
        registry=StaticToolRegistry([]),
    )

    assert model.call_count == 0
    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "unknown_tool"


def test_a_registered_tool_is_advertised_to_the_model() -> None:
    model = FakeModel([ScriptedTurn(text="Qdrant does.")])

    _execute(
        model,
        request=_request(tool_names=("read_document",)),
        registry=StaticToolRegistry([read_document_tool(CORPUS)]),
    )

    assert model.requests[0].tool_names() == ("read_document",)


def test_an_answer_cut_off_by_the_token_ceiling_is_reported_as_a_failure() -> None:
    model = FakeModel([ScriptedTurn(text="half an ans", finish_reason="max_tokens")])

    run = _execute(model)

    assert run.outcome.status == "failed"
    assert run.outcome.stop_reason == "token_budget"
    assert run.outcome.output_text == ""


def test_a_finish_for_tool_use_without_a_call_is_a_provider_error() -> None:
    model = FakeModel([ScriptedTurn(text="", finish_reason="tool_use")])

    run = _execute(model)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert "proposed no call" in run.outcome.error.message


def test_a_long_answer_is_clipped_to_the_domain_ceiling() -> None:
    model = FakeModel([ScriptedTurn(text="x" * 9000)])

    run = _execute(model)

    assert run.outcome.status == "completed"
    assert len(run.outcome.output_text) == 4096
    assert run.outcome.output_text.endswith("[truncated]")


def test_a_stream_completion_without_usage_keeps_the_reported_usage() -> None:
    model = FakeModel([ScriptedTurn(text="ok", usage=USAGE)])

    run = _execute(model)

    assert run.outcome.usage.tokens == USAGE


def test_the_completion_event_is_the_last_word_on_the_finish_reason() -> None:
    async def scenario() -> ModelStreamCompleted:
        events = [
            event
            async for event in FakeModel([ScriptedTurn(text="ok")]).stream(
                ModelRequest(messages=(user_message("hi"),))
            )
        ]
        last = events[-1]
        assert isinstance(last, ModelStreamCompleted)
        return last

    assert asyncio.run(scenario()).finish_reason == "stop"
