"""Identity, authorization envelope and tool-call decisions.

Authorization has three independent sources: the envelope captured when a task
was submitted, the policy floor assembled from immutable settings at process
start, and live ACL and tool-registry state. The effective permission is their
deny-overrides intersection, which is why this module models an envelope as
data with a predicate rather than as a single mutable "current policy" object.

Merging several envelopes into one effective envelope is deliberately absent:
that algebra belongs to the policy engine work package, together with the
approval and ledger boundaries it protects.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, field_validator, model_validator

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import JsonObject, ShortText, VersionedModel
from agent_workbench.domain.tools import (
    PermissionScope,
    ToolName,
    ToolRisk,
    ToolSpec,
)

PolicyEffect = Literal["allow", "deny", "allow_with_modified_input"]

# Ordered from least to most dangerous; a ceiling admits everything below it.
RISK_ORDER: Final[tuple[ToolRisk, ...]] = (
    "read",
    "write",
    "external",
    "destructive",
)


def risk_within(risk: ToolRisk, ceiling: ToolRisk) -> bool:
    """Return whether ``risk`` stays at or below ``ceiling``."""

    return RISK_ORDER.index(risk) <= RISK_ORDER.index(ceiling)


class PrincipalContext(VersionedModel):
    """Authenticated caller, resolved by the interface layer.

    A request body never names its own owner: identity arrives from the
    interface layer's authentication result, and every repository query carries
    the tenant explicitly.
    """

    principal_id: Identifier
    tenant_id: Identifier
    scopes: tuple[PermissionScope, ...] = ()

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))


class AuthorizationEnvelope(VersionedModel):
    """The permission ceiling captured when work was submitted.

    Defaults are deny-shaped: an empty allowlist permits nothing and the risk
    ceiling starts at read-only. Widening the current policy later must never
    widen an old task, so this envelope is stored with the task and re-applied
    on every resume.
    """

    allowed_tools: tuple[ToolName, ...] = ()
    denied_tools: tuple[ToolName, ...] = ()
    max_tool_risk: ToolRisk = "read"
    approval_required_risks: tuple[ToolRisk, ...] = (
        "write",
        "external",
        "destructive",
    )

    @field_validator("allowed_tools", "denied_tools")
    @classmethod
    def normalize_tool_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator("approval_required_risks")
    @classmethod
    def normalize_risks(cls, value: tuple[ToolRisk, ...]) -> tuple[ToolRisk, ...]:
        return tuple(sorted(set(value), key=RISK_ORDER.index))

    def permits(self, spec: ToolSpec) -> bool:
        """Return whether this envelope alone admits the tool.

        Denial always wins over the allowlist, and the risk ceiling applies
        even to an explicitly allowed tool: a tool whose risk was raised after
        submission stops being permitted without any envelope rewrite.
        """

        if spec.name in self.denied_tools:
            return False
        if spec.name not in self.allowed_tools:
            return False
        return risk_within(spec.risk, self.max_tool_risk)

    def requires_approval(self, spec: ToolSpec) -> bool:
        return spec.risk in self.approval_required_risks


class ExecutionContext(VersionedModel):
    """Everything a policy decision may depend on, made explicit.

    ``policy_identity`` pairs the operator-set policy revision with the
    fingerprint derived from the rules themselves, so a hand-edited rule set
    that kept its label is still detected. It is recorded with every decision
    and every side-effect ledger entry.
    """

    principal: PrincipalContext
    envelope: AuthorizationEnvelope
    agent_run_id: Identifier
    policy_identity: ShortText
    task_id: Identifier | None = None
    workflow_thread_id: Identifier | None = None
    graph_node_id: Identifier | None = None
    lease_epoch: int | None = Field(default=None, ge=0)


class PolicyDecision(VersionedModel):
    """The only three answers the policy engine may give."""

    effect: PolicyEffect
    reason_code: ShortText
    modified_input: JsonObject | None = None
    requires_approval: bool = False

    @model_validator(mode="after")
    def validate_effect(self) -> PolicyDecision:
        modified = self.modified_input is not None
        if modified is not (self.effect == "allow_with_modified_input"):
            raise ValueError(
                "modified_input is present exactly when the effect is "
                "allow_with_modified_input"
            )
        if self.effect == "deny" and self.requires_approval:
            raise ValueError("a denied call cannot also request approval")
        return self

    @classmethod
    def allow(
        cls,
        reason_code: str,
        *,
        requires_approval: bool = False,
    ) -> PolicyDecision:
        return cls(
            effect="allow",
            reason_code=reason_code,
            requires_approval=requires_approval,
        )

    @classmethod
    def deny(cls, reason_code: str) -> PolicyDecision:
        return cls(effect="deny", reason_code=reason_code)

    @classmethod
    def allow_modified(
        cls,
        reason_code: str,
        modified_input: JsonObject,
        *,
        requires_approval: bool = False,
    ) -> PolicyDecision:
        """Allow the call with rewritten arguments.

        The caller must re-run schema validation and policy evaluation against
        the rewritten arguments: an edit that skipped either check would be a
        way to smuggle input past both.
        """

        return cls(
            effect="allow_with_modified_input",
            reason_code=reason_code,
            modified_input=modified_input,
            requires_approval=requires_approval,
        )


__all__ = [
    "RISK_ORDER",
    "AuthorizationEnvelope",
    "ExecutionContext",
    "PolicyDecision",
    "PolicyEffect",
    "PrincipalContext",
    "risk_within",
]
