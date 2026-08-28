"""The delegation boundary: one scoped verb and a sink the caller may not address.

A tool handler that starts another run needs three things from the event layer,
and the obvious way to give it them is to hand it the parent's ``EventSink``.
That is refused here for the reason ``ports/tools.py`` already refuses it for
progress reporting (ADR-068): a sink is the *whole* event vocabulary, and a
handler holding one can emit ``AnswerCommitted`` on the run that invoked it.

So the handler gets a channel instead: one scoped verb, already bound to the run
doing the delegating, plus a factory for the child's own sink -- which the
handler cannot aim anywhere except at that child, because it does not choose the
scope, it only names the child.

**Why the verb is a context manager rather than a pair of calls.** The first
shape of this port had ``delegated()`` and ``completed()``. It is the natural
shape and it is wrong, in a way that only shows up on the path nobody exercises
by hand. ``ToolExecutor`` runs every handler inside ``asyncio.timeout``; when
that fires, ``CancelledError`` is raised *at the handler's current await point*,
which is the line awaiting the child. A handler written as two calls never
reaches the second one, and the stream is left holding an ``AgentDelegated``
with nothing after it -- which is precisely the state this port exists to
prevent, because a reader cannot tell it apart from a child that is still going.

Making the terminal event the job of ``__aexit__`` moves that from something a
handler must remember to something it cannot avoid. It is the same reasoning
``domain/agents.py`` uses for the depth ceiling: a counter is a thing to trust, an
absent tool is a thing to read.

Which side emits is fixed by the payload's own field name.
``AgentDelegated.child_agent_run_id`` is a parent pointing at a child, so both
delegation events land on the **parent's** run; the child's ``RunStarted`` and
``RunCompleted`` land on the child's. A reader replaying one stream therefore
sees the delegation announced before the run it announces, and never has to know
which coroutine got there first.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable

from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.ports.event_log import EventSink

#: What the body of a delegation is handed: somewhere to put the child's
#: outcome. Calling it is what turns the terminal event from "cancelled" into
#: what actually happened, so a body that returns without calling it has said
#: the child did not finish -- which, on every path where that can happen, is
#: true.
RecordOutcome = Callable[[AgentOutcome], None]


@runtime_checkable
class DelegationChannel(Protocol):
    """How a run says it started another one, and where that one writes."""

    def delegating(
        self,
        *,
        child_agent_run_id: str,
        definition_name: str,
    ) -> AbstractAsyncContextManager[RecordOutcome]:
        """Announce a child on entry and account for it on exit, always.

        Entering emits ``AgentDelegated`` on the parent's run. Leaving emits
        ``AgentCompleted`` -- with the outcome the body recorded, or a cancelled
        one if the body did not get that far.

        Leaving on the cancellation path is the whole point, and it is not free:
        an implementation has to shield the emit, because an ``await`` inside a
        ``finally`` that is already unwinding a ``CancelledError`` raises again
        before doing anything.
        """
        ...

    def sink_for_child(self, child_agent_run_id: str) -> EventSink:
        """The sink the child run emits through: same stream, its own run id.

        Same stream because everything that reads events reads them by stream:
        the SSE route authorizes one, the timeline pages through one, the
        frontend subscribes to one. A child on its own stream would need a
        second authorization path before it could be seen at all.

        Its own run id because that is the only thing separating the two runs'
        ``RunStarted`` events once they are in the same stream.
        """
        ...


__all__ = ["DelegationChannel", "RecordOutcome"]
