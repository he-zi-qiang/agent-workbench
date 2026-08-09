"""Contract tests for the thin official-SDK boundary."""

from __future__ import annotations

import asyncio
import base64
import socket

import pytest
import uvicorn
from mcp import Client, types
from mcp.server import Server, ServerRequestContext

from agent_workbench.adapters.mcp import client as client_module
from agent_workbench.adapters.mcp.client import (
    RemoteBinaryBlock,
    RemoteResourceLink,
    RemoteTextBlock,
    connect_mcp_client,
)


class _ReadyUvicornServer(uvicorn.Server):
    """Expose ASGI startup completion without timing sleeps."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        self.ready.set()


def test_the_official_v2_in_memory_transport_lists_pages_and_calls_a_tool() -> None:
    list_cursors: list[str | None] = []
    calls: list[tuple[str, object]] = []

    async def list_tools(
        context: ServerRequestContext[None],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        del context
        cursor = params.cursor if params is not None else None
        list_cursors.append(cursor)
        if cursor is None:
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="echo",
                        description="Echo one value.",
                        inputSchema={
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                        },
                    )
                ],
                nextCursor="page-2",
            )
        assert cursor == "page-2"
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="render",
                    description=None,
                    inputSchema={"type": "object"},
                    execution=types.ToolExecution(taskSupport="required"),
                )
            ]
        )

    async def call_tool(
        context: ServerRequestContext[None],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        del context
        calls.append((params.name, params.arguments))
        return types.CallToolResult(
            content=[
                types.TextContent(text="done"),
                types.ImageContent(
                    data=base64.b64encode(b"image-bytes").decode("ascii"),
                    mimeType="image/png",
                ),
                types.AudioContent(
                    data=base64.b64encode(b"audio-bytes").decode("ascii"),
                    mimeType="audio/wav",
                ),
                types.ResourceLink(
                    name="manual",
                    uri="https://example.test/manual",
                    mimeType="text/html",
                ),
                types.EmbeddedResource(
                    resource=types.TextResourceContents(
                        uri="file:///notes.txt",
                        mimeType="text/plain",
                        text="embedded notes",
                    )
                ),
                types.EmbeddedResource(
                    resource=types.BlobResourceContents(
                        uri="file:///report.bin",
                        mimeType="application/octet-stream",
                        blob=base64.b64encode(b"blob-bytes").decode("ascii"),
                    )
                ),
            ],
            structuredContent={"ok": True},
        )

    async def scenario() -> None:
        server = Server(
            "adapter-contract",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        async with Client(
            server,
            cache=None,
            raise_exceptions=True,
        ) as sdk_client:
            # The SDK object is deliberately wrapped immediately.  All tests
            # below this boundary observe only project-owned frozen values.
            client = client_module._SDKMCPClient(sdk_client)
            first = await client.list_tools_page(None)
            second = await client.list_tools_page(first.next_cursor)
            result = await client.call_tool("echo", {"value": "hello"})

        assert [tool.name for tool in first.tools] == ["echo"]
        assert first.tools[0].input_schema["type"] == "object"
        assert first.next_cursor == "page-2"
        assert [tool.name for tool in second.tools] == ["render"]
        # The negotiated 2026-07-28 wire no longer carries the legacy
        # Tool.execution field, even if the server-side compatibility model
        # contains it.
        assert second.tools[0].task_support is None
        assert second.next_cursor is None
        assert result.structured_content == {"ok": True}
        assert result.is_error is False
        assert result.content == (
            RemoteTextBlock(text="done"),
            RemoteBinaryBlock(
                data=b"image-bytes", media_type="image/png", kind="image"
            ),
            RemoteBinaryBlock(
                data=b"audio-bytes", media_type="audio/wav", kind="audio"
            ),
            RemoteResourceLink(
                name="manual",
                uri="https://example.test/manual",
                media_type="text/html",
            ),
            RemoteTextBlock(
                text="embedded notes",
                media_type="text/plain",
                embedded_resource=True,
            ),
            RemoteBinaryBlock(
                data=b"blob-bytes",
                media_type="application/octet-stream",
                kind="resource",
            ),
        )

    asyncio.run(scenario())

    assert list_cursors == [None, "page-2"]
    assert calls == [("echo", {"value": "hello"})]


def test_legacy_task_support_metadata_is_projected_when_the_sdk_exposes_it() -> None:
    async def list_tools(
        context: ServerRequestContext[None],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        del context, params
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="long_job",
                    inputSchema={"type": "object"},
                    execution=types.ToolExecution(taskSupport="required"),
                )
            ]
        )

    async def scenario() -> str | None:
        server = Server("legacy-task-support", on_list_tools=list_tools)
        # Force the SDK's <=2025 initialize/list path. The current modern
        # server/discover wire intentionally removes Tool.execution, so only a
        # real legacy negotiation proves fallback metadata survives the wire.
        async with Client(
            server,
            mode="legacy",
            cache=None,
            raise_exceptions=True,
        ) as sdk_client:
            client = client_module._SDKMCPClient(sdk_client)
            page = await client.list_tools_page(None)
            return page.tools[0].task_support

    assert asyncio.run(scenario()) == "required"


def test_invalid_base64_is_rejected_at_the_sdk_boundary() -> None:
    async def list_tools(
        context: ServerRequestContext[None],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        del context, params
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="bad_image",
                    inputSchema={"type": "object"},
                )
            ]
        )

    async def call_tool(
        context: ServerRequestContext[None],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        del context, params
        return types.CallToolResult(
            content=[types.ImageContent(data="not base64!", mimeType="image/png")]
        )

    async def scenario() -> None:
        server = Server(
            "invalid-content",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        async with Client(server, cache=None, raise_exceptions=True) as sdk_client:
            client = client_module._SDKMCPClient(sdk_client)
            with pytest.raises(ValueError):
                await client.call_tool("bad_image", {})

    asyncio.run(scenario())


def test_production_url_uses_streamable_http_over_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # httpx2 honors the host OS proxy configuration.  Make the test's loopback
    # intent explicit so a developer's desktop proxy cannot intercept it.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def list_tools(
        context: ServerRequestContext[None],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        del context, params
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="ping",
                    description="Return pong.",
                    inputSchema={"type": "object"},
                )
            ]
        )

    async def call_tool(
        context: ServerRequestContext[None],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        del context
        assert params.name == "ping"
        return types.CallToolResult(content=[types.TextContent(text="pong")])

    async def scenario() -> None:
        mcp_server = Server(
            "loopback-contract",
            on_list_tools=list_tools,
            on_call_tool=call_tool,
        )
        app = mcp_server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
        )
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = _ReadyUvicornServer(
            uvicorn.Config(
                app,
                log_level="error",
                access_log=False,
                lifespan="on",
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(server.ready.wait(), timeout=5)
            async with connect_mcp_client(
                f"http://127.0.0.1:{port}/mcp",
                timeout_seconds=5,
            ) as client:
                page = await client.list_tools_page(None)
                result = await client.call_tool("ping", {})
            assert [tool.name for tool in page.tools] == ["ping"]
            assert result.content == (RemoteTextBlock(text="pong"),)
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=5)
            listener.close()

    asyncio.run(scenario())
