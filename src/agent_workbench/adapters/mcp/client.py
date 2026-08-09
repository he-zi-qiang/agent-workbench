"""The official MCP SDK behind a small project-owned client contract.

No SDK model crosses this module.  Discovery and result mapping consume the
frozen dataclasses below, which keeps ``mcp`` and ``mcp_types`` at the outer
adapter boundary and makes malformed remote payloads independently testable.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from mcp import Client, types

from agent_workbench.domain.schema import JsonObject


@dataclass(frozen=True, slots=True)
class RemoteToolDefinition:
    name: str
    description: str | None
    input_schema: object
    task_support: Literal["forbidden", "optional", "required"] | None = None


@dataclass(frozen=True, slots=True)
class RemoteToolPage:
    tools: tuple[RemoteToolDefinition, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteTextBlock:
    text: str
    media_type: str | None = None
    embedded_resource: bool = False


@dataclass(frozen=True, slots=True)
class RemoteBinaryBlock:
    data: bytes
    media_type: str | None
    kind: str


@dataclass(frozen=True, slots=True)
class RemoteResourceLink:
    name: str
    uri: str
    media_type: str | None = None


RemoteContentBlock = RemoteTextBlock | RemoteBinaryBlock | RemoteResourceLink


@dataclass(frozen=True, slots=True)
class RemoteCallResult:
    content: tuple[RemoteContentBlock, ...]
    structured_content: object | None = None
    is_error: bool = False


@runtime_checkable
class MCPClientPort(Protocol):
    """The two MCP operations this adapter needs after connection."""

    async def list_tools_page(self, cursor: str | None) -> RemoteToolPage: ...

    async def call_tool(self, name: str, arguments: JsonObject) -> RemoteCallResult: ...


@dataclass(slots=True)
class _SDKMCPClient:
    client: Client

    async def list_tools_page(self, cursor: str | None) -> RemoteToolPage:
        page = await self.client.list_tools(cursor=cursor)
        return RemoteToolPage(
            tools=tuple(_remote_tool_definition(tool) for tool in page.tools),
            next_cursor=page.next_cursor,
        )

    async def call_tool(self, name: str, arguments: JsonObject) -> RemoteCallResult:
        result = await self.client.call_tool(name, arguments)
        return RemoteCallResult(
            content=tuple(_content_block(block) for block in result.content),
            structured_content=result.structured_content,
            is_error=result.is_error,
        )


def _remote_tool_definition(tool: types.Tool) -> RemoteToolDefinition:
    """Project one SDK tool, including legacy task-support metadata."""

    return RemoteToolDefinition(
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema,
        task_support=(
            tool.execution.task_support if tool.execution is not None else None
        ),
    )


def _content_block(block: types.ContentBlock) -> RemoteContentBlock:
    if isinstance(block, types.TextContent):
        return RemoteTextBlock(text=block.text)
    if isinstance(block, types.ImageContent):
        return RemoteBinaryBlock(
            data=base64.b64decode(block.data, validate=True),
            media_type=block.mime_type,
            kind="image",
        )
    if isinstance(block, types.AudioContent):
        return RemoteBinaryBlock(
            data=base64.b64decode(block.data, validate=True),
            media_type=block.mime_type,
            kind="audio",
        )
    if isinstance(block, types.ResourceLink):
        return RemoteResourceLink(
            name=block.name,
            uri=str(block.uri),
            media_type=block.mime_type,
        )
    resource = block.resource
    if isinstance(resource, types.TextResourceContents):
        return RemoteTextBlock(
            text=resource.text,
            media_type=resource.mime_type,
            embedded_resource=True,
        )
    return RemoteBinaryBlock(
        data=base64.b64decode(resource.blob, validate=True),
        media_type=resource.mime_type,
        kind="resource",
    )


@asynccontextmanager
async def connect_mcp_client(
    endpoint: str, *, timeout_seconds: int
) -> AsyncGenerator[MCPClientPort]:
    """Open one process-owned SDK client with caching disabled.

    The official v2 Client negotiates the 2026 protocol and falls back to older
    revisions.  A URL selects Streamable HTTP; stdio is intentionally absent
    from this adapter's configuration contract.
    """

    async with Client(
        endpoint,
        read_timeout_seconds=float(timeout_seconds),
        cache=None,
    ) as client:
        yield _SDKMCPClient(client)


__all__ = [
    "MCPClientPort",
    "RemoteBinaryBlock",
    "RemoteCallResult",
    "RemoteContentBlock",
    "RemoteResourceLink",
    "RemoteTextBlock",
    "RemoteToolDefinition",
    "RemoteToolPage",
    "connect_mcp_client",
]
