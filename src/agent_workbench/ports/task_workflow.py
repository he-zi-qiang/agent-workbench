"""Framework-neutral boundary for a checkpointed Task workflow.

The workflow adapter owns graph compilation and checkpoints.  Callers own the
stable workflow identity: both ``thread_id`` and ``graph_version`` are explicit
on every operation, so an adapter cannot silently resume a checkpoint with a
different graph definition.

``resume`` intentionally accepts no initial ``TaskState``.  The state already
belongs to the checkpoint identified by ``thread_id``; accepting it again
would make it possible to append the original input twice after a crash.
Concrete LangGraph or LangChain state, config and command objects must stay
inside the adapter.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import StringConstraints, model_validator

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel
from agent_workbench.domain.tasks import TaskNodeId, TaskState

GraphVersion = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    ),
]
WorkflowDisposition = Literal["completed", "interrupted"]


class TaskWorkflowResult(DomainModel):
    """One bounded invocation's framework-independent result.

    This value is deliberately not a standalone persisted aggregate:
    ``TaskState`` is schema-versioned and the workflow adapter persists it in
    its checkpoint.  The wrapper reports only the invocation disposition and
    the stable identity needed by the Task service to reconcile that result.
    """

    thread_id: Identifier
    graph_version: GraphVersion
    disposition: WorkflowDisposition
    state: TaskState
    next_nodes: tuple[TaskNodeId, ...] = ()

    @model_validator(mode="after")
    def validate_disposition(self) -> TaskWorkflowResult:
        if len(set(self.next_nodes)) != len(self.next_nodes):
            raise ValueError("next_nodes must be unique")
        if self.disposition == "completed" and self.next_nodes:
            raise ValueError("a completed workflow cannot have next_nodes")
        if self.disposition == "interrupted" and not self.next_nodes:
            raise ValueError("an interrupted workflow must identify a next node")
        return self


class CheckpointPosition(DomainModel):
    """Where a thread's graph stopped, in terms no framework object appears in.

    ``graph_version`` is ``None`` for a checkpoint that never recorded which
    graph wrote it. That is not the same as a mismatch, and it is not more
    recoverable than one: an unlabelled position is one no process can claim to
    understand.

    ``pending_nodes`` is empty for a finished graph -- and also for a checkpoint
    whose graph this process cannot build, because pending work is LangGraph's
    own computation and asking for it requires compiling the graph that wrote
    it. That case is not ambiguous in practice: a position whose version cannot
    be built is parked before anything reads its pending nodes.
    """

    graph_version: GraphVersion | None = None
    pending_nodes: tuple[TaskNodeId, ...] = ()
    awaiting_approval_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_interrupt(self) -> CheckpointPosition:
        if self.awaiting_approval_id is not None and not self.pending_nodes:
            # A graph waiting for an approval has not finished. Allowing both
            # at once would make "finished" and "awaiting approval" depend on
            # which one the reader checks first.
            raise ValueError("a position awaiting an approval must have pending nodes")
        return self

    @property
    def finished(self) -> bool:
        return not self.pending_nodes


class WorkflowThreadAlreadyExistsError(RuntimeError):
    """Raised when first-run input is submitted to an existing thread."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"workflow thread already exists: {thread_id}")


class WorkflowThreadNotFoundError(RuntimeError):
    """Raised when no checkpoint exists for the requested thread."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"workflow thread not found: {thread_id}")


class WorkflowGraphVersionMismatchError(RuntimeError):
    """Raised instead of interpreting a checkpoint with another graph."""

    def __init__(
        self,
        *,
        thread_id: str,
        checkpoint_graph_version: str,
        requested_graph_version: str,
    ) -> None:
        self.thread_id = thread_id
        self.checkpoint_graph_version = checkpoint_graph_version
        self.requested_graph_version = requested_graph_version
        super().__init__(
            "workflow graph version mismatch for "
            f"{thread_id}: checkpoint={checkpoint_graph_version}, "
            f"requested={requested_graph_version}"
        )


@runtime_checkable
class TaskWorkflowPort(Protocol):
    """Run or resume one checkpointed Task graph."""

    async def run(
        self,
        state: TaskState,
        *,
        thread_id: Identifier,
        graph_version: GraphVersion,
    ) -> TaskWorkflowResult:
        """Start ``state`` once under a previously unused workflow thread.

        Reusing ``thread_id`` for another first run must raise
        ``WorkflowThreadAlreadyExistsError`` rather than merge or append input.
        """
        ...

    async def resume(
        self,
        *,
        thread_id: Identifier,
        graph_version: GraphVersion,
    ) -> TaskWorkflowResult:
        """Continue the existing checkpoint without resubmitting initial state.

        Missing checkpoints raise ``WorkflowThreadNotFoundError``.  A graph
        version different from the checkpoint's version raises
        ``WorkflowGraphVersionMismatchError`` and leaves the checkpoint
        untouched.
        """
        ...

    async def inspect(self, thread_id: Identifier) -> CheckpointPosition | None:
        """Report where ``thread_id`` stands, without running anything.

        ``None`` when the thread has no checkpoint. This is what lets a Worker
        decide between starting, resuming and parking *before* it commits to
        any of them, rather than by attempting one and reading the exception.
        """
        ...


__all__ = [
    "CheckpointPosition",
    "GraphVersion",
    "TaskWorkflowPort",
    "TaskWorkflowResult",
    "WorkflowDisposition",
    "WorkflowGraphVersionMismatchError",
    "WorkflowThreadAlreadyExistsError",
    "WorkflowThreadNotFoundError",
]
