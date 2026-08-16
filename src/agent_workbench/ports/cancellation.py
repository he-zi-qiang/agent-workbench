"""Cooperative cancellation.

Cancellation is a fact before it is a signal. A task is cancelled because
PostgreSQL says so, or because a lease was lost; this token is only how that
fact reaches the code currently running. Nothing here decides to cancel.

The token is process-local by design. Cross-worker cancellation travels through
the task registry, and a worker that lost its lease is stopped by fencing, not
by a token it no longer trusts.

Two ways to observe it, and they answer different questions. ``cancelled`` and
``raise_if_cancelled`` are checkpoints: code that is *doing* something asks
between steps, which is why every existing consumer polls around its awaits.
``wait_cancelled`` is for code that is *waiting* on something else, where there
are no steps to ask between -- a poll placed after a wait that may never end is
a poll that never runs.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from agent_workbench.domain.errors import OperationCancelledError


@runtime_checkable
class CancellationToken(Protocol):
    """Read-only view of whether the current work should stop."""

    @property
    def cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None:
        """Raise ``OperationCancelledError`` if cancellation was requested."""
        ...

    async def wait_cancelled(self) -> None:
        """Return once cancellation has been requested, and not before.

        Returns immediately if it already has been. Otherwise this parks, and
        for a token that will never be cancelled it parks forever -- which is
        the honest answer to "tell me when", not a defect. Callers race it
        against the thing they actually want and must cancel the loser; a
        waiter left parked on an event nobody will set is a leak, one per race.

        It is a coroutine rather than an exposed ``asyncio.Event`` so that an
        implementation without one -- a token whose answer is fixed, a future
        adapter watching another process -- can still satisfy this.
        """
        ...


class NullCancellationToken:
    """A token that is never cancelled.

    Useful for synchronous entry points and tests. Production callers pass a
    real token so that a cancelled task stops at the next checkpoint.
    """

    __slots__ = ()

    @property
    def cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None

    async def wait_cancelled(self) -> None:
        """Never returns, because this token's answer can never change.

        A fresh event nobody holds, rather than a shared one: an
        ``asyncio.Event`` binds to the loop that first awaits it, and a
        module-level instance would bind to whichever loop happened to be
        first and then be useless -- or worse, subtly wrong -- to every other.
        Nothing is stored because nothing here can ever be set.
        """

        await asyncio.Event().wait()


class CancellationSource:
    """Owner side of a token: the only object allowed to request cancellation."""

    __slots__ = ("_cancelled", "_reason", "_requested")

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = "cancel_requested"
        # Created on first await, not here. A source is built by a route
        # handler or by composition, which need not be inside the loop that
        # will wait on it, and an Event bound to the wrong loop fails much
        # later and much less clearly.
        self._requested: asyncio.Event | None = None

    def cancel(self, reason: str = "cancel_requested") -> None:
        # Idempotent: the first reason wins, so a later generic cancel cannot
        # overwrite the specific one that actually stopped the work.
        if not self._cancelled:
            self._cancelled = True
            self._reason = reason
            if self._requested is not None:
                self._requested.set()

    async def wait_cancelled(self) -> None:
        # Checked before parking, and checked without touching the event: a
        # cancel that happened before anybody waited set no event, because
        # there was none to set.
        if self._cancelled:
            return
        if self._requested is None:
            self._requested = asyncio.Event()
        await self._requested.wait()

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise OperationCancelledError(self._reason)


__all__ = [
    "CancellationSource",
    "CancellationToken",
    "NullCancellationToken",
]
