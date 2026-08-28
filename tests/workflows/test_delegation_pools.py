"""Why a delegating deployment builds two concurrency pools instead of one.

This is the mistake the assembly is shaped to avoid, and it is invisible in
review: the natural thing to write is one ``BoundedParallelExecutor`` and to
hand the same executor to the delegation tool, because that is what "run every
agent invocation through the bounded stack" sounds like.

It deadlocks. The semaphore is held for the whole invocation, so a parent
waiting inside a tool call is holding a slot until its child returns, and the
child is queued for a slot only the parent's return can free. Nothing errors:
the parent sits in ``executing_tools`` until the run's deadline, and the symptom
is a Task that is slow rather than a Task that is wrong.

The second test is the control, and it is the one worth keeping. It builds the
shared-pool version deliberately and asserts that it hangs -- so if somebody
later "simplifies" the composition back to one pool, the first test goes red and
this one explains what they did.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agent_workbench.adapters.delegation import EventDelegationChannel
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.tools.delegate import TOOL_NAME, DelegateTool
from agent_workbench.application.delegation import (
    DeferredExecutor,
    DelegationScope,
    DelegationScopingExecutor,
)
from agent_workbench.application.sub_agents import ANALYST
from agent_workbench.domain.agents import SubAgentCatalogue
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.runs import AgentOutcome, AgentRunRequest, RunBudget
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.cancellation import CancellationToken, NullCancellationToken
from agent_workbench.ports.event_log import EventScope, EventSink
from agent_workbench.ports.tools import ToolInvocation
from agent_workbench.workflows.task_handlers import BoundedParallelExecutor

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
STREAM = "stream_1"
CATALOGUE = SubAgentCatalogue((ANALYST,))

#: Long enough that a working stack finishes inside it many times over, short
#: enough that the deadlocking one does not make the suite wait.
PATIENCE_SECONDS = 2.0


class _RuntimeThatDelegatesOnce:
    """Stands in for the tool loop: the parent run makes one tool call.

    Not a real runtime, and it does not need to be. What is under test is the
    shape of the executor stack around the loop, and a scripted model would only
    add a way for this test to fail for an unrelated reason.
    """

    def __init__(self) -> None:
        self.tool: DelegateTool | None = None
        self.results: list[str] = []

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        if request.system_prompt == "parent":
            assert self.tool is not None
            result = await self.tool.handle(
                ToolInvocation(
                    call=ToolCall(
                        tool_call_id="call_1",
                        tool_name=TOOL_NAME,
                        arguments={"subagent_type": "analyst", "prompt": "think"},
                    ),
                    context=_execution(),
                    cancellation=cancellation,
                    timeout_seconds=60,
                )
            )
            self.results.append(result.status)
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text="done",
        )


def _execution() -> ExecutionContext:
    return ExecutionContext(
        principal=PrincipalContext(principal_id="user_1", tenant_id="tenant_a"),
        envelope=AuthorizationEnvelope(allowed_tools=(TOOL_NAME,)),
        agent_run_id="run_parent",
        policy_identity="rev_1:abcdef",
    )


def _parent_request() -> AgentRunRequest:
    from agent_workbench.domain.messages import user_message
    from agent_workbench.domain.runs import TraceContext

    return AgentRunRequest(
        trace=TraceContext(agent_run_id="run_parent"),
        run_kind="chat",
        stream_id=STREAM,
        principal=_execution().principal,
        envelope=_execution().envelope,
        budget=RunBudget(
            max_steps=4,
            max_tool_calls=2,
            deadline=NOW + timedelta(seconds=60),
        ),
        system_prompt="parent",
        messages=(user_message("go"),),
        tool_names=(TOOL_NAME,),
    )


def _assemble(
    *, share_one_pool: bool
) -> tuple[AgentExecutor, _RuntimeThatDelegatesOnce]:
    """Build both stacks the way composition does, or the way it must not.

    ``max_parallel=1`` on each pool. The real defaults are 3 and 2, which
    deadlock just as reliably once the graph's own fan-out has taken the other
    slots -- one is simply the smallest arrangement that shows it every time
    rather than under load.
    """

    events = InMemoryEventLog()
    scope = DelegationScope()
    runtime = _RuntimeThatDelegatesOnce()
    deferred = DeferredExecutor()

    def channel_for(request: AgentRunRequest) -> EventDelegationChannel:
        return EventDelegationChannel(
            log=events,
            parent_scope=EventScope(
                stream_id=request.stream_id, run_id=request.trace.agent_run_id
            ),
        )

    def scoped(inner: AgentExecutor) -> AgentExecutor:
        return DelegationScopingExecutor(
            inner,
            scope=scope,
            channel_for=channel_for,
            max_depth=1,
            max_children=4,
        )

    parent_pool = BoundedParallelExecutor(runtime, max_parallel=1)
    child_pool = (
        parent_pool
        if share_one_pool
        else BoundedParallelExecutor(runtime, max_parallel=1)
    )
    deferred.bind(scoped(child_pool))
    runtime.tool = DelegateTool(
        executor=deferred,
        catalogue=CATALOGUE,
        scope=scope,
        clock=lambda: NOW,
        agent_run_ids=lambda: "run_child",
    )
    return scoped(parent_pool), runtime


class TestTheChildDoesNotWaitForTheParentsSlot:
    def test_a_parent_waiting_on_a_child_does_not_hold_the_slot_it_needs(
        self,
    ) -> None:
        """Two pools: the delegation completes well inside the patience."""

        executor, runtime = _assemble(share_one_pool=False)

        async def scenario() -> None:
            outcome = await asyncio.wait_for(
                executor.run(
                    _parent_request(),
                    EventDelegationChannel(
                        log=InMemoryEventLog(),
                        parent_scope=EventScope(stream_id=STREAM, run_id="run_parent"),
                    ).sink_for_child("run_parent"),
                    NullCancellationToken(),
                ),
                timeout=PATIENCE_SECONDS,
            )
            assert outcome.status == "completed"
            assert runtime.results == ["ok"]

        asyncio.run(scenario())

    def test_one_shared_pool_is_the_deadlock_this_arrangement_avoids(self) -> None:
        """The control, and the reason the test above is not a tautology.

        Built deliberately wrong. If a later change collapses the composition
        back to a single pool, the test above goes red and this one names what
        happened -- which is worth more than either test alone, because the
        failure it describes has no error message of its own.
        """

        executor, _ = _assemble(share_one_pool=True)

        async def scenario() -> None:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    executor.run(
                        _parent_request(),
                        EventDelegationChannel(
                            log=InMemoryEventLog(),
                            parent_scope=EventScope(
                                stream_id=STREAM, run_id="run_parent"
                            ),
                        ).sink_for_child("run_parent"),
                        NullCancellationToken(),
                    ),
                    timeout=PATIENCE_SECONDS,
                )

        asyncio.run(scenario())
