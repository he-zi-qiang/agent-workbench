"""Framework-neutral, test-only fault-injection boundary.

Production code owns the *locations* of reliability windows but never chooses
whether to stop there.  A no-op implementation is the only production binding;
tests inject an allowlisted controller and coordinate through its barriers.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

FailpointName = Literal[
    "after_claim_commit_before_advisory_lock",
    "after_node_before_checkpoint",
    "inside_checkpoint_put",
    "after_graph_complete_before_registry_commit",
]


@runtime_checkable
class FaultInjector(Protocol):
    """Observe one named reliability window, optionally pausing or failing it."""

    async def hit(self, name: FailpointName) -> None: ...


__all__ = ["FailpointName", "FaultInjector"]
