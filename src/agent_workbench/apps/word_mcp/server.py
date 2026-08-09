"""Official MCP v2 server surface for path-free Word rendering."""

from __future__ import annotations

import base64
import hashlib
from typing import Any, Final

from mcp import types
from mcp.server import Server, ServerRequestContext
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_workbench.apps.word_mcp.contract import (
    RENDER_DOCUMENT_INPUT_SCHEMA,
    WordDocumentInputError,
    parse_document_request,
)
from agent_workbench.apps.word_mcp.renderer import (
    WORD_DOCUMENT_MEDIA_TYPE,
    render_document,
)

SERVER_NAME: Final[str] = "agent-workbench-word"
SERVER_VERSION: Final[str] = "1.0.0"
TOOL_NAME: Final[str] = "render_document"
MCP_PATH: Final[str] = "/mcp"
HEALTH_PATH: Final[str] = "/health"
MAX_MCP_REQUEST_BYTES: Final[int] = 262_144
TOOL_DESCRIPTION: Final[str] = (
    "Create a polished Word .docx artifact from bounded structured content. "
    "Call this tool only when the user explicitly asks for a Word or .docx file; "
    "do not call it for an ordinary text or Markdown report. The renderer is "
    "stateless, accepts no path or ownership fields, and returns one embedded "
    "Word document."
)

# Field names are the SDK's own (snake_case), not the wire's camelCase. The
# models set `populate_by_name`, so the camelCase aliases also construct these
# at runtime -- but only the declared names type-check, and a module that
# silently drifts to the wire spelling is one nothing verifies against the SDK.
_TOOL: Final[types.Tool] = types.Tool(
    name=TOOL_NAME,
    title="Render Word document",
    description=TOOL_DESCRIPTION,
    input_schema=RENDER_DOCUMENT_INPUT_SCHEMA,
    annotations=types.ToolAnnotations(
        title="Render Word document",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)

#: What the SDK's default lifespan yields. `Server[None]` does not type-check
#: against it, and this server has no lifespan state of its own to declare.
LifespanState = dict[str, Any]


def create_server() -> Server[LifespanState]:
    """Build an independent server suitable for HTTP or in-memory tests."""

    async def list_tools(
        context: ServerRequestContext[LifespanState],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        del context, params
        return types.ListToolsResult(tools=[_TOOL])

    async def call_tool(
        context: ServerRequestContext[LifespanState],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        del context
        if params.name != TOOL_NAME:
            return _error_result("unknown tool")
        try:
            request = parse_document_request(params.arguments or {})
            content = render_document(request)
        except WordDocumentInputError as error:
            return _error_result(f"invalid document request: {error}")
        except Exception:
            # Third-party exceptions may contain document text or host details.
            # Keep both out of protocol results and operator-visible Task events.
            return _error_result("document rendering failed")

        digest = hashlib.sha256(content).hexdigest()
        resource = types.BlobResourceContents(
            uri=f"urn:agent-workbench:word:{digest}",
            mime_type=WORD_DOCUMENT_MEDIA_TYPE,
            blob=base64.b64encode(content).decode("ascii"),
        )
        return types.CallToolResult(content=[types.EmbeddedResource(resource=resource)])

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        title="Agent Workbench Word renderer",
        description=(
            "A project-owned, stateless MCP server that renders bounded "
            "structured content into an embedded DOCX package."
        ),
        instructions=TOOL_DESCRIPTION,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def create_app(*, host: str = "127.0.0.1") -> Starlette:
    """Build the stateless Streamable HTTP app plus a liveness route."""

    async def health(request: Request) -> JSONResponse:
        del request
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVER_NAME,
                "transport": "streamable-http",
            }
        )

    return create_server().streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_MCP_REQUEST_BYTES,
        host=host,
        custom_starlette_routes=[Route(HEALTH_PATH, endpoint=health, methods=["GET"])],
    )


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=message)],
        is_error=True,
    )


__all__ = [
    "HEALTH_PATH",
    "MCP_PATH",
    "SERVER_NAME",
    "TOOL_DESCRIPTION",
    "TOOL_NAME",
    "create_app",
    "create_server",
]
