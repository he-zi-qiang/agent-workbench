"""The agent boundary.

Exactly one component owns a model-tool loop, and in this project it is the
custom runtime. A graph node calls this protocol; it does not run a second loop
of its own, and no third-party agent executor is registered behind it.

The v1 contract deliberately stops at the run level: a run either finishes,
fails or is cancelled. Resuming a partially executed tool loop across processes
would need run snapshots, message cursors and an orphan-tool protocol, so task
level recovery stays with the workflow checkpointer instead.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_workbench.domain.runs import AgentOutcome, AgentRunRequest
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink


@runtime_checkable
class AgentExecutor(Protocol):
    """Runs one agent to a terminal outcome."""

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        """Execute the run, emitting events as it goes.

        Implementations must return a terminal ``AgentOutcome`` rather than
        raising for expected failures: the caller is a graph node that has to
        record and route on the result either way.
        """
        ...


__all__ = ["AgentExecutor"]
