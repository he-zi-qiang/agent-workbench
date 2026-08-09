"""MCP schemas are filtered without weakening the native tool path."""

from __future__ import annotations

import pytest

from agent_workbench.adapters.mcp.naming import SkipReason
from agent_workbench.adapters.mcp.schema_gate import admit
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.tools.registry import StaticToolRegistry
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolResult, ToolSpec
from agent_workbench.ports.tools import ToolBinding, ToolInvocation
from agent_workbench.runtime.schema_validation import UnsupportedToolSchema
from agent_workbench.runtime.tool_gateway import ToolGateway

GOOD: JsonObject = {
    "type": "object",
    "properties": {"query": {"type": "string", "minLength": 1}},
    "required": ["query"],
    "additionalProperties": False,
}
UNSUPPORTED: JsonObject = {
    "type": "object",
    "properties": {"query": {"oneOf": [{"type": "string"}]}},
}


def test_a_batch_skips_only_the_schema_the_gateway_cannot_enforce() -> None:
    admitted = [
        result
        for name, schema in (("good", GOOD), ("unsupported", UNSUPPORTED))
        if not isinstance((result := admit(name, schema)), SkipReason)
    ]

    assert admitted == [GOOD]
    refusal = admit("unsupported", UNSUPPORTED)
    assert isinstance(refusal, SkipReason)
    assert "oneOf" in refusal.reason


def test_only_an_object_shaped_input_schema_is_admitted() -> None:
    refusal = admit("scalar", {"type": "string"})

    assert isinstance(refusal, SkipReason)
    assert admit("object", GOOD) == GOOD


def test_the_native_path_still_fails_closed_on_an_unsupported_schema() -> None:
    async def handler(invocation: ToolInvocation) -> ToolResult:
        return ToolResult.succeeded(invocation.call)

    binding = ToolBinding(
        spec=ToolSpec(
            name="native_bad_schema",
            description="A deliberately invalid native schema.",
            input_schema=UNSUPPORTED,
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=1,
        ),
        handler=handler,
    )
    registry = StaticToolRegistry((binding,))

    with pytest.raises(UnsupportedToolSchema, match="oneOf"):
        ToolGateway(registry=registry, policy=EnvelopePolicyEngine(registry=registry))
