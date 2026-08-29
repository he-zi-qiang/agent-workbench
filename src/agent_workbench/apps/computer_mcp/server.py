"""The project-owned computer-use MCP server.

Every tool here is a thin shell over :class:`ScreenGate`. The shells are thin
on purpose: a check performed in a tool handler is a check the next tool
handler forgets, so all four of them live in the gate and every handler goes
through it (ADR-070). The two checks ADR-091 added for activation live there
for the same reason, and the handler below is three lines because of it.

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

from agent_workbench.apps.computer_mcp.gate import (
    ConsentAsker,
    ScreenGate,
    ScreenRefusedError,
)
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
        name="list_granted_applications",
        title="Read this session's approved list",
        description=(
            "What a person has already approved in this session, and at which "
            "tier. Reads state and changes none: nothing is shown to anybody "
            "and no dialog opens, which is what makes it the right way to "
            "answer 'what may I touch' -- calling request_access again to find "
            "out would put a second dialog in front of a person who has "
            "already decided once. Also says whether one of them is frontmost "
            "right now, because that is the check every other tool here fails "
            "on; it never says what is in front when the answer is something "
            "nobody approved."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    ),
    types.Tool(
        name="activate_application",
        title="Bring an approved application to the front",
        description=(
            "Make one already-approved, already-running application frontmost, "
            "which is the only way to reach a second application: every other "
            "tool here acts on whatever is in front, and refuses when that is "
            "not something the person approved.\n"
            "Refused in three cases worth knowing before you call it. The "
            "target is not in the approved list -- call request_access. The "
            "target is approved but not running -- this brings an application "
            "forward and never starts one, so ask the person to open it. Or "
            "something nobody approved is frontmost at this moment: a person "
            "is using a window that is not part of this task, and taking the "
            "screen from it is their decision rather than yours. Do not poll "
            "for that last one to clear.\n"
            "KNOWN NOT TO WORK ON macOS AS THIS SERVER IS DEPLOYED (F-30). "
            "Measured 2026-08-29 on macOS 26.5.2 with both screen grants "
            "held: no available call changes the frontmost application from a "
            'process of this shape, so this tool refuses with "was asked to '
            'come to the front and did not" every time. It is described in '
            "full here rather than quietly removed because the gate around it "
            "is real and the refusal is honest -- but do not build a plan on "
            "it reaching a second application, and do not retry it."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["bundle_id"],
            "properties": {
                "bundle_id": {
                    "type": "string",
                    "maxLength": 256,
                    "description": (
                        "The bundle id exactly as request_access or "
                        "list_granted_applications reported it."
                    ),
                }
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
            "returned image, which is smaller. Pass the display_id this "
            "reports back with those coordinates: a point measured here names "
            "a different place on any other screen."
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
            "Click at a point, in the points of the display the screenshot "
            "came from. Refused on a 'read' tier application. " + _TIER_NOTE
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
                "display_id": {
                    "type": "integer",
                    "description": (
                        "The display these coordinates were measured on, as "
                        "the screenshot reported it. Omit for the main "
                        "display. A point that is not on the named display is "
                        "refused rather than clicked somewhere else."
                    ),
                },
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
        description=(
            "Scroll at a point, in the points of the display the screenshot "
            "came from. Permitted at tier 'click' and above."
        ),
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
                "display_id": {
                    "type": "integer",
                    "description": (
                        "The display these coordinates were measured on. Omit "
                        "for the main display."
                    ),
                },
            },
        },
    ),
)


def create_server(
    screen: ScreenPort,
    *,
    consent: ConsentAsker | None = None,
) -> Server[LifespanState]:
    """Build a server over one screen. The gate's grants live for its life.

    ``consent`` is how a person is asked, and it is a parameter so that a test
    can answer without a dialog. A test that reached the real one would open a
    modal window on whoever is running the suite and hold the run until they
    noticed it -- and on a machine with no ``osascript`` it fails outright,
    which is how CI found this. Left unset it is the macOS dialog, because a
    server that granted itself access when nobody wired an approver is exactly
    what ADR-076 replaced.
    """

    gate = (
        ScreenGate(screen=screen)
        if consent is None
        else ScreenGate(screen=screen, consent=consent)
    )

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

    if name == "list_granted_applications":
        held = gate.grants()
        if not held:
            # Not an error result. The model asked a legitimate question and
            # "nothing yet" is the true answer to it -- an error here would
            # read as a broken server and send it looking for another route.
            return _text(
                "No application has been approved in this session yet.\n"
                "Call request_access with the applications you need and wait "
                "for the person to approve them."
            )
        front = gate.frontmost_grant()
        lines = [
            f"{grant.application.name} ({grant.application.bundle_id}): "
            f"tier {grant.tier}"
            + (
                "  <- frontmost"
                if front is not None
                and front.application.bundle_id == grant.application.bundle_id
                else ""
            )
            for grant in held
        ]
        # Both branches name what is *true of the screen*, and neither
        # overstates it: `request_access` opens a dialog and is unaffected by
        # what is in front, so "everything else would be refused" would be a
        # sentence a model could catch this server being wrong about.
        tail = (
            "Clicking, typing, scrolling and screenshots all reach the "
            "frontmost application, which is the marked one."
            if front is not None
            else "None of them is frontmost right now, so clicking, typing "
            "and scrolling would all be refused, and so would bringing one "
            "of these forward -- the screen currently belongs to a window "
            "nobody approved, which is why it is not named here."
        )
        return _text("Approved for this session:\n" + "\n".join(lines) + "\n\n" + tail)

    if name == "activate_application":
        held = await gate.activate(str(arguments["bundle_id"]))
        return _text(
            f"{held.application.name} is frontmost, at tier {held.tier}. "
            "Take a screenshot before acting on it: this changed which window "
            "the coordinates of every other tool land in."
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
                        "Give coordinates in points, measured from the "
                        "top-left of this display, and pass "
                        f"display_id={capture.display.display_id} with them."
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
            display_id=_display_id(arguments),
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
            display_id=_display_id(arguments),
        )
        return _text(f"Scrolled in {held.application.name}.")

    return _error(f"unknown tool {name}")


def create_app(
    *,
    host: str = "127.0.0.1",
    screen: ScreenPort | None = None,
    consent: ConsentAsker | None = None,
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

    return create_server(resolved, consent=consent).streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=False,
        host=host,
        custom_starlette_routes=[Route(HEALTH_PATH, endpoint=health, methods=["GET"])],
    )


def _display_id(arguments: dict[str, Any]) -> int | None:
    """The display a coordinate claims to have been measured on, if any.

    Absent means the main display, which is what a one-screen session will
    always mean and should not have to say. Read in one place rather than at
    each call site: two handlers that spell this differently is how a screen
    ends up being chosen by whichever one was edited last.
    """

    given = arguments.get("display_id")
    return None if given is None else int(given)


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
