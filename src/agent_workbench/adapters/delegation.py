"""The delegation channel, implemented over the event log.

Three decisions live here rather than in the handler that uses it.

**Both delegation events are emitted on the parent's scope.** The payload says
so itself: ``AgentDelegated.child_agent_run_id`` is a run naming a *different*
run, which only a parent can do. Emitting them from the child would mean the
child's sink had to be able to write under a run id that is not its own, and
then "which run does this event belong to" would stop being answerable from the
envelope.

**The child's sink differs from the parent's in exactly one field.**
``ScopedEventSink`` is frozen and holds one scope, so a child sink is the parent
scope with a new ``run_id`` -- not a new log, not a new stream, not a second
subscription. Everything that reads events reads them by stream, and the SSE
route authorizes a stream and nothing finer, so a child that wrote to its own
stream would be a child nothing in the product could show.

**The terminal event survives cancellation, and it takes two things to do it.**
A ``finally`` alone is not enough, and the two halves guard different moments.

``asyncio.ensure_future`` **detaches** the emit. When ``ToolExecutor``'s
``asyncio.timeout`` fires, the handler is already unwinding a
``CancelledError``, and the next bare ``await`` raises again before doing
anything -- so a ``finally`` that simply awaited would run and emit nothing. A
task already handed to the loop does not need this coroutine to survive; the
loop is still running, because the cancelled run still owes the model a
``ToolResult``.

``asyncio.shield`` guards the *other* moment: a cancellation that arrives while
this line is waiting for a slow write. Awaiting the task directly would cascade
that cancellation into it -- a Worker shutting down mid-write, which is exactly
when the record matters most. Each half is pinned by its own test, and each
fails alone.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.domain.events import AgentCompleted, AgentDelegated
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.ports.delegation import RecordOutcome
from agent_workbench.ports.event_log import EventLogPort, EventScope, EventSink

#: Strong references to in-flight terminal emits.
#:
#: A shielded task the awaiting coroutine never gets to collect is a task the
#: garbage collector may take before the loop runs it -- and the symptom of that
#: is the missing event this whole mechanism exists to guarantee. Discarded by
#: the done callback, so the set is empty whenever nothing is in flight.
_IN_FLIGHT: set[asyncio.Task[None]] = set()


def _unfinished(child_agent_run_id: str) -> AgentOutcome:
    """What a delegation reports when its body never recorded anything.

    Reached on cancellation and on an exception thrown past the child. Both are
    honest as ``cancelled``: the run was stopped from outside rather than
    stopping itself, which is exactly what ``AgentOutcome`` reserves that status
    for.
    """

    return AgentOutcome(
        agent_run_id=child_agent_run_id,
        status="cancelled",
        stop_reason="cancelled",
    )


@dataclass(frozen=True, slots=True)
class EventDelegationChannel:
    """Announces delegations on one run, and hands out its children's sinks."""

    log: EventLogPort
    #: The delegating run's own scope. Its ``run_id`` is what both delegation
    #: events land on; its ``stream_id`` is what every child inherits.
    parent_scope: EventScope

    def _parent_sink(self) -> EventSink:
        return ScopedEventSink(log=self.log, scope=self.parent_scope)

    async def _completed(self, child_agent_run_id: str, outcome: AgentOutcome) -> None:
        await self._parent_sink().emit(
            AgentCompleted(
                child_agent_run_id=child_agent_run_id,
                status=outcome.status,
                stop_reason=outcome.stop_reason,
                # The child's own account of what it spent. It is the only
                # place this number is recorded against the run that sent the
                # child, because a `ToolResult` carries no usage and the
                # parent's private ledger counts the delegation as one tool
                # call and nothing else.
                usage=outcome.usage,
            )
        )

    @asynccontextmanager
    async def delegating(
        self,
        *,
        child_agent_run_id: str,
        definition_name: str,
    ) -> AsyncGenerator[RecordOutcome]:
        await self._parent_sink().emit(
            AgentDelegated(
                child_agent_run_id=child_agent_run_id,
                profile_name=definition_name,
                # The node the *parent* is running on, when it is running on
                # one. A delegated run sits on no node of its own, and putting
                # the parent's here is what lets a Task timeline group the
                # delegation with the step that made it rather than orphaning
                # it beside the graph.
                graph_node_id=self.parent_scope.graph_node_id,
            )
        )
        recorded: list[AgentOutcome] = []
        try:
            yield recorded.append
        finally:
            outcome = recorded[0] if recorded else _unfinished(child_agent_run_id)
            emitting = asyncio.ensure_future(
                self._completed(child_agent_run_id, outcome)
            )
            _IN_FLIGHT.add(emitting)
            emitting.add_done_callback(_IN_FLIGHT.discard)
            # Not suppressed. A `CancelledError` raised here is a real one --
            # either the body was already unwinding it, or it arrived while this
            # line waited -- and swallowing it would let a cancelled handler
            # return a `ToolResult` as though nothing had happened. The shield
            # is what makes propagating it safe: `emitting` is detached, so it
            # finishes whether or not this coroutine survives to see it.
            await asyncio.shield(emitting)

    def sink_for_child(self, child_agent_run_id: str) -> EventSink:
        # Written out field by field rather than copied with an update, so that
        # a field added to `EventScope` later has to be considered here instead
        # of being carried down by default. The one that changes is `run_id`;
        # the rest travelling unchanged is the whole point.
        return ScopedEventSink(
            log=self.log,
            scope=EventScope(
                stream_id=self.parent_scope.stream_id,
                run_id=child_agent_run_id,
                task_id=self.parent_scope.task_id,
                graph_node_id=self.parent_scope.graph_node_id,
            ),
        )


__all__ = ["EventDelegationChannel"]
