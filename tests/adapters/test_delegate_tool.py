"""One delegation, end to end: the call in, the run, the report back.

The property the whole tier rests on is in the first section, and it is dull to
state and expensive to lose: **every path returns exactly one ToolResult**. A
delegated run can end four ways -- it answers, it hits a ceiling, it is
cancelled, it is asked for by a name nobody registered -- and the parent's model
is waiting on a ``tool_call_id`` in all four. The handler that raised on any of
them would hand the gateway an exception to normalize, which it does, into
``tool_failed`` carrying an exception type and no stop reason: the one fact
whoever is reading the transcript needs.

The second section is about the event stream, and it uses the real in-memory log
rather than a spy, because what is being asserted is a *shape* -- two run ids
inside one stream, the delegation announced by the parent and the run announced
by the child -- and a spy would only prove that some method was called.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from agent_workbench.adapters.delegation import EventDelegationChannel
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.tools.delegate import (
    TOOL_NAME,
    DelegateTool,
    spec_for,
)
from agent_workbench.application.delegation import DelegationContext, DelegationScope
from agent_workbench.application.sub_agents import ANALYST, RESEARCHER
from agent_workbench.domain.agents import SubAgentCatalogue
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.events import RunCompleted, RunStarted
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    RunBudget,
    TokenUsage,
)
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import CancellationToken, NullCancellationToken
from agent_workbench.ports.event_log import EventScope, EventSink
from agent_workbench.ports.tools import ToolInvocation

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
STREAM = "stream_1"
PARENT_RUN = "run_parent"
CHILD_RUN = "run_child"

CATALOGUE = SubAgentCatalogue((RESEARCHER, ANALYST))


@dataclass
class _ScriptedExecutor:
    """Answers with a prepared outcome, and records what it was asked to run.

    Deliberately not a runtime. What this file is about is the handler around
    the call, and a real loop here would make every assertion depend on a
    scripted model as well.
    """

    outcome: AgentOutcome
    #: Emitted on the child's sink before answering, so the assertions about
    #: which run id a child's events carry have something to look at.
    announce: bool = True
    seen: list[AgentRunRequest] = field(default_factory=list["AgentRunRequest"])

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        self.seen.append(request)
        if self.announce:
            await emit.emit(
                RunStarted(
                    run_kind=request.run_kind,
                    model_profile=request.model_profile,
                    tool_names=request.tool_names,
                    budget=request.budget,
                )
            )
            await emit.emit(
                RunCompleted(
                    stop_reason=self.outcome.stop_reason,
                    usage=self.outcome.usage,
                )
            )
        return self.outcome


def _completed(text: str = "the finding", tokens: int = 120) -> AgentOutcome:
    return AgentOutcome(
        agent_run_id=CHILD_RUN,
        status="completed",
        stop_reason="completed",
        output_text=text,
        usage=BudgetUsage(
            steps=3,
            tool_calls=2,
            tokens=TokenUsage(input_tokens=tokens, output_tokens=tokens // 2),
        ),
    )


def _stopped(stop_reason: str = "max_steps") -> AgentOutcome:
    return AgentOutcome(
        agent_run_id=CHILD_RUN,
        status="failed",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        output_text="I had got as far as the second search",
        error=ErrorInfo(code="budget_exceeded", message="the run stopped at a ceiling"),
    )


def _execution() -> ExecutionContext:
    return ExecutionContext(
        principal=PrincipalContext(principal_id="user_1", tenant_id="tenant_a"),
        envelope=AuthorizationEnvelope(
            allowed_tools=("knowledge_search", TOOL_NAME),
        ),
        agent_run_id=PARENT_RUN,
        policy_identity="rev_1:abcdef",
    )


def _invocation(
    arguments: dict[str, object],
    *,
    timeout_seconds: int = 600,
) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(
            tool_call_id="call_1",
            tool_name=TOOL_NAME,
            arguments=arguments,  # type: ignore[arg-type]
        ),
        context=_execution(),
        cancellation=NullCancellationToken(),
        timeout_seconds=timeout_seconds,
    )


def _delegate(
    executor: object,
    *,
    log: InMemoryEventLog | None = None,
    scope: DelegationScope | None = None,
    max_children: int = 4,
) -> tuple[DelegateTool, DelegationScope, DelegationContext, InMemoryEventLog]:
    events = log if log is not None else InMemoryEventLog()
    channel = EventDelegationChannel(
        log=events,
        parent_scope=EventScope(stream_id=STREAM, run_id=PARENT_RUN),
    )
    context = DelegationContext(
        stream_id=STREAM,
        run_kind="task",
        budget=RunBudget(max_steps=12, max_tool_calls=8),
        channel=channel,
        max_children=max_children,
    )
    delegation_scope = scope if scope is not None else DelegationScope()
    tool = DelegateTool(
        executor=executor,  # type: ignore[arg-type]
        catalogue=CATALOGUE,
        scope=delegation_scope,
        clock=lambda: NOW,
        agent_run_ids=lambda: CHILD_RUN,
    )
    return tool, delegation_scope, context, events


class TestEveryPathAnswersExactlyOnce:
    def test_a_completed_child_reports_what_it_wrote(self) -> None:
        tool, scope, context, _ = _delegate(_ScriptedExecutor(_completed()))

        async def scenario() -> None:
            with scope.using(context):
                result = await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "think"})
                )
            assert result.status == "ok"
            assert result.content == "the finding"
            assert result.tool_call_id == "call_1"

        asyncio.run(scenario())

    def test_a_child_stopped_at_a_ceiling_is_an_error_carrying_its_partial_work(
        self,
    ) -> None:
        """Two halves of one decision.

        The result is an *error*, because partial work must not read as a
        finished report -- the same rule ``AgentOutcome`` states for a run
        stopped by a budget. And the text the child did write is handed over
        anyway, because a parent that can see how far the child got writes a
        better second prompt than one told only that something failed.
        """

        tool, scope, context, _ = _delegate(_ScriptedExecutor(_stopped()))

        async def scenario() -> None:
            with scope.using(context):
                result = await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "think"})
                )
            assert result.status == "error"
            assert result.error is not None
            assert "max_steps" in result.error.message
            assert "second search" in result.content

        asyncio.run(scenario())

    def test_an_unknown_subagent_type_is_a_refusal_not_a_crash(self) -> None:
        """A model may propose any string. The proposal still has to become
        exactly one ToolResult, and the message names what does exist -- the
        next turn is the model's only chance to get it right."""

        tool, scope, context, _ = _delegate(_ScriptedExecutor(_completed()))

        async def scenario() -> None:
            with scope.using(context):
                result = await tool.handle(
                    _invocation({"subagent_type": "nobody", "prompt": "think"})
                )
            assert result.status == "error"
            assert result.error is not None
            assert result.error.code == "invalid_tool_input"
            assert "analyst" in result.error.message

        asyncio.run(scenario())

    def test_an_unentered_scope_refuses_rather_than_inventing_a_parent(self) -> None:
        tool, _, _, _ = _delegate(_ScriptedExecutor(_completed()))

        async def scenario() -> None:
            result = await tool.handle(
                _invocation({"subagent_type": "analyst", "prompt": "think"})
            )
            assert result.status == "error"
            assert result.error is not None
            assert result.error.code == "policy_denied"

        asyncio.run(scenario())

    def test_a_run_past_its_child_allowance_is_refused_before_a_child_starts(
        self,
    ) -> None:
        """Counted before the child is assembled. A ceiling consulted after the
        spend is not a ceiling, and here it would be a ceiling that costs a
        whole run to enforce."""

        executor = _ScriptedExecutor(_completed())
        tool, scope, context, _ = _delegate(executor, max_children=1)

        async def scenario() -> None:
            with scope.using(context):
                first = await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "one"})
                )
                second = await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "two"})
                )
            assert first.status == "ok"
            assert second.status == "error"
            assert second.error is not None
            assert second.error.code == "budget_exceeded"
            # The refusal did not cost a run.
            assert len(executor.seen) == 1

        asyncio.run(scenario())

    def test_an_empty_prompt_is_refused_before_a_run_is_started(self) -> None:
        """The child inherits no conversation, so an empty brief is a run that
        cannot do anything -- and one that would still be charged for."""

        executor = _ScriptedExecutor(_completed())
        tool, scope, context, _ = _delegate(executor)

        async def scenario() -> None:
            with scope.using(context):
                result = await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "   "})
                )
            assert result.status == "error"
            assert executor.seen == []

        asyncio.run(scenario())


class TestTheReportIsBoundedByTheToolNotByTheRuntime:
    def test_a_report_over_the_ceiling_is_cut_and_says_it_was_cut(self) -> None:
        """The parent's context check happens a turn late by construction, so
        the tool that knows how big its own answer is does the cutting."""

        big = "y" * (ANALYST.max_report_chars + 5_000)
        tool, scope, context, _ = _delegate(_ScriptedExecutor(_completed(text=big)))

        async def scenario() -> None:
            with scope.using(context):
                result = await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "think"})
                )
            assert len(result.content) < len(big)
            assert "truncated" in result.content

        asyncio.run(scenario())

    def test_the_cut_is_a_field_and_not_only_a_sentence_at_the_end(self) -> None:
        """The marker in the text is for the model; this is for everyone else.

        ``ToolCompleted.truncated`` has documented exactly this fact since the
        event was written, and nothing produced it -- so the console's copy of
        a clipped report was a half report with its one marker gone. The marker
        is at the *end* of 8000 characters and the event log keeps 4096, so it
        is the first thing ``bounded()`` drops. A field survives that.
        """

        big = "y" * (ANALYST.max_report_chars + 5_000)
        tool, scope, context, _ = _delegate(_ScriptedExecutor(_completed(text=big)))

        async def scenario() -> None:
            with scope.using(context):
                result = await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "think"})
                )
            assert result.truncated is True

        asyncio.run(scenario())

    def test_a_report_inside_the_ceiling_says_it_was_not_cut(self) -> None:
        """The other half, because a flag that is always true is not a flag."""

        tool, scope, context, _ = _delegate(_ScriptedExecutor(_completed()))

        async def scenario() -> None:
            with scope.using(context):
                result = await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "think"})
                )
            assert result.truncated is False
            assert "truncated" not in result.content

        asyncio.run(scenario())

    def test_a_failed_child_that_also_overran_says_its_partial_work_was_cut(
        self,
    ) -> None:
        """The path that used to discard the fact.

        This branch called ``clip_report(...)[0]`` and threw the second half
        away, so a child that both failed *and* overran handed its parent a
        report cut mid-sentence with nothing marking it -- while the success
        branch three lines below marked the identical cut. The argument for
        marking it is stronger here: passing partial work along is the whole
        reason this branch carries text at all.
        """

        big = "y" * (ANALYST.max_report_chars + 5_000)
        outcome = AgentOutcome(
            agent_run_id=CHILD_RUN,
            status="failed",
            stop_reason="max_steps",
            output_text=big,
            error=ErrorInfo(code="budget_exceeded", message="stopped at a ceiling"),
        )
        tool, scope, context, _ = _delegate(_ScriptedExecutor(outcome))

        async def scenario() -> None:
            with scope.using(context):
                result = await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "think"})
                )
            assert result.status == "error"
            assert result.truncated is True
            assert "truncated" in result.content

        asyncio.run(scenario())


class TestTheStreamShowsATreeAndNotAFlatRun:
    def test_the_parent_says_it_delegated_and_the_child_says_it_started(
        self,
    ) -> None:
        """One stream, two run ids, and each event on the run that owns it.

        ``AgentDelegated.child_agent_run_id`` is a run naming a *different*
        run, which only a parent can do -- so both delegation events land on the
        parent while ``RunStarted`` lands on the child. Fixing the emitter here
        is what stops the timeline's rendering order from depending on which
        coroutine got there first.
        """

        tool, scope, context, events = _delegate(_ScriptedExecutor(_completed()))

        async def scenario() -> None:
            with scope.using(context):
                await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "think"})
                )
            stored = await events.read(STREAM)
            by_type = [(held.event_type, held.run_id) for held in stored]

            assert by_type == [
                ("AgentDelegated", PARENT_RUN),
                ("RunStarted", CHILD_RUN),
                ("RunCompleted", CHILD_RUN),
                ("AgentCompleted", PARENT_RUN),
            ]
            # Everything is in one stream, which is what makes the child
            # visible to a subscriber that authorized the parent's.
            assert {held.stream_id for held in stored} == {STREAM}

        asyncio.run(scenario())

    def test_what_the_child_spent_is_recorded_against_the_run_that_sent_it(
        self,
    ) -> None:
        """The only place this number exists on the sending side: a ToolResult
        carries no usage, and the parent's own ledger counts a delegation as one
        tool call and nothing else."""

        tool, scope, context, events = _delegate(_ScriptedExecutor(_completed()))

        async def scenario() -> None:
            with scope.using(context):
                await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "think"})
                )
            stored = await events.read(STREAM)
            completed = [held for held in stored if held.event_type == "AgentCompleted"]
            assert len(completed) == 1
            payload = completed[0].payload
            assert payload.usage.tokens.input_tokens == 120  # type: ignore[union-attr]
            assert context.spent().tokens.input_tokens == 120

        asyncio.run(scenario())

    def test_a_child_that_never_answered_is_still_reported_as_finished(
        self,
    ) -> None:
        """A delegation that announced a child and never said what became of it
        leaves a reader unable to tell a crashed child from a running one --
        which is exactly what a crashed *parent* leaves behind, and the two must
        not look the same."""

        tool, scope, context, events = _delegate(_ScriptedExecutor(_stopped()))

        async def scenario() -> None:
            with scope.using(context):
                await tool.handle(
                    _invocation({"subagent_type": "analyst", "prompt": "think"})
                )
            stored = await events.read(STREAM)
            kinds = [held.event_type for held in stored]
            assert kinds.count("AgentDelegated") == 1
            assert kinds.count("AgentCompleted") == 1

        asyncio.run(scenario())


class TestTheChildRunsUnderTheParentsAuthority:
    def test_the_child_is_narrowed_to_the_intersection_of_two_ceilings(
        self,
    ) -> None:
        """``researcher`` names ``knowledge_search``; the parent holds it and
        ``delegate_agent``. The child gets one tool, and it is not the one that
        would let it delegate again."""

        executor = _ScriptedExecutor(_completed())
        tool, scope, context, _ = _delegate(executor)

        async def scenario() -> None:
            with scope.using(context):
                await tool.handle(
                    _invocation({"subagent_type": "researcher", "prompt": "find out"})
                )
            request = executor.seen[0]
            assert request.tool_names == ("knowledge_search",)
            assert TOOL_NAME not in request.tool_names
            assert request.envelope.max_tool_risk == "read"
            assert request.principal.tenant_id == "tenant_a"

        asyncio.run(scenario())

    def test_the_child_deadline_shrinks_with_the_call_it_is_made_inside(
        self,
    ) -> None:
        """Taken from the invocation, not from the spec: the gateway's ceiling
        for this call already accounts for what the parent had left."""

        executor = _ScriptedExecutor(_completed())
        tool, scope, context, _ = _delegate(executor)

        async def scenario() -> None:
            with scope.using(context):
                await tool.handle(
                    _invocation(
                        {"subagent_type": "analyst", "prompt": "think"},
                        timeout_seconds=45,
                    )
                )
            deadline = executor.seen[0].budget.deadline
            assert deadline is not None
            assert (deadline - NOW).total_seconds() == 45

        asyncio.run(scenario())


@dataclass
class _SlowExecutor:
    """A child that takes longer than the caller is willing to wait."""

    seconds: float = 5.0
    started: int = 0

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        self.started += 1
        await asyncio.sleep(self.seconds)
        raise AssertionError("this child was supposed to be cut off")


class TestAnAnnouncedChildAlwaysGetsAnEnding:
    def test_a_child_cut_off_by_the_tool_timeout_is_still_reported(self) -> None:
        """The failure this port's context manager exists to make unwritable.

        ``ToolExecutor`` runs every handler inside ``asyncio.timeout``. When it
        fires, ``CancelledError`` is raised at the handler's current await point
        -- the line awaiting the child -- so a handler written as
        ``delegated(...)`` then ``completed(...)`` never reaches the second
        call. The stream is then left with an ``AgentDelegated`` and nothing
        after it, which reads exactly like a child that is still running.

        The timeout here is applied the same way the real executor applies it,
        rather than by cancelling the task from outside, so what is under test
        is the path production actually takes.
        """

        tool, scope, context, events = _delegate(_SlowExecutor())

        async def scenario() -> None:
            with scope.using(context), pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await tool.handle(
                        _invocation({"subagent_type": "analyst", "prompt": "think"})
                    )
            # The terminal emit is shielded, so it survives the cancellation but
            # is not awaited by the coroutine that was cancelled. One turn of
            # the loop is what it needs.
            await asyncio.sleep(0.01)

            stored = await events.read(STREAM)
            kinds = [held.event_type for held in stored]
            assert kinds.count("AgentDelegated") == 1
            assert kinds.count("AgentCompleted") == 1
            completed = next(
                held for held in stored if held.event_type == "AgentCompleted"
            )
            assert completed.payload.status == "cancelled"  # type: ignore[union-attr]

        asyncio.run(scenario())

    def test_a_child_cut_off_gives_its_place_back(self) -> None:
        """A reservation left outstanding would shrink the allowance of a run
        that is still going, and shrink it by a child that is not running."""

        tool, scope, context, _ = _delegate(_SlowExecutor(), max_children=1)

        async def scenario() -> None:
            with scope.using(context):
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(0.05):
                        await tool.handle(
                            _invocation({"subagent_type": "analyst", "prompt": "think"})
                        )
                assert context.outstanding == 0
                assert context.may_delegate()

        asyncio.run(scenario())


@dataclass
class _SlowToFinishLog:
    """An event log whose terminal write takes long enough to be interrupted.

    The production shape of the case the shield exists for: a Worker being shut
    down while a delegation's ``AgentCompleted`` is mid-write to PostgreSQL.
    """

    inner: InMemoryEventLog
    seconds: float = 0.05

    async def append(
        self,
        scope: EventScope,
        payload: object,
        *,
        parent_event_id: str | None = None,
        event_key: str | None = None,
    ) -> object:
        if getattr(payload, "kind", "") == "AgentCompleted":
            await asyncio.sleep(self.seconds)
        return await self.inner.append(
            scope,
            payload,  # type: ignore[arg-type]
            parent_event_id=parent_event_id,
            event_key=event_key,
        )


class TestTheTerminalEventSurvivesAShutdown:
    def test_a_cancellation_arriving_mid_write_does_not_lose_the_ending(
        self,
    ) -> None:
        """What ``asyncio.shield`` buys, isolated from what ``ensure_future``
        buys.

        Detaching the emit into its own task is what makes it survive a
        cancellation that has *already* been raised. This is the other case:
        the cancellation arrives while the finally is suspended waiting for the
        write. Awaiting the task directly would cascade the cancellation into
        it; the shield is what stops that.
        """

        events = InMemoryEventLog()
        slow = _SlowToFinishLog(events)
        channel = EventDelegationChannel(
            log=slow,  # type: ignore[arg-type]
            parent_scope=EventScope(stream_id=STREAM, run_id=PARENT_RUN),
        )
        context = DelegationContext(
            stream_id=STREAM,
            run_kind="task",
            budget=RunBudget(max_steps=12, max_tool_calls=8),
            channel=channel,
        )
        scope = DelegationScope()
        tool = DelegateTool(
            executor=_ScriptedExecutor(_completed(), announce=False),  # type: ignore[arg-type]
            catalogue=CATALOGUE,
            scope=scope,
            clock=lambda: NOW,
            agent_run_ids=lambda: CHILD_RUN,
        )

        async def scenario() -> None:
            with scope.using(context):
                running = asyncio.ensure_future(
                    tool.handle(
                        _invocation({"subagent_type": "analyst", "prompt": "think"})
                    )
                )
                # Long enough to be inside the slow terminal write, short
                # enough to be well before it finishes.
                await asyncio.sleep(0.01)
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running

            # The shielded write is still going; give it the rest of its time.
            await asyncio.sleep(0.1)

            kinds = [held.event_type for held in await events.read(STREAM)]
            assert kinds == ["AgentDelegated", "AgentCompleted"]

        asyncio.run(scenario())


class TestSiblingDelegationsShareOneAllowance:
    def test_two_delegations_in_one_turn_cannot_both_take_the_last_place(
        self,
    ) -> None:
        """The batch this tool is deliberately shaped to allow is also the batch
        that breaks a naive count.

        ``delegate_agent`` is ``read`` and therefore ``parallel``, so the
        scheduler puts several calls in one group and the runtime starts them in
        a single ``gather``. Both would read ``len(spawned) == 0`` on the way
        in. Only the synchronous reservation separates them.
        """

        executor = _ScriptedExecutor(_completed())
        tool, scope, context, _ = _delegate(executor, max_children=1)

        async def scenario() -> None:
            with scope.using(context):
                results = await asyncio.gather(
                    tool.handle(
                        _invocation({"subagent_type": "analyst", "prompt": "one"})
                    ),
                    tool.handle(
                        _invocation({"subagent_type": "analyst", "prompt": "two"})
                    ),
                )
            statuses = sorted(result.status for result in results)
            assert statuses == ["error", "ok"]
            assert len(executor.seen) == 1, (
                "the refused sibling must not have started a run"
            )

        asyncio.run(scenario())


class TestTheSpecDescribesThisDeploymentAndNotAnother:
    def test_the_schema_enumerates_exactly_the_registered_sub_agents(self) -> None:
        """A free string would push the only possible check past a spent turn."""

        spec = spec_for(CATALOGUE)
        properties = spec.input_schema["properties"]
        assert isinstance(properties, dict)
        subagent_type = properties["subagent_type"]
        assert isinstance(subagent_type, dict)

        assert subagent_type["enum"] == ["researcher", "analyst"]

    def test_the_description_names_every_agent_the_model_may_choose(self) -> None:
        spec = spec_for(CATALOGUE)

        assert "researcher" in spec.description
        assert "analyst" in spec.description

    def test_the_description_forbids_writing_the_report_the_call_returns(
        self,
    ) -> None:
        """Measured 2026-08-28, and the reason this sentence exists.

        A `work` node asked to delegate three analyses opened with
        ``好的，我按 coordinator 的角色执行`` and wrote all three analyses out in
        prose -- headed ``# Analyst A 返回：`` -- before calling this tool for
        real. The work was done twice. Its own turns produced ~34,000 characters
        against the ~32,000 its children were bounded to return, and since
        ``max_total_tokens`` counts every turn's whole prompt, that passage cost
        its length once per remaining turn. The run died on ``token_budget``.

        Asserted on the description rather than on a model's behaviour because
        that is the half this repository controls: whether the instruction is
        present is a fact about the build, whether it is obeyed is not.
        """

        description = spec_for(CATALOGUE).description

        # The instruction, not merely the word "delegate".
        assert "do not describe the delegation" in description
        assert "never write the sub-agent's findings yourself" in description
        # And the reason, because a bare prohibition is the kind of line that
        # gets trimmed by whoever next shortens this text.
        assert "work done" in description and "twice" in description

    def test_delegation_is_declared_read_and_therefore_stays_parallel(self) -> None:
        """Not a stylistic claim. ``validate_risk_consistency`` forces every
        write tool to be exclusive, and an exclusive delegation tool could not
        fan out within a turn -- so a writing counterpart has to be a second
        tool rather than a flag on this one.
        """

        spec = spec_for(CATALOGUE)

        assert spec.risk == "read"
        assert spec.concurrency == "parallel"
        assert spec.idempotency == "safe"
