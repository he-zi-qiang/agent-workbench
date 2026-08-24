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
    """A cache write is prompt traffic the provider reports on its own line.

    Unlike a cache *read* it is not already inside ``input_tokens``, so it is
    the one cache figure ``total`` still adds.
    """

    usage = TokenUsage(
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_tokens=5,
    )

    assert usage.total == 125


def test_a_cached_prompt_is_not_counted_twice() -> None:
    """``input_tokens`` already contains the cached part; ``total`` cannot re-add it.

    The numbers are the DeepSeek contract's, pinned by
    ``tests/contracts/test_deepseek_model.py``: ``prompt_tokens=118`` with
    ``prompt_cache_hit_tokens=64``, and 24 tokens of answer. That prompt was
    118 tokens long and 64 of those 118 came from cache, so the turn moved 142
    tokens -- not 206.

    The control is the same turn with the cache cold. Caching changes what a
    prompt *costs*, never how long it *was*, so the two must agree; before this
    fix they differed by exactly the cache hit.
    """

    cached = TokenUsage(input_tokens=118, output_tokens=24, cache_read_tokens=64)
    uncached = TokenUsage(input_tokens=118, output_tokens=24)

    assert cached.total == 142
    assert uncached.total == 142


def test_a_cache_report_larger_than_its_prompt_does_not_inflate_the_total() -> None:
    """The same clamp ``ModelPrices.cost_micro_usd`` applies, for the same reason.

    A provider should never report more cached tokens than prompt tokens. If
    one does, the prompt is treated as entirely cached rather than the surplus
    being counted as extra traffic: a total that overstates slightly is a
    better failure than one that grows as the report gets more absurd.

    Its control is the ordinary case, which the clamp must leave alone.
    """

    impossible = TokenUsage(input_tokens=100, output_tokens=20, cache_read_tokens=900)
    ordinary = TokenUsage(input_tokens=900, output_tokens=20, cache_read_tokens=100)

    assert impossible.total == 920
    assert ordinary.total == 920


def test_caching_does_not_bring_the_token_ceiling_forward() -> None:
    """The defect this fixes: ``token_budget`` firing below the configured ceiling.

    ``model.*.prompt_cache_enabled`` defaults to True, so the double count was
    the normal path rather than an edge case. A 200-token ceiling with a
    142-token run has 58 tokens left whether or not the prompt was cached; the
    old sum read 206 and ended the run as ``token_budget``.

    Controls on both sides: the uncached twin must also continue, and a run
    that genuinely passes the ceiling must still stop -- a ``total`` that
    halted nothing would satisfy the first two assertions just as well.
    """

    budget = _budget(max_total_tokens=200)
    cached = BudgetUsage(
        tokens=TokenUsage(input_tokens=118, output_tokens=24, cache_read_tokens=64)
    )
    uncached = BudgetUsage(tokens=TokenUsage(input_tokens=118, output_tokens=24))
    overrun = BudgetUsage(
        tokens=TokenUsage(input_tokens=190, output_tokens=24, cache_read_tokens=64)
    )

    assert budget.halt_reason_for(cached) is None
    assert budget.halt_reason_for(uncached) is None
    assert budget.halt_reason_for(overrun) == "token_budget"


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


def test_a_lease_epoch_only_exists_inside_a_task() -> None:
    """An epoch is a claim on a Task, so one without a Task names nothing.

    Caught at the trace rather than at the side-effect ledger, where the same
    mistake would arrive much later and in a much worse disguise: a refused
    tool call, in a node whose context was mis-assembled several layers up.
    """

    with pytest.raises(ValidationError, match="only inside a task"):
        TraceContext(agent_run_id="run_1", lease_epoch=1)

    with pytest.raises(ValidationError):
        TraceContext(agent_run_id="run_1", task_id="task_1", lease_epoch=0)

    claimed = TraceContext(
        agent_run_id="run_1",
        task_id="task_1",
        workflow_thread_id="thread_1",
        graph_node_id="work",
        lease_epoch=3,
    )
    assert claimed.lease_epoch == 3


def test_a_run_nobody_leased_carries_no_epoch() -> None:
    """Absent is a real state, and it reads as "may record no external effect".

    Every chat run is in it, and so is any task run assembled outside a claim.
    A default of 1 would be a lease over nothing that still passes a fence.
    """

    assert TraceContext(agent_run_id="run_1").lease_epoch is None


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


# --- a code run is not a Task, and the type says so ---------------------------


def _code_request(**overrides: object) -> dict[str, object]:
    """The smallest legal code run, as keyword arguments a test can bend."""

    base: dict[str, object] = {
        "trace": TraceContext(agent_run_id="run_1"),
        "run_kind": "code",
        "stream_id": "ses_1",
        "principal": PrincipalContext(principal_id="user_1", tenant_id="tenant_a"),
        "envelope": AuthorizationEnvelope(),
        "budget": RunBudget(
            max_steps=4,
            max_tool_calls=4,
            deadline=datetime(2026, 8, 13, tzinfo=UTC),
        ),
        "messages": (user_message("do the thing"),),
    }
    base.update(overrides)
    return base


def test_a_code_run_may_not_carry_a_task_position() -> None:
    """Its events would land on a timeline the Registry never opened."""

    for position in ("task_id", "workflow_thread_id"):
        with pytest.raises(ValidationError):
            AgentRunRequest(
                **_code_request(
                    trace=TraceContext(agent_run_id="run_1", **{position: "id_1"}),
                )
            )


def test_a_code_run_must_carry_a_deadline() -> None:
    """No lease, no reaper, no invocation budget -- the clock is all there is."""

    with pytest.raises(ValidationError):
        AgentRunRequest(
            **_code_request(budget=RunBudget(max_steps=4, max_tool_calls=4))
        )


def test_the_two_code_rules_do_not_touch_the_other_run_kinds() -> None:
    """The control, and it is two-sided.

    Without it, a validator that refused *every* request would satisfy both
    assertions above while breaking every Task and Chat run in the system.
    """

    task = AgentRunRequest(
        **_code_request(
            run_kind="task",
            trace=TraceContext(
                agent_run_id="run_1",
                task_id="task_1",
                workflow_thread_id="thread_1",
                graph_node_id="work",
            ),
            budget=RunBudget(max_steps=4, max_tool_calls=4),
        )
    )
    chat = AgentRunRequest(
        **_code_request(
            run_kind="chat",
            budget=RunBudget(max_steps=4, max_tool_calls=4),
        )
    )
    code = AgentRunRequest(**_code_request())

    assert (task.run_kind, chat.run_kind, code.run_kind) == ("task", "chat", "code")
    # A task run with no deadline is still legal: what bounds it is the lease
    # and the invocation budget, neither of which a code run has.
    assert task.budget.deadline is None
