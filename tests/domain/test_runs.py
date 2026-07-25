"""Run identity, budget arithmetic and terminal-outcome consistency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.messages import (
    Message,
    TextBlock,
    assistant_message,
    user_message,
)
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    RunBudget,
    TokenUsage,
    TraceContext,
)

DEADLINE = datetime(2026, 7, 25, 4, 0, 0, tzinfo=UTC)
PRINCIPAL = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")
ENVELOPE = AuthorizationEnvelope(allowed_tools=("knowledge_search",))


def _budget(**overrides: object) -> RunBudget:
    defaults: dict[str, object] = {"max_steps": 4, "max_tool_calls": 8}
    return RunBudget.model_validate(defaults | overrides)


def _request(**overrides: object) -> AgentRunRequest:
    defaults: dict[str, object] = {
        "trace": TraceContext(agent_run_id="run_1"),
        "run_kind": "chat",
        "stream_id": "stream_1",
        "principal": PRINCIPAL,
        "envelope": ENVELOPE,
        "budget": _budget(),
        "messages": (user_message("hello"),),
    }
    return AgentRunRequest.model_validate(defaults | overrides)


def test_tool_call_ceiling_cannot_sit_below_the_step_ceiling() -> None:
    with pytest.raises(ValidationError, match="max_tool_calls must be >="):
        _budget(max_steps=8, max_tool_calls=4)


def test_a_fresh_run_is_allowed_to_start() -> None:
    assert _budget().stop_reason_for(BudgetUsage()) is None


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (BudgetUsage(steps=4), "max_steps"),
        (BudgetUsage(tool_calls=8), "max_tool_calls"),
        (BudgetUsage(tokens=TokenUsage(input_tokens=1000)), "token_budget"),
        (BudgetUsage(cost_micro_usd=5000), "cost_budget"),
    ],
)
def test_each_ceiling_stops_the_run(usage: BudgetUsage, expected: str) -> None:
    budget = _budget(max_total_tokens=1000, max_cost_micro_usd=5000)

    assert budget.stop_reason_for(usage) == expected


def test_the_deadline_needs_an_explicit_clock() -> None:
    """Budget checks stay deterministic; the caller supplies the time."""

    budget = _budget(deadline=DEADLINE)

    assert budget.stop_reason_for(BudgetUsage()) is None
    assert budget.stop_reason_for(BudgetUsage(), now=DEADLINE - timedelta(1)) is None
    assert budget.stop_reason_for(BudgetUsage(), now=DEADLINE) == "deadline"


def test_token_usage_counts_cache_traffic() -> None:
    usage = TokenUsage(
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=300,
        cache_write_tokens=5,
    )

    assert usage.total == 425


def test_child_usage_aggregates_into_the_parent() -> None:
    parent = BudgetUsage(steps=1, tool_calls=2, cost_micro_usd=10)
    child = BudgetUsage(
        steps=3,
        tool_calls=1,
        tokens=TokenUsage(input_tokens=50),
        cost_micro_usd=7,
    )

    merged = parent.merged(child)

    assert (merged.steps, merged.tool_calls, merged.cost_micro_usd) == (4, 3, 17)
    assert merged.tokens.input_tokens == 50


def test_a_graph_node_only_exists_inside_a_task_thread() -> None:
    with pytest.raises(ValidationError, match="task workflow thread"):
        TraceContext(agent_run_id="run_1", graph_node_id="node_plan")

    nested = TraceContext(
        agent_run_id="run_1",
        task_id="task_1",
        workflow_thread_id="thread_1",
        graph_node_id="node_plan",
    )
    assert nested.parent_agent_run_id is None


def test_system_content_has_exactly_one_home() -> None:
    """Two sources would make the effective instructions adapter-dependent."""

    system_turn = Message(role="system", content=(TextBlock(text="be terse"),))

    with pytest.raises(ValidationError, match="system_prompt"):
        _request(messages=(system_turn, user_message("hello")))


def test_a_run_starts_from_a_user_message_or_tool_results() -> None:
    with pytest.raises(ValidationError, match="user message or from tool results"):
        _request(messages=(assistant_message(text="I spoke last"),))


def test_a_run_needs_at_least_one_message() -> None:
    with pytest.raises(ValidationError, match="at least one message"):
        _request(messages=())


def test_tool_names_must_not_repeat() -> None:
    with pytest.raises(ValidationError, match="must not repeat"):
        _request(tool_names=("knowledge_search", "knowledge_search"))


def test_a_completed_outcome_carries_no_error() -> None:
    with pytest.raises(ValidationError, match="carries no error"):
        AgentOutcome(
            agent_run_id="run_1",
            status="completed",
            stop_reason="completed",
            error=ErrorInfo(code="tool_failed", message="but reported success"),
        )


def test_a_budget_stop_is_reported_as_a_failure() -> None:
    """Truncated work must not look complete to the node that consumes it."""

    with pytest.raises(ValidationError, match="stops for completion"):
        AgentOutcome(
            agent_run_id="run_1",
            status="completed",
            stop_reason="max_steps",
        )

    failed = AgentOutcome(
        agent_run_id="run_1",
        status="failed",
        stop_reason="max_steps",
        error=ErrorInfo(code="budget_exceeded", message="step ceiling reached"),
    )
    assert failed.stop_reason == "max_steps"


def test_a_failed_outcome_needs_an_error() -> None:
    with pytest.raises(ValidationError, match="carries an ErrorInfo"):
        AgentOutcome(agent_run_id="run_1", status="failed", stop_reason="error")


def test_a_cancelled_outcome_stops_for_cancellation() -> None:
    cancelled = AgentOutcome(
        agent_run_id="run_1",
        status="cancelled",
        stop_reason="cancelled",
    )
    assert cancelled.error is None

    with pytest.raises(ValidationError, match="stops for cancellation"):
        AgentOutcome(agent_run_id="run_1", status="cancelled", stop_reason="error")
