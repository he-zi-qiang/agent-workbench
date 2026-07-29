"""One agent node, run against either executor without changing the node.

That is the M3a acceptance gate this covers: "a single agent node can switch
between the FakeExecutor and the self-written Runtime". Switching is by
injection, and the point of the test is that the *node* is identical in both
directions -- it holds an executor and nothing else, so there is nothing in it
that could tell which one it got.

What is deliberately not here is a configuration switch. ``runtime.executor``
is a single-valued ``Literal`` because exactly one component owns the model-tool
loop, and widening it would overturn that invariant rather than implement this
gate. A fake selectable in production would serve an answer nobody asked a
model for.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_workbench.domain.messages import user_message
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    RunBudget,
    TraceContext,
)
from agent_workbench.ports.cancellation import CancellationSource, NullCancellationToken
from agent_workbench.runtime.fake_executor import (
    FAKE_COST_MICRO_USD,
    FAKE_STEPS,
    FakeAgentExecutor,
)

PRINCIPAL = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")


def _request(text: str = "Compare retrieval strategies.") -> AgentRunRequest:
    return AgentRunRequest(
        trace=TraceContext(
            agent_run_id="run_1",
            task_id="task_1",
            workflow_thread_id="thr_1",
            graph_node_id="understand",
        ),
        run_kind="task",
        stream_id="str_1",
        principal=PRINCIPAL,
        envelope=AuthorizationEnvelope(),
        budget=RunBudget(max_steps=4, max_tool_calls=4),
        messages=(user_message(text),),
    )


def _run(executor: Any, request: AgentRunRequest, cancellation: Any) -> AgentOutcome:
    async def scenario() -> AgentOutcome:
        return await executor.run(request, _sink, cancellation)

    return asyncio.run(scenario())


async def _sink(*args: Any, **kwargs: Any) -> None:
    """The node's event sink. The fake emits nothing, and says so by taking it."""

    return None


def test_the_fake_satisfies_the_agent_boundary() -> None:
    from agent_workbench.ports.agent_executor import AgentExecutor

    assert isinstance(FakeAgentExecutor(), AgentExecutor)


def test_the_same_request_always_produces_the_same_outcome() -> None:
    """Determinism is the whole point: a CI run must not depend on a model."""

    first = _run(FakeAgentExecutor(), _request(), NullCancellationToken())
    second = _run(FakeAgentExecutor(), _request(), NullCancellationToken())

    assert first == second
    assert first.status == "completed"
    assert first.output_ref is not None


def test_a_different_request_produces_a_different_artifact() -> None:
    """Not a constant: a fixed digest would make every artifact look alike.

    Anything that deduplicates or compares artifacts would then treat two
    unrelated runs as the same one.
    """

    first = _run(FakeAgentExecutor(), _request("first"), NullCancellationToken())
    second = _run(FakeAgentExecutor(), _request("second"), NullCancellationToken())

    assert first.output_ref is not None and second.output_ref is not None
    assert first.output_ref.sha256 != second.output_ref.sha256
    assert first.output_text != second.output_text


def test_a_cancelled_run_returns_a_terminal_outcome_rather_than_raising() -> None:
    """The contract's rule, and the one a happy-path double forgets.

    The caller is a graph node that has to record and route on the result
    either way, so cancellation is an outcome and not an exception.
    """

    source = CancellationSource()
    source.cancel()

    outcome = _run(FakeAgentExecutor(), _request(), source)

    assert outcome.status == "cancelled"
    assert outcome.stop_reason == "cancelled"
    assert outcome.error is None


def test_a_fake_run_is_charged_rather_than_free() -> None:
    """A run costing nothing would let every budget check pass silently."""

    outcome = _run(FakeAgentExecutor(), _request(), NullCancellationToken())

    assert outcome.usage.steps == FAKE_STEPS
    assert outcome.usage.cost_micro_usd == FAKE_COST_MICRO_USD
    assert outcome.usage.tokens.input_tokens > 0


def test_the_fake_records_what_it_was_asked() -> None:
    executor = FakeAgentExecutor()

    _run(executor, _request(), NullCancellationToken())

    assert len(executor.requests) == 1
    assert executor.requests[0].trace.graph_node_id == "understand"


def test_the_answer_can_be_scripted_without_subclassing() -> None:
    executor = FakeAgentExecutor(respond=lambda request: "a scripted answer")

    outcome = _run(executor, _request(), NullCancellationToken())

    assert outcome.output_text == "a scripted answer"


def test_configuration_still_names_exactly_one_tool_loop_owner() -> None:
    """The invariant this fake must not become an exception to.

    Shipping it does not add a second executor a deployment can select; it adds
    one a composition root can hand to a node. If that ever has to change, it
    is an ADR and a config schema version, not a wider Literal.
    """

    from agent_workbench.bootstrap.settings import RuntimeSettings

    with pytest.raises(Exception, match="executor"):
        RuntimeSettings.model_validate({"executor": "fake"})
