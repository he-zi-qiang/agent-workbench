"""Which project directory one run is operating on (ADR-073).

The same shape as :class:`WorkspaceScope`, and deliberately not a
generalisation of it. The two hold different things -- a working set versus a
directory -- and a run holds exactly one of them (ADR-073 §2), so a single
scope parameterised over both would make "which one is this" a question with a
runtime answer instead of a structural one.

The mechanism is the one ``workspace_scope`` documents: the tool registry is
built once at process start and never changes, which is what keeps "what tools
were available" answerable for an event stream already written; the *store* a
run works against belongs to one turn. A ``ContextVar`` makes "for the duration
of this turn" true rather than approximate, so two concurrent coding sessions in
one process cannot see each other's directory.

An unentered scope answers ``None`` and the tools treat that as a refusal, not
as "open one". Opening a store on demand would mean picking a root here, and the
only root this module could pick is one nobody registered.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from agent_workbench.ports.project_files import ProjectFileStore


class ProjectFileScope:
    """The project directory one turn is running against, if any."""

    __slots__ = ("_current",)

    def __init__(self) -> None:
        self._current: ContextVar[ProjectFileStore | None] = ContextVar(
            "project_file_store", default=None
        )

    @contextmanager
    def using(self, store: ProjectFileStore) -> Generator[None]:
        """Run the block against ``store``.

        Restored rather than cleared on the way out, for the reason
        ``WorkspaceScope.using`` gives: a turn that runs after another in the
        same execution must not inherit the earlier one's store. Here that
        would be worse than inheriting a stale version -- it would be writing
        one project's files into another project's directory, with every path
        check passing, because the checks are relative to whichever root was
        left behind.
        """

        token = self._current.set(store)
        try:
            yield
        finally:
            self._current.reset(token)

    def current(self) -> ProjectFileStore | None:
        return self._current.get()


__all__ = ["ProjectFileScope"]
