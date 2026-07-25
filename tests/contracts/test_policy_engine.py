"""Contract for the policy engine: deny by default, and why each denial fires."""

from __future__ import annotations

import asyncio

from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import (
    StaticToolRegistry,
    read_document_tool,
    text_statistics_tool,
)
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PolicyDecision,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

CORPUS = {"doc_1": "text"}

EXPORT_SPEC = ToolSpec(
    name="export_artifact",
    description="Write the approved report to the artifact store.",
    input_schema={"type": "object"},
    concurrency="exclusive",
    risk="write",
    idempotency="keyed",
    timeout_seconds=60,
    permission_scopes=("artifact:write",),
)


async def _export_handler(invocation: ToolInvocation) -> ToolResult:
    raise AssertionError("a denied call must never reach its handler")


def _registry() -> StaticToolRegistry:
    return StaticToolRegistry(
        [
            read_document_tool(CORPUS),
            text_statistics_tool(),
            ToolBinding(spec=EXPORT_SPEC, handler=_export_handler),
        ]
    )


def _context(
    *,
    allowed: tuple[str, ...] = ("read_document",),
    denied: tuple[str, ...] = (),
    max_risk: str = "read",
    scopes: tuple[str, ...] = (),
) -> ExecutionContext:
    return ExecutionContext(
        principal=PrincipalContext(
            principal_id="user_1",
            tenant_id="tenant_a",
            scopes=scopes,
        ),
        envelope=AuthorizationEnvelope(
            allowed_tools=allowed,
            denied_tools=denied,
            max_tool_risk=max_risk,  # pyright: ignore[reportArgumentType]
        ),
        agent_run_id="run_1",
        policy_identity="policy-v1:0e67f8dd84919551",
    )


def _decide(call: ToolCall, context: ExecutionContext) -> PolicyDecision:
    engine = EnvelopePolicyEngine(registry=_registry())
    return asyncio.run(engine.decide(call, context))


def _call(tool_name: str) -> ToolCall:
    return ToolCall(tool_call_id="toolu_1", tool_name=tool_name)


def test_an_allowed_read_tool_is_permitted() -> None:
    decision = _decide(_call("read_document"), _context())

    assert decision.effect == "allow"
    assert decision.requires_approval is False


def test_an_unknown_tool_is_denied_before_anything_else() -> None:
    decision = _decide(_call("definitely_not_registered"), _context())

    assert decision.effect == "deny"
    assert decision.reason_code == "unknown_tool"


def test_a_tool_outside_the_envelope_is_denied() -> None:
    decision = _decide(_call("text_statistics"), _context())

    assert decision.effect == "deny"
    assert decision.reason_code == "outside_submitted_envelope"


def test_denial_wins_over_the_allowlist() -> None:
    decision = _decide(
        _call("read_document"),
        _context(allowed=("read_document",), denied=("read_document",)),
    )

    assert decision.effect == "deny"


def test_the_risk_ceiling_blocks_an_allowed_write_tool() -> None:
    decision = _decide(
        _call("export_artifact"),
        _context(allowed=("export_artifact",), scopes=("artifact:write",)),
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "outside_submitted_envelope"


def test_a_missing_permission_scope_is_denied() -> None:
    decision = _decide(
        _call("export_artifact"),
        _context(allowed=("export_artifact",), max_risk="write"),
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "missing_permission_scope"


def test_a_write_tool_within_the_envelope_still_requires_approval() -> None:
    decision = _decide(
        _call("export_artifact"),
        _context(
            allowed=("export_artifact",),
            max_risk="write",
            scopes=("artifact:write",),
        ),
    )

    assert decision.effect == "allow"
    assert decision.requires_approval is True


def test_a_denial_never_carries_rewritten_arguments() -> None:
    """Only an explicit allow_with_modified_input may change the input."""

    decision = _decide(_call("text_statistics"), _context())

    assert decision.modified_input is None
