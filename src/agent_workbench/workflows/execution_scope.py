"""Which claim a graph invocation is running under.

A Task node needs to know the lease it is executing on behalf of, because that
is what every fenced write downstream is checked against. It cannot ask the
Registry: the Registry answers with whoever holds the Task *now*, and a Worker
whose lease lapsed mid-graph would read the epoch of the Worker that replaced
it and keep writing under it -- passing a fence whose entire purpose is to
refuse exactly that Worker.

So the claim travels from the one place that has it. The Worker obtains an
immutable :class:`ExecutionLease` when it claims the Task, enters it here for
the duration of one graph invocation, and every node that runs inside that
invocation reads back the same value. Nothing in between has to carry it: the
graph's state is checkpointed and a lease must never be, and the workflow
framework's node signature belongs to the framework.

The mechanism is a :class:`~contextvars.ContextVar`, which is what makes "for
the duration of one invocation" true rather than approximate. Its value is
per-execution, not per-process: a Worker that entered a lease and then awaited
a graph sees it in every coroutine that graph starts, and in nothing else. Two
Workers in one process do not collide, and a Worker that never entered a scope
does not accidentally inherit one from whatever ran before it.

The scope is an object rather than module-level functions so that the
dependency is stated where it is created -- a composition root wires the same
scope into the Worker and into the node invocation provider, and a reader can
see that the two are connected. An unentered scope answers ``None``, which the
provider treats as "this node has no authority to run", not as "use whatever
the Registry says".
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from agent_workbench.ports.task_registry import ExecutionLease


class TaskExecutionScope:
    """The lease one graph invocation is executing under, if any."""

    __slots__ = ("_current",)

    def __init__(self) -> None:
        self._current: ContextVar[ExecutionLease | None] = ContextVar(
            "task_execution_lease", default=None
        )

    @contextmanager
    def executing(self, lease: ExecutionLease) -> Generator[None]:
        """Run the block as the holder of ``lease``.

        Restored on the way out rather than cleared, so a nested or repeated
        entry cannot leave a later reader looking at an earlier claim -- a
        Worker that runs one Task, then another, must not have the first one's
        lease survive into the second.
        """

        token = self._current.set(lease)
        try:
            yield
        finally:
            self._current.reset(token)

    def current(self) -> ExecutionLease | None:
        """The lease this execution was entered with, or ``None``.

        ``None`` is an answer and not a default to fall back from: it means
        nothing claimed the Task on this path, and work that needs an
        authority must refuse rather than find one elsewhere.
        """

        return self._current.get()


__all__ = ["TaskExecutionScope"]
