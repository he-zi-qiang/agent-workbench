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

from agent_workbench.domain.commands import arguments_still_ask
from agent_workbench.domain.policies import ExecutionContext, PolicyDecision
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.tools import ToolRegistry

#: The reason codes an unattended envelope can produce (ADR-0116), spelled
#: once so the audit trail and the tests agree on them. `unattended_turn` is
#: the ordinary answer: the call would have stopped, and the submitter's
#: standing answer stood in for the click. `command_still_asks` is the
#: exception, and it names the reason the shape list gave.
UNATTENDED_REASON = "unattended_turn"
STILL_ASKS_REASON_PREFIX = "command_still_asks:"


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

        requires_approval = context.envelope.requires_approval(binding.spec)
        if requires_approval and context.envelope.answered_in_advance(binding.spec):
            # The one place a decision reads the arguments (ADR-0116). The
            # envelope says the submitter answered yes in advance; the shape
            # list says which commands that answer was never allowed to
            # cover. A match keeps the call on the ordinary path -- held for
            # a person, with the reason on the record -- and no match is the
            # standing answer applied, recorded as its own reason so a
            # reader of `PermissionResolved` can tell it from a click.
            reason = arguments_still_ask(call.arguments)
            if reason is None:
                return PolicyDecision.allow(UNATTENDED_REASON, requires_approval=False)
            return PolicyDecision.allow(
                f"{STILL_ASKS_REASON_PREFIX}{reason}", requires_approval=True
            )

        return PolicyDecision.allow(
            "within_submitted_envelope",
            requires_approval=requires_approval,
        )


__all__ = ["STILL_ASKS_REASON_PREFIX", "UNATTENDED_REASON", "EnvelopePolicyEngine"]
