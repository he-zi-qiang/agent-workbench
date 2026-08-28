"""The delegating run's own position: where it comes from, and what it cuts.

Two subjects. The scope is a ``ContextVar``, so the properties worth pinning are
the ones a plain attribute would get wrong: an unentered scope refuses instead of
inventing a parent, a nested scope restores rather than clears, and two runs
racing inside ``asyncio.gather`` do not see each other at all. That last one is
not a theoretical concern -- the whole reason a ``ContextVar`` is used here
rather than a field on the tool is that this project runs read tools in parallel.

The budget is the other subject, and the interesting thing about it is which
ceilings are *not* divided. Steps and tool calls pass down whole because ADR-030
made them backstops rather than budgets, and a backstop divided by four fires on
ordinary work. Tokens and money are divided because they are money.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from agent_workbench.application.delegation import (
    DelegationContext,
    DelegationScope,
    SpawnedChild,
    build_child_request,
    clip_report,
    derive_child_budget,
)
from agent_workbench.application.sub_agents import ANALYST, RESEARCHER
from agent_workbench.domain.agents import DELEGATE_TOOL
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.runs import BudgetUsage, RunBudget, TokenUsage
from agent_workbench.ports.delegation import DelegationChannel, RecordOutcome
from agent_workbench.ports.event_log import EventSink

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class _SilentChannel:
    """Enough of a ``DelegationChannel`` for the assertions in this file."""

    @asynccontextmanager
    async def delegating(
        self, *, child_agent_run_id: str, definition_name: str
    ) -> AsyncGenerator[RecordOutcome]:
        yield lambda outcome: None

    def sink_for_child(self, child_agent_run_id: str) -> EventSink:
        raise AssertionError("no run is started in this module")


def _context(**overrides: object) -> DelegationContext:
    fields: dict[str, object] = {
        "stream_id": "stream_1",
        "run_kind": "task",
        "budget": RunBudget(max_steps=12, max_tool_calls=8),
        "channel": _SilentChannel(),
    }
    fields.update(overrides)
    return DelegationContext(**fields)  # type: ignore[arg-type]


def _execution() -> ExecutionContext:
    return ExecutionContext(
        principal=PrincipalContext(principal_id="user_1", tenant_id="tenant_a"),
        envelope=AuthorizationEnvelope(
            allowed_tools=("knowledge_search", DELEGATE_TOOL),
        ),
        agent_run_id="run_parent",
        policy_identity="rev_1:abcdef",
    )


class TestAnUnenteredScopeSaysSo:
    def test_the_double_really_is_a_delegation_channel(self) -> None:
        """Otherwise every assertion below is about a shape nothing implements.

        ``DelegationChannel`` is ``runtime_checkable``, so this costs one line
        and catches the case where the port grows a verb and the double stops
        standing in for anything.
        """

        assert isinstance(_SilentChannel(), DelegationChannel)

    def test_an_unentered_scope_refuses_rather_than_inventing_a_parent(self) -> None:
        """``None`` is the answer, and it is a real one.

        Every field a child needs that ``ExecutionContext`` does not carry --
        the stream, the run kind, the budget, the depth -- would have to be
        guessed. A run assembled from guesses is indistinguishable in the event
        log from one somebody authorized.
        """

        assert DelegationScope().current() is None

    def test_a_scope_is_restored_on_the_way_out_not_cleared(self) -> None:
        """Nested delegation contexts are a real shape -- the outer run is
        still going while the inner one exists -- and clearing would make the
        outer one lose its own spawn count the moment the inner one finished.
        """

        scope = DelegationScope()
        outer = _context(stream_id="outer")
        inner = _context(stream_id="inner")

        with scope.using(outer):
            assert scope.current() is outer
            with scope.using(inner):
                assert scope.current() is inner
            assert scope.current() is outer

        assert scope.current() is None

    def test_two_concurrent_parents_do_not_see_each_others_children(self) -> None:
        """The reason this is a ContextVar and not an attribute.

        Two runs are live in one process constantly here -- the graph fans out
        to two researchers -- and read tools run in parallel inside a single
        run. A shared attribute would let one run's spawn count refuse the
        other's first delegation.
        """

        scope = DelegationScope()
        observed: dict[str, str | None] = {}

        async def run(name: str) -> None:
            with scope.using(_context(stream_id=name)):
                # Yield in the middle, so the two tasks are interleaved rather
                # than merely sequential: without the await this passes even if
                # the scope were a plain attribute.
                await asyncio.sleep(0)
                current = scope.current()
                observed[name] = None if current is None else current.stream_id

        async def scenario() -> None:
            await asyncio.gather(run("alpha"), run("beta"))

        asyncio.run(scenario())

        assert observed == {"alpha": "alpha", "beta": "beta"}


class TestTheChildCountIsTheRunsNotTheCalls:
    def test_a_run_that_has_spent_its_allowance_may_not_delegate_again(self) -> None:
        context = _context(max_children=2)

        assert context.may_delegate()
        for index in range(2):
            held = context.reserve()
            assert held is not None
            held.fulfil(
                SpawnedChild(
                    definition_name="analyst",
                    child_agent_run_id=f"run_child_{index}",
                    usage=BudgetUsage(),
                )
            )

        assert not context.may_delegate()
        assert context.children_remaining() == 0

    def test_a_place_is_taken_before_the_child_runs_not_after_it_finishes(
        self,
    ) -> None:
        """The whole reason ``reserve`` exists rather than a plain count.

        ``delegate_agent`` is declared ``read``, so several calls in one turn
        land in one parallel group and start inside a single ``gather``. A
        handler that read ``len(spawned)`` on the way in would get zero in all
        of them.
        """

        context = _context(max_children=2)

        first = context.reserve()
        second = context.reserve()
        third = context.reserve()

        assert first is not None
        assert second is not None
        assert third is None, "the third call must not find a place still free"

    def test_a_released_place_goes_back_into_the_allowance(self) -> None:
        """A child that never ran must not consume the run's allowance."""

        context = _context(max_children=1)
        held = context.reserve()
        assert held is not None
        assert context.reserve() is None

        held.release()

        assert context.reserve() is not None

    def test_a_fulfilled_place_becomes_a_record_of_what_was_spent(self) -> None:
        context = _context(max_children=2)
        held = context.reserve()
        assert held is not None

        held.fulfil(
            SpawnedChild(
                definition_name="analyst",
                child_agent_run_id="run_child",
                usage=BudgetUsage(steps=2),
            )
        )

        assert context.outstanding == 0
        assert [child.child_agent_run_id for child in context.spawned] == ["run_child"]

    def test_a_place_settled_twice_is_a_bug_and_says_so(self) -> None:
        """Releasing after fulfilling would hand the allowance back twice, and
        the run would quietly get more children than it was given."""

        context = _context()
        held = context.reserve()
        assert held is not None
        held.release()

        with pytest.raises(RuntimeError, match="settled twice"):
            held.release()

    def test_a_run_already_at_the_depth_ceiling_may_not_delegate_at_all(self) -> None:
        """Belt and braces beside ``permitted_child_tools``: that one takes the
        tool away, and this refuses the call if a deployment ever advertises it
        anyway."""

        assert not _context(depth=1, max_depth=1).may_delegate()
        assert _context(depth=0, max_depth=1).may_delegate()

    def test_what_the_children_spent_is_aggregated_where_it_can_be_read(
        self,
    ) -> None:
        """The second ledger. The parent's own is private to the runtime loop
        and a ToolResult carries no usage, so this is the only place the number
        exists on the sending side."""

        context = _context()
        for index in range(3):
            held = context.reserve()
            assert held is not None
            held.fulfil(
                SpawnedChild(
                    definition_name="analyst",
                    child_agent_run_id=f"run_child_{index}",
                    usage=BudgetUsage(
                        steps=2,
                        tokens=TokenUsage(input_tokens=100, output_tokens=50),
                    ),
                )
            )

        spent = context.spent()

        assert spent.steps == 6
        assert spent.tokens.input_tokens == 300
        assert spent.tokens.output_tokens == 150


class TestTheBudgetIsCutFromWhatTheParentHasLeft:
    def test_a_child_deadline_never_outlives_the_run_that_sent_it(self) -> None:
        parent = RunBudget(
            max_steps=12,
            max_tool_calls=8,
            deadline=NOW + timedelta(seconds=30),
        )

        child = derive_child_budget(
            parent, children_allowed=4, timeout_seconds=600, now=NOW
        )

        assert child.deadline == parent.deadline

    def test_a_child_of_a_deadlineless_parent_still_gets_one(self) -> None:
        """``RunBudget.deadline`` defaults to ``None`` meaning "no deadline",
        and a delegated run is exactly the kind that must not inherit that: it
        is awaited inside a tool call, so a child without a wall clock is a
        parent stuck in ``executing_tools`` forever."""

        child = derive_child_budget(
            RunBudget(max_steps=12, max_tool_calls=8),
            children_allowed=4,
            timeout_seconds=600,
            now=NOW,
        )

        assert child.deadline == NOW + timedelta(seconds=600)

    def test_the_tighter_of_the_two_clocks_is_the_one_that_survives(self) -> None:
        """Swept, because "min" is the kind of thing that is written as "max"
        exactly once and then never looked at again."""

        for parent_seconds in (10, 300, 600, 1200):
            for timeout in (10, 300, 600, 1200):
                child = derive_child_budget(
                    RunBudget(
                        max_steps=12,
                        max_tool_calls=8,
                        deadline=NOW + timedelta(seconds=parent_seconds),
                    ),
                    children_allowed=4,
                    timeout_seconds=timeout,
                    now=NOW,
                )
                expected = NOW + timedelta(seconds=min(parent_seconds, timeout))
                assert child.deadline == expected, (
                    f"parent={parent_seconds}s timeout={timeout}s"
                )

    def test_money_is_divided_and_the_backstops_are_not(self) -> None:
        """The asymmetry this function exists to express.

        A child given three steps reports that it could not finish, and that
        reads to whoever is looking as the sub-agent being incapable rather
        than as the budget being wrong.
        """

        parent = RunBudget(
            max_steps=40,
            max_tool_calls=20,
            max_total_tokens=120_000,
            max_cost_micro_usd=8_000,
        )

        child = derive_child_budget(
            parent, children_allowed=4, timeout_seconds=600, now=NOW
        )

        assert child.max_steps == 40
        assert child.max_tool_calls == 20
        assert child.max_total_tokens == 30_000
        assert child.max_cost_micro_usd == 2_000

    def test_an_absent_ceiling_is_not_turned_into_a_present_one(self) -> None:
        """Dividing ``None`` by four must stay ``None``: a ceiling this
        deployment never set must not appear because a delegation happened."""

        child = derive_child_budget(
            RunBudget(max_steps=12, max_tool_calls=8),
            children_allowed=4,
            timeout_seconds=600,
            now=NOW,
        )

        assert child.max_total_tokens is None
        assert child.max_cost_micro_usd is None

    def test_a_share_too_small_to_express_is_one_not_zero(self) -> None:
        """``RunBudget`` refuses a ceiling below 1, so integer division has to
        floor at one rather than construct an invalid budget."""

        child = derive_child_budget(
            RunBudget(max_steps=12, max_tool_calls=8, max_total_tokens=3),
            children_allowed=32,
            timeout_seconds=600,
            now=NOW,
        )

        assert child.max_total_tokens == 1


class TestTheChildRequestNarrowsOnEveryAxis:
    def test_the_child_points_at_the_run_that_sent_it(self) -> None:
        """The field this whole module exists to give a writer to."""

        request = build_child_request(
            ANALYST,
            "think about this",
            context=_context(),
            execution=_execution(),
            child_agent_run_id="run_child",
            budget=RunBudget(max_steps=4, max_tool_calls=2, deadline=NOW),
        )

        assert request.trace.parent_agent_run_id == "run_parent"
        assert request.trace.agent_run_id == "run_child"

    def test_the_child_writes_to_the_stream_its_parent_writes_to(self) -> None:
        """Everything that reads events reads them by stream: the SSE route
        authorizes one, the timeline pages through one. A child on its own
        stream would be a child nothing in the product could show."""

        request = build_child_request(
            ANALYST,
            "think",
            context=_context(stream_id="stream_7"),
            execution=_execution(),
            child_agent_run_id="run_child",
            budget=RunBudget(max_steps=4, max_tool_calls=2, deadline=NOW),
        )

        assert request.stream_id == "stream_7"

    def test_the_prompt_reaches_the_child_as_a_user_message_and_nothing_else(
        self,
    ) -> None:
        """The one thing a model writes that travels. It cannot become a system
        instruction, which is what stops "ignore your brief" in a retrieved
        passage from being an instruction the child's constitution carries."""

        request = build_child_request(
            ANALYST,
            "think about this",
            context=_context(),
            execution=_execution(),
            child_agent_run_id="run_child",
            budget=RunBudget(max_steps=4, max_tool_calls=2, deadline=NOW),
        )

        assert len(request.messages) == 1
        assert request.messages[0].role == "user"
        assert request.system_prompt == ANALYST.system_prompt

    def test_a_toolless_definition_produces_a_run_with_no_tools(self) -> None:
        request = build_child_request(
            ANALYST,
            "think",
            context=_context(),
            execution=_execution(),
            child_agent_run_id="run_child",
            budget=RunBudget(max_steps=4, max_tool_calls=2, deadline=NOW),
        )

        assert request.tool_names == ()
        assert request.envelope.allowed_tools == ()

    def test_the_child_is_not_handed_the_tool_that_created_it(self) -> None:
        """End to end through the request builder, not only through the domain
        function: the parent here really is allowed ``delegate_agent``."""

        request = build_child_request(
            RESEARCHER,
            "find out",
            context=_context(depth=0, max_depth=1),
            execution=_execution(),
            child_agent_run_id="run_child",
            budget=RunBudget(max_steps=4, max_tool_calls=2, deadline=NOW),
        )

        assert DELEGATE_TOOL not in request.tool_names
        assert DELEGATE_TOOL not in request.envelope.allowed_tools


class TestAClippedReportSaysSo:
    def test_a_report_within_the_ceiling_is_untouched(self) -> None:
        text, clipped = clip_report("short", 100)

        assert (text, clipped) == ("short", False)

    def test_a_report_over_the_ceiling_reports_that_it_was_cut(self) -> None:
        """Both halves matter: the caller needs the flag to say so in the
        report's own text, because a truncated final sentence with nothing
        marking it reads as a finished thought."""

        text, clipped = clip_report("x" * 200, 100)

        assert clipped is True
        assert len(text) == 100


class TestAReportIsClippedFromTheEnd:
    """So a sub-agent must put its conclusion at the start.

    `clip_report` keeps `text[:limit]`. Whatever a sub-agent leaves for last is
    therefore what the parent never receives, and a sub-agent cannot see the
    limit it is being cut at.

    Measured 2026-08-28 on a real delegating Task: three analysts each returned
    a complete enumeration of failure modes and each was cut at 8,000
    characters exactly where its conclusion section began. The parent said so
    itself in its report -- then spent three more delegations asking for the
    conclusions alone, and ran into `max_children_per_run`. One truncation
    turned one round of delegation into two and then blocked the second.
    """

    def test_the_clip_takes_the_tail_not_the_head(self) -> None:
        report, clipped = clip_report("ABCDEFGH", 3)

        assert report == "ABC"
        assert clipped is True

    def test_analyst_is_told_to_lead_with_its_conclusion(self) -> None:
        prompt = ANALYST.system_prompt

        assert "Open with your conclusion" in prompt
        # And why, because a bare instruction is what gets reworded away by
        # whoever next tidies this prompt.
        assert "cut off at a length you cannot see" in prompt

    def test_researcher_is_told_the_same_thing(self) -> None:
        """The other definition is clipped by exactly the same function."""

        assert "Lead with the answer itself" in RESEARCHER.system_prompt

    def test_a_short_report_is_untouched_and_says_so(self) -> None:
        report, clipped = clip_report("short", 100)

        assert report == "short"
        assert clipped is False
