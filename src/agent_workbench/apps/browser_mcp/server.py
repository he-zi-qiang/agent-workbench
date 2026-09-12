"""MCP surface for the guarded browser (ADR-0113 §3.4).

The six tools, and one extra HTTP route that is not a tool: `/frame` hands the
console the latest screencast frame. It is deliberately outside the MCP surface
because it serves a *person* watching, not a model acting -- the same split
ADR-095 made for the computer-use page.
"""

from __future__ import annotations

from typing import Any, Final

from mcp import types
from mcp.server import Server, ServerRequestContext
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_workbench.apps.browser_mcp.contract import (
    DIAGNOSTICS_INPUT_SCHEMA,
    EVAL_INPUT_SCHEMA,
    INTERACT_INPUT_SCHEMA,
    OPEN_INPUT_SCHEMA,
    SCREENSHOT_INPUT_SCHEMA,
    SNAPSHOT_INPUT_SCHEMA,
    BrowserInputError,
    parse_eval,
    parse_interact,
    parse_open,
)
from agent_workbench.apps.browser_mcp.proxy import DecisionSource
from agent_workbench.apps.browser_mcp.session import (
    BrowserSession,
    describe,
    render_logs,
)

SERVER_NAME: Final[str] = "agent-workbench-browser"
SERVER_VERSION: Final[str] = "1.0.0"
MCP_PATH: Final[str] = "/mcp"
HEALTH_PATH: Final[str] = "/health"
FRAME_PATH: Final[str] = "/frame"
MAX_MCP_REQUEST_BYTES: Final[int] = 256 * 1024

OPEN_TOOL: Final[str] = "browser_open"
SNAPSHOT_TOOL: Final[str] = "browser_snapshot"
EVAL_TOOL: Final[str] = "browser_eval"
INTERACT_TOOL: Final[str] = "browser_interact"
SCREENSHOT_TOOL: Final[str] = "browser_screenshot"
DIAGNOSTICS_TOOL: Final[str] = "browser_diagnostics"

#: Every tool this server offers, in the order a verification actually goes.
TOOL_NAMES: Final[tuple[str, ...]] = (
    OPEN_TOOL,
    SNAPSHOT_TOOL,
    EVAL_TOOL,
    INTERACT_TOOL,
    SCREENSHOT_TOOL,
    DIAGNOSTICS_TOOL,
)


def _tool(
    name: str,
    title: str,
    schema: dict[str, Any],
    *,
    read_only: bool,
    idempotent: bool,
) -> types.Tool:
    return types.Tool(
        name=name,
        title=title,
        description=schema["description"],
        input_schema=schema,
        annotations=types.ToolAnnotations(
            title=title,
            read_only_hint=read_only,
            # Nothing here deletes anything; the destructive axis is about the
            # caller's world, and this browser has no durable one.
            destructive_hint=False,
            idempotent_hint=idempotent,
            # It reaches sites, so the world it can touch is open -- bounded by
            # the destination guard rather than by this flag.
            open_world_hint=True,
        ),
    )


TOOLS: Final[tuple[types.Tool, ...]] = (
    _tool(
        OPEN_TOOL, "Open a page", OPEN_INPUT_SCHEMA, read_only=False, idempotent=True
    ),
    _tool(
        SNAPSHOT_TOOL,
        "Read the page structure",
        SNAPSHOT_INPUT_SCHEMA,
        read_only=True,
        idempotent=True,
    ),
    _tool(
        EVAL_TOOL,
        "Evaluate JavaScript in the page",
        EVAL_INPUT_SCHEMA,
        # It can mutate the page it runs in, and a graph replay must not assume
        # otherwise.
        read_only=False,
        idempotent=False,
    ),
    _tool(
        INTERACT_TOOL,
        "Click, type, scroll",
        INTERACT_INPUT_SCHEMA,
        read_only=False,
        idempotent=False,
    ),
    _tool(
        SCREENSHOT_TOOL,
        "Look at the page",
        SCREENSHOT_INPUT_SCHEMA,
        read_only=True,
        idempotent=True,
    ),
    _tool(
        DIAGNOSTICS_TOOL,
        "What the page reported",
        DIAGNOSTICS_INPUT_SCHEMA,
        # It drains the buffers, so calling it twice does not give the same
        # answer -- saying otherwise would invite a replay that loses entries.
        read_only=True,
        idempotent=False,
    ),
)

LifespanState = dict[str, Any]


def create_server(
    session: BrowserSession, decisions: DecisionSource
) -> Server[LifespanState]:
    """Build a server over one browser session and the proxy guarding it."""

    async def list_tools(
        context: ServerRequestContext[LifespanState],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        del context, params
        return types.ListToolsResult(tools=list(TOOLS))

    async def call_tool(
        context: ServerRequestContext[LifespanState],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        del context
        arguments = params.arguments or {}
        try:
            return await _dispatch(session, decisions, params.name, arguments)
        except BrowserInputError as error:
            return _error(f"invalid {params.name} request: {error}")
        except TimeoutError:
            return _error(
                f"{params.name} timed out; the page may still be loading or a "
                "script may not have settled"
            )
        except Exception as error:
            return _error(f"{params.name} failed: {error}")

    return Server(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def _dispatch(
    session: BrowserSession,
    decisions: DecisionSource,
    name: str,
    arguments: dict[str, Any],
) -> types.CallToolResult:
    if name == OPEN_TOOL:
        request = parse_open(arguments)
        # The session owns where a workspace path resolves to, because the
        # session is what was told the root. Spelling the mount point here
        # again is what made this open a non-existent file on the native path.
        target = (
            request.url
            if request.url is not None
            else session.workspace_url(request.workspace_path or "")
        )
        outcome = await session.open(target, request.timeout_ms)
        return _text(
            describe(
                {
                    "url": outcome.url,
                    "title": outcome.title,
                    "status": outcome.status,
                    "console_errors_during_load": outcome.console_errors,
                }
            )
        )

    if name == SNAPSHOT_TOOL:
        limit = int(arguments.get("max_chars", 40_000))
        return _text(await session.snapshot(limit))

    if name == EVAL_TOOL:
        request = parse_eval(arguments)
        value = await session.evaluate(request.expression, request.timeout_ms)
        return _text(describe(value))

    if name == INTERACT_TOOL:
        request = parse_interact(arguments)
        done = await session.interact(request.actions, request.timeout_ms)
        return _text("\n".join(done))

    if name == SCREENSHOT_TOOL:
        image = await session.screenshot(
            full_page=bool(arguments.get("full_page", False)),
            quality=int(arguments.get("quality", 70)),
        )
        import base64

        return types.CallToolResult(
            content=[
                types.ImageContent(
                    data=base64.b64encode(image).decode("ascii"),
                    mime_type="image/jpeg",
                )
            ]
        )

    if name == DIAGNOSTICS_TOOL:
        limit = int(arguments.get("limit", 100))
        entries, dropped = session.drain_logs(limit)
        body = [render_logs(entries, dropped)]

        judged = await decisions.take()
        if judged is None:
            # Distinct from "nothing was refused", and the difference decides
            # whether a quiet answer can be trusted. Under Compose the guard
            # lives in another container (ADR-0113 §3.3), so this read can fail
            # on its own; flattening that into an empty list would tell the
            # model its page is fine when nobody checked.
            body.append(
                "\nThe destination guard could not be reached, so this answer "
                "does not say whether any request was refused."
            )
            return _text("\n".join(body))

        taken, decisions_dropped = judged
        refused = [
            f"{decision.method} {decision.host}:{decision.port} -- {decision.detail}"
            for decision in taken
            if not decision.allowed
        ]
        if refused:
            # Shown apart from console noise: a refused destination is a
            # decision this deployment made, not a bug in the page, and the
            # model needs to tell those apart.
            body.append("\nDestinations refused by the guard:\n" + "\n".join(refused))
        if decisions_dropped:
            body.append(f"\n({decisions_dropped} earlier decisions dropped)")
        return _text("\n".join(body))

    return _error(f"unknown tool {name!r}")


def create_app(
    session: BrowserSession,
    decisions: DecisionSource,
    *,
    host: str = "127.0.0.1",
) -> Starlette:
    """The Streamable HTTP app, plus readiness and the console's frame route."""

    async def health(request: Request) -> JSONResponse:
        del request
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVER_NAME,
                "transport": "streamable-http",
                "tools": list(TOOL_NAMES),
                "has_frame": session.latest_frame() is not None,
            }
        )

    async def frame(request: Request) -> Response:
        """The latest screencast frame, for the console panel (§3.6).

        Read-only and unconditional: there is no way to steer the browser from
        here, which is what keeps "a person is watching" from turning into "two
        operators are driving" (§4).
        """

        del request
        image = session.latest_frame()
        if image is None:
            return Response(status_code=204)
        return Response(
            image,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    return create_server(session, decisions).streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=False,
        stateless_http=True,
        max_request_body_size=MAX_MCP_REQUEST_BYTES,
        host=host,
        custom_starlette_routes=[
            Route(HEALTH_PATH, endpoint=health, methods=["GET"]),
            Route(FRAME_PATH, endpoint=frame, methods=["GET"]),
        ],
    )


def _text(body: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=body)])


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=message)], is_error=True
    )


__all__ = [
    "DIAGNOSTICS_TOOL",
    "EVAL_TOOL",
    "FRAME_PATH",
    "HEALTH_PATH",
    "INTERACT_TOOL",
    "MCP_PATH",
    "OPEN_TOOL",
    "SCREENSHOT_TOOL",
    "SERVER_NAME",
    "SNAPSHOT_TOOL",
    "TOOLS",
    "TOOL_NAMES",
    "create_app",
    "create_server",
]
