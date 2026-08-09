"""The strict local schema gate applied to untrusted MCP tool schemas."""

from __future__ import annotations

from agent_workbench.adapters.mcp.naming import SkipReason
from agent_workbench.domain.schema import JsonObject
from agent_workbench.runtime.schema_validation import (
    UnsupportedToolSchema,
    assert_schema_supported,
)


def admit(remote_name: str, schema: JsonObject) -> JsonObject | SkipReason:
    """Admit exactly the schema subset the common ToolGateway can enforce.

    Native schemas still fail Worker assembly in ``ToolGateway``.  This adapter
    catches ``UnsupportedToolSchema`` only before a third-party MCP tool reaches
    the registry, so one incompatible remote declaration cannot take down the
    native tool set.
    """

    if schema.get("type") != "object":
        return SkipReason(
            remote_name=remote_name,
            reason="input schema must declare an object at its top level",
        )
    try:
        assert_schema_supported(schema, origin=f"MCP tool {remote_name}")
    except UnsupportedToolSchema as error:
        return SkipReason(remote_name=remote_name, reason=str(error))
    return schema


__all__ = ["admit"]
