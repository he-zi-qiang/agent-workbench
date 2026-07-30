"""Adapt a Task research branch to the one policy-gated tool runtime."""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.adapters.tools.external_search import TOOL_NAME
from agent_workbench.application.task_research import EvidenceUnavailableError
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.identifiers import new_tool_call_id
from agent_workbench.domain.policies import ExecutionContext, PrincipalContext
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.ports.research import ExternalEvidenceSkipped
from agent_workbench.runtime.tool_gateway import PreparedCall, ToolGateway


@dataclass(frozen=True, slots=True)
class GatewayExternalEvidence:
    """Invoke ``external_search`` through schema, policy and audit gates."""

    gateway: ToolGateway

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
        if execution.task_id != task_id or execution.principal != principal:
            raise ValueError("external research execution context does not match Task")
        cancellation.raise_if_cancelled()
        call = ToolCall(
            tool_call_id=new_tool_call_id(),
            tool_name=TOOL_NAME,
            arguments={"query": query},
        )
        await self.gateway.propose(call, sink=sink)
        prepared = await self.gateway.prepare(call, context=execution, sink=sink)
        if isinstance(prepared, PreparedCall):
            prepared = await self.gateway.authorize(
                prepared, context=execution, sink=sink
            )
        if isinstance(prepared, PreparedCall):
            result = await self.gateway.invoke(
                prepared,
                context=execution,
                cancellation=cancellation,
                sink=sink,
            )
        else:
            result = prepared
        cancellation.raise_if_cancelled()
        if result.status != "ok" or result.artifact is None:
            if result.error is not None:
                if result.error.code == "approval_required":
                    return ExternalEvidenceSkipped(reason="approval_required")
                if result.error.code == "policy_denied":
                    return ExternalEvidenceSkipped(reason="policy_denied")
                if result.error.code == "provider_unavailable":
                    return ExternalEvidenceSkipped(reason="provider_unavailable")
            raise EvidenceUnavailableError(
                "external research tool did not return evidence"
            )
        return result.artifact


__all__ = ["GatewayExternalEvidence"]
