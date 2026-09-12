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


# --- an unattended envelope (ADR-0116) ---------------------------------------
#
# The submitter answered the destructive gate in advance. The engine still
# reads the arguments, and the shapes in `domain/commands.py` are the calls that
# answer was never allowed to cover.

RUN_SPEC = ToolSpec(
    name="project_run",
    description="Run one shell command in the project directory.",
    input_schema={"type": "object"},
    concurrency="exclusive",
    risk="destructive",
    idempotency="safe",
    timeout_seconds=60,
    permission_scopes=("project:run",),
)


async def _run_handler(invocation: ToolInvocation) -> ToolResult:
    raise AssertionError("the policy engine never runs a handler")


def _unattended_context(*, unattended: bool) -> ExecutionContext:
    return ExecutionContext(
        principal=PrincipalContext(
            principal_id="user_1",
            tenant_id="tenant_a",
            scopes=("project:run",),
        ),
        envelope=AuthorizationEnvelope(
            allowed_tools=("project_run",),
            max_tool_risk="destructive",
            approval_required_risks=("destructive",),
            unattended=unattended,
        ),
        agent_run_id="run_1",
        policy_identity="policy-v1:0e67f8dd84919551",
    )


def _decide_run(command: str, *, unattended: bool) -> PolicyDecision:
    registry = StaticToolRegistry(
        [
            read_document_tool(CORPUS),
            ToolBinding(spec=RUN_SPEC, handler=_run_handler),
        ]
    )
    engine = EnvelopePolicyEngine(registry=registry)
    call = ToolCall(
        tool_call_id="toolu_1",
        tool_name="project_run",
        arguments={"command": command},
    )
    return asyncio.run(engine.decide(call, _unattended_context(unattended=unattended)))


def test_an_attended_envelope_holds_every_destructive_call() -> None:
    """The control: the field defaults off, and off is what every caller meant."""

    decision = _decide_run("pytest -q", unattended=False)

    assert decision.effect == "allow"
    assert decision.requires_approval is True
    assert decision.reason_code == "within_submitted_envelope"


def test_an_unattended_envelope_answers_a_routine_command_in_advance() -> None:
    decision = _decide_run("pytest -q", unattended=True)

    assert decision.effect == "allow"
    assert decision.requires_approval is False
    # Its own reason, so `PermissionResolved` tells a standing answer from a
    # call that never needed one.
    assert decision.reason_code == "unattended_turn"


def test_an_unattended_envelope_still_holds_a_shape_that_costs_the_work() -> None:
    decision = _decide_run("git reset --hard", unattended=True)

    assert decision.effect == "allow"
    assert decision.requires_approval is True
    assert decision.reason_code == "command_still_asks:discards uncommitted work"


def test_unattended_says_nothing_about_a_tool_below_destructive() -> None:
    """`external` and `write` are not this field's business (ADR-058, ADR-087)."""

    decision = _decide(
        _call("export_artifact"),
        ExecutionContext(
            principal=PrincipalContext(
                principal_id="user_1",
                tenant_id="tenant_a",
                scopes=("artifact:write",),
            ),
            envelope=AuthorizationEnvelope(
                allowed_tools=("export_artifact",),
                max_tool_risk="write",
                approval_required_risks=("write", "destructive"),
                unattended=True,
            ),
            agent_run_id="run_1",
            policy_identity="policy-v1:0e67f8dd84919551",
        ),
    )

    assert decision.requires_approval is True
    assert decision.reason_code == "within_submitted_envelope"
