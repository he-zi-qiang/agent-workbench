"""Provider-neutral external search boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.evidence import ExternalSearchHit
from agent_workbench.domain.policies import ExecutionContext, PrincipalContext
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink

ExternalEvidenceSkipReason = Literal[
    "approval_required",
    "policy_denied",
    "provider_unavailable",
]


@dataclass(frozen=True, slots=True)
class ExternalEvidenceSkipped:
    """A policy or absent optional provider deliberately produced no evidence."""

    reason: ExternalEvidenceSkipReason


@runtime_checkable
class ExternalSearchPort(Protocol):
    """Search public sources without receiving a model-selected identity."""

    async def search(
        self,
        *,
        query: str,
        limit: int,
        cancellation: CancellationToken,
    ) -> tuple[ExternalSearchHit, ...]:
        """Return bounded public results or raise an adapter-specific error."""
        ...


@runtime_checkable
class ExternalEvidenceToolPort(Protocol):
    """Obtain external evidence through the runtime's policy-gated tool path."""

    async def gather(
        self,
        *,
        query: str,
        task_id: str,
        principal: PrincipalContext,
        execution: ExecutionContext,
        sink: EventSink,
        cancellation: CancellationToken,
    ) -> ArtifactRef | ExternalEvidenceSkipped:
        """Return evidence, or a deliberate no-evidence result after auditing."""
        ...


__all__ = [
    "ExternalEvidenceSkipReason",
    "ExternalEvidenceSkipped",
    "ExternalEvidenceToolPort",
    "ExternalSearchPort",
]
