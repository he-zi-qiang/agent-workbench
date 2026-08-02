"""Recording what a run did, and never letting that break the run.

``observability.otel_enabled`` has been ``Literal[True]`` since the settings
were written, with nothing behind it -- a flag that could not be turned off and
did not turn anything on. These tests are what make the flag mean something,
and they concentrate on the property that matters more than any metric:

**a collector's problem is never a run's problem.**

An agent run has a budget, a lease and a person waiting. None of them should be
spent on a metrics backend, so every path here is checked against a telemetry
object that fails on purpose.

The second theme is what may be recorded at all. The settings pin
``record_prompt_body`` and ``record_tool_result_body`` to False, and the
attribute type is what enforces that rather than anybody remembering it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_workbench.adapters.telemetry.otel import OtelTelemetry
from agent_workbench.bootstrap.projections import ObservabilityConfig
from agent_workbench.bootstrap.telemetry_factory import build_telemetry
from agent_workbench.ports.telemetry import (
    RUN_COMPLETED,
    RUN_DURATION,
    RUN_FAILED,
    RUN_STARTED,
    NullTelemetry,
    Telemetry,
)


class _Recording:
    """Remembers what it was told, so a test can read it back."""

    def __init__(self) -> None:
        self.spans: list[str] = []
        self.counts: list[tuple[str, int, dict[str, Any]]] = []
        self.records: list[tuple[str, float, dict[str, Any]]] = []

    def span(self, name: str, *, attributes: Any = None) -> Any:
        self.spans.append(name)
        from contextlib import nullcontext

        del attributes
        return nullcontext()

    def count(self, name: str, *, value: int = 1, attributes: Any = None) -> None:
        self.counts.append((name, value, dict(attributes or {})))

    def record(self, name: str, value: float, *, attributes: Any = None) -> None:
        self.records.append((name, value, dict(attributes or {})))


class _Exploding:
    """Every instrument this hands out raises. Nothing may escape."""

    def create_counter(self, name: str) -> Any:
        del name
        raise RuntimeError("the collector is unreachable")

    def create_histogram(self, name: str, unit: str = "") -> Any:
        del name, unit
        raise RuntimeError("the collector is unreachable")

    def start_as_current_span(self, name: str, attributes: Any = None) -> Any:
        del name, attributes
        raise RuntimeError("the collector is unreachable")


# --------------------------------------------------------------------------
# The default records nothing, and cannot fail
# --------------------------------------------------------------------------


def test_the_default_is_silent_and_satisfies_the_port() -> None:
    """A deployment with no collector is not one that behaves differently."""

    telemetry: Telemetry = NullTelemetry()

    with telemetry.span("anything"):
        telemetry.count("a")
        telemetry.record("b", 1.0)

    assert isinstance(telemetry, Telemetry)


# --------------------------------------------------------------------------
# A broken collector is not a broken run
# --------------------------------------------------------------------------


def test_a_failing_instrument_does_not_reach_the_caller() -> None:
    telemetry = OtelTelemetry(tracer=_Exploding(), meter=_Exploding())

    telemetry.count("agent.run.started")
    telemetry.record("agent.run.duration_ms", 12.0)


def test_a_span_that_cannot_start_still_runs_the_work() -> None:
    """The work is the point; the span is a description of it.

    A span that could not start must not take the body with it, or an
    unreachable collector becomes an outage.
    """

    telemetry = OtelTelemetry(tracer=_Exploding(), meter=_Exploding())
    ran = False

    with telemetry.span("agent.run"):
        ran = True

    assert ran


def test_a_nonsense_endpoint_does_not_stop_a_process_from_starting() -> None:
    """Not a refusal to start.

    Every other factory in bootstrap fails closed, because a process that
    cannot reach its model or its index cannot do its job. One that cannot
    reach its collector can -- it just does it unobserved.

    Measured rather than assumed: ``OTLPSpanExporter`` accepts a malformed
    endpoint without complaint, so this builds a *real* collector pointed at
    nowhere, and what it asserts is that recording against it is still
    harmless. The factory's ``except`` is therefore a guard for exporters that
    do validate, not the thing under test here -- a sabotage round found it
    unreachable from this input, which is worth writing down rather than
    leaving the test's name implying otherwise.
    """

    assembled = build_telemetry(
        ObservabilityConfig(
            service_name="agent-workbench",
            exporter_endpoint="not a url at all",
            trace_sample_ratio=1.0,
            metrics_enabled=True,
        )
    )

    with assembled.telemetry.span("agent.run"):
        assembled.telemetry.count("agent.run.started")
    asyncio.run(assembled.dispose())


def test_no_configuration_is_the_same_answer_as_a_broken_one() -> None:
    """Both mean nothing is collected.

    A process that behaved differently between them would make "is this
    deployment observable" a question about which failure happened.
    """

    assembled = build_telemetry(None)

    assert isinstance(assembled.telemetry, NullTelemetry)
    asyncio.run(assembled.dispose())


# --------------------------------------------------------------------------
# What the runtime reports
# --------------------------------------------------------------------------


def _run_once(telemetry: Telemetry, *, fail: bool) -> Any:
    from agent_workbench.adapters.events import ScopedEventSink
    from agent_workbench.adapters.memory.event_log import InMemoryEventLog
    from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
    from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
    from agent_workbench.adapters.tools import StaticToolRegistry
    from agent_workbench.domain.messages import user_message
    from agent_workbench.domain.policies import (
        AuthorizationEnvelope,
        PrincipalContext,
    )
    from agent_workbench.domain.runs import AgentRunRequest, RunBudget, TraceContext
    from agent_workbench.ports.cancellation import NullCancellationToken
    from agent_workbench.ports.event_log import EventScope
    from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway

    registry = StaticToolRegistry([])
    runtime = ClaudeLikeAgentRuntime(
        model=FakeModel([ScriptedTurn(text="an answer")]),
        gateway=ToolGateway(
            registry=registry, policy=EnvelopePolicyEngine(registry=registry)
        ),
        policy_identity="policy-1:ffff",
        telemetry=telemetry,
    )
    request = AgentRunRequest(
        run_kind="chat",
        model_profile="main",
        messages=(user_message("hello"),),
        # A cost ceiling this runtime cannot enforce is the cheapest way to
        # reach a terminal failure without inventing a broken model.
        budget=(
            RunBudget(max_steps=2, max_tool_calls=2)
            if not fail
            else RunBudget(max_steps=2, max_tool_calls=2, max_cost_micro_usd=10)
        ),
        trace=TraceContext(agent_run_id="run_1"),
        stream_id="stream_1",
        principal=PrincipalContext(tenant_id="tenant_a", principal_id="user_1"),
        envelope=AuthorizationEnvelope(),
    )
    sink = ScopedEventSink(
        InMemoryEventLog(), EventScope(stream_id="stream_1", run_id="run_1")
    )
    return asyncio.run(runtime.run(request, sink, NullCancellationToken()))


def test_a_run_reports_that_it_started_and_how_it_ended() -> None:
    telemetry = _Recording()

    outcome = _run_once(telemetry, fail=False)

    assert outcome.status == "completed"
    assert "agent.run" in telemetry.spans
    names = [name for name, _, _ in telemetry.counts]
    assert RUN_STARTED in names
    assert RUN_COMPLETED in names
    assert RUN_FAILED not in names


def test_a_failed_run_is_counted_as_one() -> None:
    """Success rate is a DoD metric, and it needs both halves to mean anything."""

    telemetry = _Recording()

    outcome = _run_once(telemetry, fail=True)

    assert outcome.status == "failed"
    names = [name for name, _, _ in telemetry.counts]
    assert RUN_FAILED in names
    assert RUN_COMPLETED not in names


def test_a_run_records_its_duration_and_what_it_spent() -> None:
    telemetry = _Recording()

    _run_once(telemetry, fail=False)

    measured = {name for name, _, _ in telemetry.records}
    assert RUN_DURATION in measured
    assert "agent.model.input_tokens" in measured
    assert "agent.model.output_tokens" in measured


def test_nothing_recorded_carries_a_prompt_or_an_answer() -> None:
    """``record_prompt_body`` and ``record_tool_result_body`` are pinned False.

    Enforced by the attribute type rather than by anybody remembering: the
    values are bounded scalars, so there is no parameter a body could arrive
    through. This asserts the result on a real run.
    """

    telemetry = _Recording()

    _run_once(telemetry, fail=False)

    every_value = [
        str(value)
        for _, _, attributes in telemetry.counts + telemetry.records  # type: ignore[operator]
        for value in attributes.values()
    ]
    assert not any("hello" in value for value in every_value)
    assert not any("an answer" in value for value in every_value)


@pytest.mark.parametrize("failing", [True, False])
def test_a_run_completes_whatever_the_collector_does(failing: bool) -> None:
    """The claim this whole file is about, on the real runtime."""

    telemetry: Telemetry = (
        OtelTelemetry(tracer=_Exploding(), meter=_Exploding())
        if failing
        else NullTelemetry()
    )

    outcome = _run_once(telemetry, fail=False)

    assert outcome.status == "completed"
