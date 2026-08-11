"""The boundary the Task's one write node reaches its effect through.

A port rather than a direct call, for the same reason external search is one:
the graph node must not be able to reach a handler except through the runtime's
tool path, and a port is what makes that structural instead of remembered. The
adapter that implements it drives the gateway; nothing in ``workflows`` knows
that, and nothing there can bypass it.

The return value is an artifact id, not a reference. A resume that recovers an
export performed by an earlier attempt has the ledger's record of what was made
and not the object's size or digest, so an id is the most this boundary can
promise on both paths -- and promising a full reference would mean re-reading
the store just to make the two shapes agree.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink


@runtime_checkable
class ReportExportPort(Protocol):
    """Perform this Task's export, or return the one already performed."""

    async def export(
        self,
        *,
        draft_ref: Identifier,
        #: The approval this export rests on, or ``None`` on a deployment that
        #: does not gate exports (`workflow.export_requires_approval`).
        #:
        #: Optional rather than required because it is *provenance*, not
        #: authority: nothing checks it, and its one use is a line in the
        #: report saying who approved this. An ungated export has no such line
        #: to write truthfully, and inventing an identifier to satisfy a
        #: signature would put a fabricated approval into a delivered document.
        approval_id: Identifier | None,
        execution: ExecutionContext,
        sink: EventSink,
        cancellation: CancellationToken,
    ) -> Identifier:
        """Return the exported report's artifact id.

        Raises rather than returning a sentinel when nothing was exported: an
        approved Task that cannot export has failed, and a caller that had to
        remember to check a sentinel is a caller that will eventually settle
        such a Task as succeeded.
        """
        ...


__all__ = ["ReportExportPort"]
