"""Cooperative cancellation.

Cancellation is a fact before it is a signal. A task is cancelled because
PostgreSQL says so, or because a lease was lost; this token is only how that
fact reaches the code currently running. Nothing here decides to cancel.

The token is process-local by design. Cross-worker cancellation travels through
the task registry, and a worker that lost its lease is stopped by fencing, not
by a token it no longer trusts.
"""

from __future__ import annotations

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


class CancellationSource:
    """Owner side of a token: the only object allowed to request cancellation."""

    __slots__ = ("_cancelled", "_reason")

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = "cancel_requested"

    def cancel(self, reason: str = "cancel_requested") -> None:
        # Idempotent: the first reason wins, so a later generic cancel cannot
        # overwrite the specific one that actually stopped the work.
        if not self._cancelled:
            self._cancelled = True
            self._reason = reason

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
