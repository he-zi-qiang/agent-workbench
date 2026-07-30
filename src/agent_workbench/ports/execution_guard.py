"""A task-scoped, process-independent execution exclusivity boundary.

The Registry lease fences lifecycle updates.  An execution guard is the second
fact a Worker must hold while it performs non-transactional graph work: it
prevents two live processes from executing the same Task after a crash or
lease-reclaim race.  The contract deliberately contains no PostgreSQL type or
advisory-lock vocabulary, so a future backend keeps the same lost/release
semantics.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from agent_workbench.domain.identifiers import Identifier


class GuardUnavailableError(RuntimeError):
    """The task is already guarded, or the guard backend cannot grant it."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"execution guard is unavailable for task {task_id}")


@runtime_checkable
class ExecutionGuard(Protocol):
    """One acquired, task-pinned exclusive execution slot.

    ``lost`` is a signal rather than an exception because the monitor runs in
    its own coroutine.  The Worker can wait for it alongside graph execution
    and cancel work before any later fenced write is attempted.
    """

    task_id: Identifier
    worker_id: Identifier
    epoch: int
    backend_pid: int

    @property
    def lock_key(self) -> int:
        """The stable signed-int64 key held by this guard's backend session."""
        ...

    @property
    def lost(self) -> asyncio.Event: ...

    async def healthcheck(self) -> bool:
        """Confirm that the pinned backend session is still the same session."""
        ...

    async def release(self) -> None:
        """Release exclusivity and its pinned resource; idempotent on cleanup."""
        ...


@runtime_checkable
class GuardFactory(Protocol):
    """Acquire Guards without exposing an adapter's connection model."""

    async def acquire(
        self,
        *,
        task_id: Identifier,
        worker_id: Identifier,
        epoch: int,
    ) -> ExecutionGuard:
        """Return a held guard or raise :class:`GuardUnavailableError`."""
        ...


__all__ = [
    "ExecutionGuard",
    "GuardFactory",
    "GuardUnavailableError",
]
