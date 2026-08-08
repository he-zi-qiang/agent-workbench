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
    stale_execution_outcome,
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


def test_a_tool_ceiling_below_the_step_ceiling_is_a_budget_not_an_error() -> None:
    """ADR-022. "Four tool calls, and four more turns to answer from them."

    This combination used to be rejected outright, on the reading that a step
    unable to call a tool was a misconfiguration. Under the loop that *ended*
    a run when its tool calls ran out, that reading was right and the two
    ceilings could never be set independently anyway: one call per turn reaches
    both at the same turn and `max_steps` is reported first, so a lower tool
    ceiling was unreachable by construction.

    The loop now closes the toolbox instead of ending the run, which makes the
    spare steps answering turns. Refusing this budget would refuse the only
    way to say "search twice, then answer" -- the sentence the chat fallback's
    prompt has always contained and its budget could never enforce.
    """

    budget = _budget(max_steps=8, max_tool_calls=4)

    assert budget.max_steps == 8
    assert budget.max_tool_calls == 4
    # The step ceiling still bounds the loop; nothing here is unbounded.
    assert budget.halt_reason_for(BudgetUsage(steps=8)) == "max_steps"


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


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (BudgetUsage(steps=4), "max_steps"),
        (BudgetUsage(tokens=TokenUsage(input_tokens=1000)), "token_budget"),
        (BudgetUsage(cost_micro_usd=5000), "cost_budget"),
    ],
)
def test_each_ceiling_that_ends_a_run_ends_it(
    usage: BudgetUsage, expected: str
) -> None:
    """`halt_reason_for` agrees with `stop_reason_for` on every shared ceiling.

    Written as the same table minus one row, because the two must not drift:
    every limit that stops more work also ends the run, except the one below.
    """

    budget = _budget(max_total_tokens=1000, max_cost_micro_usd=5000)

    assert budget.halt_reason_for(usage) == expected


def test_a_spent_tool_allowance_does_not_end_the_run() -> None:
    """The one row that differs, and the whole reason the split exists.

    A run out of tool calls can still write its answer from what the calls
    already returned. Ending it there discards exactly the work the allowance
    paid for -- measured on the chat web fallback as two successful searches,
    5.5KB of results, and an answer that said it could not search.
    """

    budget = _budget(max_steps=4, max_tool_calls=8)
    spent = BudgetUsage(steps=1, tool_calls=8)

    assert budget.stop_reason_for(spent) == "max_tool_calls"
    assert budget.halt_reason_for(spent) is None
    assert budget.tool_allowance_spent(spent) is True


def test_an_unspent_allowance_is_not_reported_as_spent() -> None:
    """The control: one call short is not out of calls."""

    budget = _budget(max_steps=4, max_tool_calls=8)

    assert budget.tool_allowance_spent(BudgetUsage(tool_calls=7)) is False
    assert budget.tool_allowance_spent(BudgetUsage(tool_calls=9)) is True


def test_a_run_out_of_steps_ends_even_with_tool_calls_to_spare() -> None:
    """The step ceiling is what bounds the loop once the toolbox is closed.

    Without this, "the tool ceiling no longer ends a run" would be one edit
    away from a run that circles forever proposing nothing.
    """

    budget = _budget(max_steps=4, max_tool_calls=8)

    assert budget.halt_reason_for(BudgetUsage(steps=4, tool_calls=0)) == "max_steps"


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


def test_stale_execution_has_one_fixed_non_retryable_outcome() -> None:
    outcome = stale_execution_outcome("run_1")

    assert outcome.agent_run_id == "run_1"
    assert outcome.status == "failed"
    assert outcome.stop_reason == "deadline"
    assert outcome.error is not None
    assert outcome.error.code == "stale_execution"
    assert outcome.error.message == "execution lease expired"
    assert outcome.error.retryable is False
