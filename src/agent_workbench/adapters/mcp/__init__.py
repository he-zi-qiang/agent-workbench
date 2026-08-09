"""MCP tools translated into the Workbench's local tool contract.

The package is deliberately an outer adapter.  Remote names, schemas and
content blocks are not allowed to leak into the framework-neutral runtime;
they are normalized here before a :class:`ToolBinding` can be assembled.
"""

from agent_workbench.adapters.mcp.naming import SkipReason, tool_name_for
from agent_workbench.adapters.mcp.registry_source import discover_bindings
from agent_workbench.adapters.mcp.schema_gate import admit

__all__ = ["SkipReason", "admit", "discover_bindings", "tool_name_for"]
