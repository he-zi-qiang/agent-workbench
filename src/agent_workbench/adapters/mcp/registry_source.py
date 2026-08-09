"""Freeze one MCP directory into ordinary local ``ToolBinding`` objects."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from pydantic import TypeAdapter, ValidationError

from agent_workbench.adapters.mcp.client import MCPClientPort, RemoteToolDefinition
from agent_workbench.adapters.mcp.naming import SkipReason, tool_name_for
from agent_workbench.adapters.mcp.result_mapping import MCPToolHandler
from agent_workbench.adapters.mcp.schema_gate import admit
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import (
    ToolDescription,
    ToolName,
    ToolSpec,
)
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.tools import ToolBinding

MAX_DISCOVERY_PAGES = 100
MAX_DISCOVERED_TOOLS = 1_000

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_DESCRIPTION: TypeAdapter[str] = TypeAdapter(ToolDescription)
logger = logging.getLogger(__name__)


class MCPDiscoveryLimitError(RuntimeError):
    """A remote directory did not terminate inside the startup bound."""


async def discover_bindings(
    *,
    alias: str,
    allowed_remote_tools: tuple[str, ...],
    timeout_seconds: int,
    client: MCPClientPort,
    artifacts: ArtifactStore,
    artifact_threshold_bytes: int,
    max_result_bytes: int,
    max_artifact_bytes: int,
    server_lock: asyncio.Lock | None = None,
) -> tuple[ToolBinding, ...]:
    """Perform one bounded startup discovery and freeze the admitted bindings.

    "One discovery" includes every pagination request needed for that snapshot.
    A repeated cursor, excessive directory, protocol error or timeout rejects
    the server snapshot as a whole; individual bad names/schemas reject only
    those tools.
    """

    try:
        remote_tools = await _list_all_tools(client, timeout_seconds=timeout_seconds)
    except Exception as error:
        logger.warning(
            "mcp_discovery_failed",
            extra={
                "mcp_server_alias": alias,
                "mcp_error_type": type(error).__name__,
            },
        )
        return ()

    allowed = set(allowed_remote_tools)
    selected = [tool for tool in remote_tools if tool.name in allowed]
    by_remote = Counter(tool.name for tool in selected)
    duplicated_remote = {name for name, count in by_remote.items() if count > 1}

    candidates: list[tuple[ToolName, RemoteToolDefinition, JsonObject, str]] = []
    for tool in selected:
        if tool.name in duplicated_remote:
            _log_skip(alias, tool.name, "server listed the remote tool more than once")
            continue
        if tool.task_support == "required":
            _log_skip(alias, tool.name, "remote MCP Tasks support is required")
            continue
        local_name = tool_name_for(alias, tool.name)
        if isinstance(local_name, SkipReason):
            _log_skip(alias, tool.name, local_name.reason)
            continue
        try:
            schema = _JSON_OBJECT.validate_python(tool.input_schema, strict=True)
        except ValidationError:
            _log_skip(alias, tool.name, "input schema is not a JSON object")
            continue
        gated = admit(tool.name, schema)
        if isinstance(gated, SkipReason):
            _log_skip(alias, tool.name, gated.reason)
            continue
        try:
            description = _DESCRIPTION.validate_python(
                (tool.description or "").strip()
                or f"MCP tool {tool.name} from server {alias}."
            )
        except ValidationError:
            _log_skip(alias, tool.name, "description exceeds the local tool contract")
            continue
        candidates.append((local_name, tool, gated, description))

    local_counts = Counter(name for name, _, _, _ in candidates)
    collisions = {name for name, count in local_counts.items() if count > 1}
    lock = server_lock if server_lock is not None else asyncio.Lock()
    bindings: list[ToolBinding] = []
    for local_name, tool, schema, description in candidates:
        if local_name in collisions:
            _log_skip(alias, tool.name, f"normalized name collides at {local_name}")
            continue
        bindings.append(
            ToolBinding(
                spec=ToolSpec(
                    name=local_name,
                    description=description,
                    input_schema=schema,
                    concurrency="exclusive",
                    risk="external",
                    # Config only admits servers whose allowlisted tools are
                    # safe to invoke again when a whole graph node replays.
                    idempotency="safe",
                    timeout_seconds=timeout_seconds,
                    permission_scopes=(f"mcp:{alias}",),
                ),
                handler=MCPToolHandler(
                    client=client,
                    remote_name=tool.name,
                    artifacts=artifacts,
                    artifact_threshold_bytes=artifact_threshold_bytes,
                    max_result_bytes=max_result_bytes,
                    max_artifact_bytes=max_artifact_bytes,
                    server_lock=lock,
                ),
            )
        )

    for missing in sorted(allowed - set(by_remote)):
        _log_skip(
            alias,
            missing,
            "configured tool was absent from the startup directory",
        )
    return tuple(sorted(bindings, key=lambda binding: binding.spec.name))


async def _list_all_tools(
    client: MCPClientPort, *, timeout_seconds: int
) -> tuple[RemoteToolDefinition, ...]:
    tools: list[RemoteToolDefinition] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    async with asyncio.timeout(timeout_seconds):
        for _ in range(MAX_DISCOVERY_PAGES):
            page = await client.list_tools_page(cursor)
            tools.extend(page.tools)
            if len(tools) > MAX_DISCOVERED_TOOLS:
                raise MCPDiscoveryLimitError(
                    f"MCP directory exceeds {MAX_DISCOVERED_TOOLS} tools"
                )
            cursor = page.next_cursor
            if cursor is None:
                return tuple(tools)
            if cursor in seen_cursors:
                raise MCPDiscoveryLimitError("MCP directory repeated a cursor")
            seen_cursors.add(cursor)
    raise MCPDiscoveryLimitError(f"MCP directory exceeds {MAX_DISCOVERY_PAGES} pages")


def _log_skip(alias: str, remote_name: str, reason: str) -> None:
    logger.warning(
        "mcp_tool_skipped",
        extra={
            "mcp_server_alias": alias,
            "mcp_remote_tool": remote_name,
            "mcp_skip_reason": reason,
        },
    )


__all__ = [
    "MAX_DISCOVERED_TOOLS",
    "MAX_DISCOVERY_PAGES",
    "MCPDiscoveryLimitError",
    "discover_bindings",
]
