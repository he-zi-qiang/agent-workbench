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


class SourcesUnreadableError(RuntimeError):
    """Search named pages, and not one of them could be read.

    Part of this port's contract rather than any one adapter's, because the
    distinction it draws is one every caller has to make and no caller can make
    for itself. An empty result means the query found nothing, and that is what
    a caller tells the model -- "say the search found nothing rather than
    answering from memory". When search actually returned pages and every fetch
    failed, that sentence blames the query for a fault in the network path, and
    nothing downstream can tell the two apart from an empty tuple.

    Measured on a developer machine behind a fake-IP proxy: every hostname
    resolved into 198.18.0.0/15, the address guard refused all nineteen results
    by design, and the answer read "搜索没有返回结果" -- which sent the reader
    looking for a better query for a problem that was in their DNS.

    ``reasons`` counts failures by short code rather than listing them by URL.
    The message reaches the model's context through a tool result, so it carries
    what a reader needs to act -- how many pages, and how they failed -- and not
    which address any single refusal was aimed at.
    """

    def __init__(
        self,
        named: int,
        reasons: dict[str, int] | None = None,
        *,
        hint: str | None = None,
    ) -> None:
        self.named = named
        self.reasons = dict(reasons or {})
        self.hint = hint
        breakdown = ", ".join(
            f"{code}={count}" for code, count in sorted(self.reasons.items())
        )
        detail = f" ({breakdown})" if breakdown else ""
        # The hint is the operator's half of the message. The counts above tell
        # a model what happened; a code that only ever appears when a machine is
        # configured a particular way should also tell the person reading the
        # log what to change -- which is the half that was missing when the
        # docstring above was written from a debugging session rather than from
        # the output.
        suffix = f" {hint}" if hint else ""
        super().__init__(
            f"search named {named} page(s) and none could be read{detail}.{suffix}"
        )


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
        """Return bounded public results or raise an adapter-specific error.

        Raises :class:`SourcesUnreadableError` when the search itself succeeded
        and named pages that could not then be read. An empty tuple is reserved
        for a search that genuinely matched nothing.
        """
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
    "SourcesUnreadableError",
]
