"""Official MCP v2 server surface for the two read-only web tools."""

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

from agent_workbench.apps.web_mcp.contract import (
    DOWNLOAD_DOCUMENT_INPUT_SCHEMA,
    FETCH_PAGE_INPUT_SCHEMA,
    WebRequestInputError,
    parse_download_request,
    parse_fetch_page_request,
)
from agent_workbench.apps.web_mcp.fetcher import WebFetcher, WebFetchError

SERVER_NAME: Final[str] = "agent-workbench-web"
SERVER_VERSION: Final[str] = "1.0.0"
FETCH_PAGE_TOOL: Final[str] = "fetch_page"
DOWNLOAD_DOCUMENT_TOOL: Final[str] = "download_document"
MCP_PATH: Final[str] = "/mcp"
HEALTH_PATH: Final[str] = "/health"
MAX_MCP_REQUEST_BYTES: Final[int] = 262_144

FETCH_PAGE_DESCRIPTION: Final[str] = (
    "Read one web page and return its readable text. Use this when you know "
    "which page you want, rather than searching for one. The text is extracted "
    "and the markup is discarded, so this is not a way to inspect HTML. Pages "
    "that build themselves with JavaScript return little or nothing, and pages "
    "that are not text -- a PDF or a spreadsheet -- are refused here and "
    "belong to download_document."
)

DOWNLOAD_DOCUMENT_DESCRIPTION: Final[str] = (
    "Download one file by URL and return its bytes. Use this for anything that "
    "is not a readable web page: a PDF, a spreadsheet, an image, an archive. "
    "The file is stored as an artifact; its text is not extracted here."
)

_ANNOTATIONS: Final[types.ToolAnnotations] = types.ToolAnnotations(
    # Both are GETs. ADR-027's whole line is that reading the outside world
    # changes nothing out there, which is what makes a graph-node replay able
    # to make the same request again without a second effect.
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    # The one hint that is honestly true here: what a URL answers is not this
    # process's to predict, and it may differ between two identical requests.
    open_world_hint=True,
)

_TOOLS: Final[tuple[types.Tool, ...]] = (
    types.Tool(
        name=FETCH_PAGE_TOOL,
        title="Read a web page",
        description=FETCH_PAGE_DESCRIPTION,
        input_schema=FETCH_PAGE_INPUT_SCHEMA,
        annotations=_ANNOTATIONS,
    ),
    types.Tool(
        name=DOWNLOAD_DOCUMENT_TOOL,
        title="Download a document",
        description=DOWNLOAD_DOCUMENT_DESCRIPTION,
        input_schema=DOWNLOAD_DOCUMENT_INPUT_SCHEMA,
        annotations=_ANNOTATIONS,
    ),
)

#: What the SDK's default lifespan yields. `Server[None]` does not type-check
#: against it, and this server has no lifespan state of its own to declare.
LifespanState = dict[str, Any]


def create_server(fetcher: WebFetcher) -> Server[LifespanState]:
    """Build an independent server suitable for HTTP or in-memory tests."""

    async def list_tools(
        context: ServerRequestContext[LifespanState],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        del context, params
        return types.ListToolsResult(tools=list(_TOOLS))

    async def call_tool(
        context: ServerRequestContext[LifespanState],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        del context
        arguments = params.arguments or {}
        try:
            if params.name == FETCH_PAGE_TOOL:
                text = await fetcher.fetch_page(parse_fetch_page_request(arguments))
                return types.CallToolResult(content=[types.TextContent(text=text)])
            if params.name == DOWNLOAD_DOCUMENT_TOOL:
                document = await fetcher.download_document(
                    parse_download_request(arguments)
                )
                digest = hashlib.sha256(document.content).hexdigest()
                return types.CallToolResult(
                    content=[
                        types.EmbeddedResource(
                            resource=types.BlobResourceContents(
                                uri=f"urn:agent-workbench:web:{digest}",
                                mime_type=document.media_type,
                                blob=base64.b64encode(document.content).decode("ascii"),
                            )
                        )
                    ]
                )
        except WebRequestInputError as error:
            return _error_result(f"invalid request: {error}")
        except WebFetchError as error:
            return _error_result(f"{error.code}: {error}")
        except Exception:
            # Third-party exceptions may carry proxy hostnames, local paths or
            # the response body. Keep all three out of protocol results and
            # operator-visible Task events.
            return _error_result("the request could not be completed")
        return _error_result("unknown tool")

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        title="Agent Workbench web reader",
        description=(
            "A project-owned, stateless MCP server that reads web pages and "
            "downloads files. It never writes to the sites it reads."
        ),
        instructions=FETCH_PAGE_DESCRIPTION,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def create_app(*, host: str = "127.0.0.1", fetcher: WebFetcher) -> Starlette:
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

    return create_server(fetcher).streamable_http_app(
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
    "DOWNLOAD_DOCUMENT_TOOL",
    "FETCH_PAGE_TOOL",
    "HEALTH_PATH",
    "MCP_PATH",
    "SERVER_NAME",
    "create_app",
    "create_server",
]
