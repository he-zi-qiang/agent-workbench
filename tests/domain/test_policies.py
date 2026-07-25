"""Authorization envelope rules and the three legal policy answers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_workbench.domain.policies import (
    RISK_ORDER,
    AuthorizationEnvelope,
    PolicyDecision,
    PrincipalContext,
    risk_within,
)
from agent_workbench.domain.tools import ToolRisk, ToolSpec


def _spec(name: str = "read_document", risk: ToolRisk = "read") -> ToolSpec:
    if risk == "read":
        return ToolSpec(
            name=name,
            description="Read one document.",
            input_schema={"type": "object"},
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=30,
        )
    return ToolSpec(
        name=name,
        description="Cause an effect.",
        input_schema={"type": "object"},
        concurrency="exclusive",
        risk=risk,
        idempotency="keyed",
        timeout_seconds=30,
        permission_scopes=("artifact:write",),
    )


def test_an_empty_envelope_permits_nothing() -> None:
    """Defaults are deny-shaped, so a missing grant is never an accident."""

    assert AuthorizationEnvelope().permits(_spec()) is False


def test_denial_overrides_the_allowlist() -> None:
    envelope = AuthorizationEnvelope(
        allowed_tools=("read_document",),
        denied_tools=("read_document",),
    )

    assert envelope.permits(_spec()) is False


def test_the_risk_ceiling_applies_even_to_an_allowed_tool() -> None:
    """A tool whose risk was raised later stops being permitted by itself."""

    envelope = AuthorizationEnvelope(
        allowed_tools=("export_artifact",),
        max_tool_risk="read",
    )

    assert envelope.permits(_spec("export_artifact", "write")) is False

    widened = AuthorizationEnvelope(
        allowed_tools=("export_artifact",),
        max_tool_risk="write",
    )
    assert widened.permits(_spec("export_artifact", "write")) is True


def test_approval_is_required_by_risk_class() -> None:
    envelope = AuthorizationEnvelope(allowed_tools=("export_artifact",))

    assert envelope.requires_approval(_spec("export_artifact", "write")) is True
    assert envelope.requires_approval(_spec()) is False


def test_risk_order_runs_from_least_to_most_dangerous() -> None:
    assert RISK_ORDER == ("read", "write", "external", "destructive")
    assert risk_within("write", "destructive") is True
    assert risk_within("destructive", "write") is False


def test_envelope_collections_are_normalized() -> None:
    envelope = AuthorizationEnvelope(
        allowed_tools=("b_tool", "a_tool", "b_tool"),
        approval_required_risks=("destructive", "write", "write"),
    )

    assert envelope.allowed_tools == ("a_tool", "b_tool")
    assert envelope.approval_required_risks == ("write", "destructive")


def test_principal_scopes_are_normalized() -> None:
    principal = PrincipalContext(
        principal_id="user_1",
        tenant_id="tenant_a",
        scopes=("b:read", "a:read", "b:read"),
    )

    assert principal.scopes == ("a:read", "b:read")


def test_modified_input_belongs_to_exactly_one_effect() -> None:
    with pytest.raises(ValidationError, match="allow_with_modified_input"):
        PolicyDecision(
            effect="allow",
            reason_code="ok",
            modified_input={"clamped": True},
        )
    with pytest.raises(ValidationError, match="allow_with_modified_input"):
        PolicyDecision(effect="allow_with_modified_input", reason_code="clamped")


def test_a_denied_call_cannot_also_request_approval() -> None:
    with pytest.raises(ValidationError, match="cannot also request approval"):
        PolicyDecision(effect="deny", reason_code="blocked", requires_approval=True)


def test_decision_constructors_produce_the_three_legal_answers() -> None:
    allow = PolicyDecision.allow("within_envelope")
    deny = PolicyDecision.deny("outside_envelope")
    modified = PolicyDecision.allow_modified("clamped_top_k", {"top_k": 8})

    assert (allow.effect, allow.modified_input) == ("allow", None)
    assert (deny.effect, deny.requires_approval) == ("deny", False)
    assert modified.effect == "allow_with_modified_input"
    assert modified.modified_input == {"top_k": 8}
