"""The project-owned computer-use MCP server.

Every tool here is a thin shell over :class:`ScreenGate`. The shells are thin
on purpose: a check performed in a tool handler is a check the next tool
handler forgets, so all four of them live in the gate and every handler goes
through it (ADR-070).

The tool descriptions carry more prose than most in this repository, and that
is deliberate too. They are the only place a model reads *before* it tries
something, and the difference between "left_click" and "left_click, and here is
why it will be refused on a terminal and what to use instead" is the difference
between one refusal and a model looking for a way around one.
"""

from __future__ import annotations

import base64
from typing import Any, Final

from mcp import types
from mcp.server import Server, ServerRequestContext
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_workbench.apps.computer_mcp.gate import ScreenGate, ScreenRefusedError
from agent_workbench.domain.computer import ApplicationIdentity
from agent_workbench.ports.screen import ScreenPort, ScreenUnavailableError

SERVER_NAME: Final[str] = "agent-workbench-computer"
SERVER_VERSION: Final[str] = "1.0.0"
MCP_PATH: Final[str] = "/mcp"
HEALTH_PATH: Final[str] = "/health"

LifespanState = dict[str, Any]

_TIER_NOTE: Final[str] = (
    "Applications are granted at one of three tiers, decided by what the "
    "application is and re-checked against whatever is frontmost at the "
    "moment you act: browsers and anything that moves money are 'read' "
    "(visible, never driven); terminals and IDEs are 'click' (a button may be "
    "pressed, nothing may be typed); everything else is 'full'."
)

_TOOLS: Final[tuple[types.Tool, ...]] = (
    types.Tool(
        name="request_access",
        title="Ask for access to applications",
        description=(
            "Name every application this task needs before touching any of "
            "them. A dialog opens on the person's screen and this call does "
            "not return until they answer it or it times out; a refusal means "
            "none of the list is available and asking again with the same "
            "list will show them the same dialog. " + _TIER_NOTE
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["applications"],
            "properties": {
                "reason": {
                    "type": "string",
                    "maxLength": 400,
                    "description": (
                        "One sentence the person reads while deciding. "
                        "Describe the task, not the mechanism."
                    ),
                },
                "applications": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["bundle_id", "name"],
                        "properties": {
                            "bundle_id": {"type": "string", "maxLength": 256},
                            "name": {"type": "string", "maxLength": 256},
                        },
                    },
                },
            },
        },
    ),
    types.Tool(
        name="screenshot",
        title="Look at a display",
        description=(
            "Capture one display, scaled to fit a vision token budget. "
            "Coordinates for every other tool are in the display's own point "
            "space, which the result reports -- never in the pixels of the "
            "returned image, which is smaller."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"display_id": {"type": "integer"}},
        },
    ),
    types.Tool(
        name="left_click",
        title="Click",
        description=(
            "Click at a point, in display points. Refused on a 'read' tier "
            "application. " + _TIER_NOTE
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["x", "y"],
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "count": {"type": "integer", "minimum": 1, "maximum": 3},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
            },
        },
    ),
    types.Tool(
        name="type",
        title="Type text",
        description=(
            "Type into whatever has keyboard focus. Requires tier 'full': on "
            "a terminal or IDE this is refused, because a keystroke there runs "
            "a command or edits a file, and this project has a sandbox tool "
            "and workspace tools that do both with a policy gate and an audit "
            "trail. If focus moves part-way through, the rest is NOT typed "
            "and the reply says how much was."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string", "maxLength": 4096}},
        },
    ),
    types.Tool(
        name="key",
        title="Press a key combination",
        description=(
            "Press one chord, such as 'cmd+c' or 'Return'. Requires tier "
            "'full', for the same reason as typing."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["combination"],
            "properties": {"combination": {"type": "string", "maxLength": 64}},
        },
    ),
    types.Tool(
        name="scroll",
        title="Scroll",
        description="Scroll at a point. Permitted at tier 'click' and above.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["x", "y", "direction"],
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                },
                "amount": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    ),
)


def create_server(screen: ScreenPort) -> Server[LifespanState]:
    """Build a server over one screen. The gate's grants live for its life."""

    gate = ScreenGate(screen=screen)

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
        arguments: dict[str, Any] = dict(params.arguments or {})
        try:
            return await _dispatch(gate, params.name, arguments)
        except ScreenRefusedError as refused:
            # An error *result*, not a protocol error: the model asked a
            # legitimate question and the answer is no, with reasons it is
            # meant to read and act on.
            return _error(str(refused))
        except ScreenUnavailableError as broken:
            return _error(f"the screen is unavailable: {broken}")
        except (KeyError, TypeError, ValueError) as invalid:
            return _error(f"invalid request: {invalid}")

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        title="Agent Workbench computer use",
        description=(
            "Look at this machine's screen and act on it, under a per-session "
            "allowlist and a three-tier permission model."
        ),
        instructions=_TIER_NOTE,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def _dispatch(
    gate: ScreenGate, name: str, arguments: dict[str, Any]
) -> types.CallToolResult:
    if name == "request_access":
        wanted = tuple(
            ApplicationIdentity(
                bundle_id=str(held["bundle_id"]), name=str(held["name"])
            )
            for held in arguments["applications"]
        )
        given = await gate.grant(wanted, reason=str(arguments.get("reason", "")))
        lines = [
            f"{held.application.name} ({held.application.bundle_id}): tier {held.tier}"
            for held in given
        ]
        return _text(
            "A person approved these for this session:\n"
            + "\n".join(lines)
            + "\n\nThis does not include permission to record the screen; the "
            "operating system asks for that separately, once."
        )

    if name == "screenshot":
        display_id = arguments.get("display_id")
        capture = await gate.screenshot(None if display_id is None else int(display_id))
        return types.CallToolResult(
            content=[
                types.TextContent(
                    text=(
                        f"Display {capture.display.display_id}: "
                        f"{capture.display.width}x{capture.display.height} "
                        f"points, shown at {capture.width}x{capture.height}. "
                        "Give coordinates in points."
                    )
                ),
                types.ImageContent(
                    data=base64.b64encode(capture.content).decode("ascii"),
                    mime_type=capture.media_type,
                ),
            ],
            structured_content={
                "display_id": capture.display.display_id,
                "point_width": capture.display.width,
                "point_height": capture.display.height,
                "image_width": capture.width,
                "image_height": capture.height,
            },
        )

    if name == "left_click":
        held = await gate.click(
            int(arguments["x"]),
            int(arguments["y"]),
            button=str(arguments.get("button", "left")),
            count=int(arguments.get("count", 1)),
        )
        return _text(f"Clicked in {held.application.name}.")

    if name == "type":
        text = str(arguments["text"])
        held = await gate.type_text(text)
        return _text(f"Typed {len(text)} characters into {held.application.name}.")

    if name == "key":
        held = await gate.key(str(arguments["combination"]))
        return _text(f"Pressed {arguments['combination']} in {held.application.name}.")

    if name == "scroll":
        held = await gate.scroll(
            int(arguments["x"]),
            int(arguments["y"]),
            direction=str(arguments["direction"]),
            amount=int(arguments.get("amount", 3)),
        )
        return _text(f"Scrolled in {held.application.name}.")

    return _error(f"unknown tool {name}")


def create_app(
    *, host: str = "127.0.0.1", screen: ScreenPort | None = None
) -> Starlette:
    """The loopback app. Builds the platform adapter unless one is supplied."""

    if screen is None:
        from agent_workbench.adapters.screen import for_this_platform

        screen = for_this_platform()
    resolved = screen

    async def health(request: Request) -> JSONResponse:
        del request
        displays = resolved.displays()
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVER_NAME,
                "transport": "streamable-http",
                "displays": len(displays),
                "capabilities": sorted(resolved.capabilities()),
            }
        )

    return create_server(resolved).streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=False,
        host=host,
        custom_starlette_routes=[Route(HEALTH_PATH, endpoint=health, methods=["GET"])],
    )


def _text(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=message)])


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=message)], is_error=True
    )


__all__ = [
    "HEALTH_PATH",
    "MCP_PATH",
    "SERVER_NAME",
    "create_app",
    "create_server",
]
