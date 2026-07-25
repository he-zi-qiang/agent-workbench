"""Deny-by-default policy over the submitted authorization envelope.

This is the interim engine. It answers the question the tool gateway must ask
on every call -- is this tool inside the envelope this work was submitted with,
and does the principal hold its scopes -- using only facts that already exist.

What it does not do yet is the full effective-authorization intersection:
envelope, the policy floor assembled from settings at process start, live ACL
state and the current tool registry, combined deny-overrides. That belongs with
the approval boundary and the side-effect ledger, because tightening only means
something once there is an irreversible write to stop.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.domain.policies import ExecutionContext, PolicyDecision
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.tools import ToolRegistry


@dataclass(frozen=True, slots=True)
class EnvelopePolicyEngine:
    """Allows a call only when the envelope and the principal both permit it."""

    registry: ToolRegistry

    async def decide(
        self,
        call: ToolCall,
        context: ExecutionContext,
    ) -> PolicyDecision:
        binding = self.registry.get(call.tool_name)
        if binding is None:
            return PolicyDecision.deny("unknown_tool")

        if not context.envelope.permits(binding.spec):
            return PolicyDecision.deny("outside_submitted_envelope")

        missing = set(binding.spec.permission_scopes) - set(context.principal.scopes)
        if missing:
            # Scopes are named in the decision only as a reason code: which
            # scope is missing is an operator detail, not model-facing content.
            return PolicyDecision.deny("missing_permission_scope")

        return PolicyDecision.allow(
            "within_submitted_envelope",
            requires_approval=context.envelope.requires_approval(binding.spec),
        )


__all__ = ["EnvelopePolicyEngine"]
