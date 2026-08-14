"""Which working set one run is operating on (ADR-028).

The tool registry is built once at process start and never changes, which is
what makes "which tools were available" answerable for an event stream that has
already been written. A workspace session is the opposite: it belongs to one
invocation, because the version it starts from is the one that invocation read
at its entry.

So the binding in the registry holds this scope rather than a session, and
whoever runs the agent enters a session around it. That is the same mechanism
:class:`TaskExecutionScope` uses for the lease, for the same reason -- a
``ContextVar`` makes "for the duration of one invocation" true rather than
approximate, so two concurrent runs in one process do not see each other's
workspace.

An unentered scope answers ``None``, and the tools treat that as a refusal
rather than as "make one". A workspace created on demand would be one nothing
committed and no checkpoint names, so everything written into it would be
silently lost at the end of the run.

**Why this lives in ``application`` rather than beside the graph.** It was in
``workflows`` while a Task was the only thing that had a working set, and three
adapters imported it from there -- an adapter reaching into the workflow layer,
which is backwards. What the scope actually describes is "one run's session",
and a run is not necessarily a graph node: a Code session has a working set and
no graph at all. Moving it here is what lets the tools keep one import while
the thing entering the scope changes.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from agent_workbench.application.workspace import WorkspaceSession


class WorkspaceScope:
    """The working-set session one node invocation is running with, if any."""

    __slots__ = ("_current",)

    def __init__(self) -> None:
        self._current: ContextVar[WorkspaceSession | None] = ContextVar(
            "workspace_session", default=None
        )

    @contextmanager
    def using(self, session: WorkspaceSession) -> Generator[None]:
        """Run the block against ``session``.

        Restored rather than cleared on the way out, so a node that runs after
        another in the same execution cannot inherit the earlier one's session
        and commit writes against a version it never read.
        """

        token = self._current.set(session)
        try:
            yield
        finally:
            self._current.reset(token)

    def current(self) -> WorkspaceSession | None:
        return self._current.get()


__all__ = ["WorkspaceScope"]
