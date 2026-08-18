"""The protocol surface: what a model sees, and what a refusal looks like to it."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp import Client
from starlette.testclient import TestClient

from agent_workbench.adapters.memory.screen import FakeScreen
from agent_workbench.apps.computer_mcp.server import (
    HEALTH_PATH,
    create_app,
    create_server,
)
from agent_workbench.domain.computer import ApplicationIdentity

NOTES = ApplicationIdentity(bundle_id="com.apple.Notes", name="Notes")
TERMINAL = ApplicationIdentity(bundle_id="com.apple.Terminal", name="Terminal")


def _session(screen: FakeScreen, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    async def scenario() -> list[Any]:
        results: list[Any] = []
        async with Client(
            create_server(screen), cache=None, raise_exceptions=True
        ) as client:
            for name, arguments in calls:
                results.append(await client.call_tool(name, arguments))
        return results

    return asyncio.run(scenario())


def test_the_tools_are_the_six_this_server_declares() -> None:
    async def scenario() -> Any:
        async with Client(
            create_server(FakeScreen()), cache=None, raise_exceptions=True
        ) as client:
            return await client.list_tools()

    names = [tool.name for tool in asyncio.run(scenario()).tools]
    assert names == [
        "request_access",
        "screenshot",
        "left_click",
        "type",
        "key",
        "scroll",
    ]


def test_granting_answers_with_the_tier_each_application_got() -> None:
    """The person approves names; the reply is where the model learns what
    those names bought it, before it tries something that will be refused."""

    screen = FakeScreen(focus=NOTES)
    [granted] = _session(
        screen,
        [
            (
                "request_access",
                {
                    "applications": [
                        {"bundle_id": NOTES.bundle_id, "name": NOTES.name},
                        {"bundle_id": TERMINAL.bundle_id, "name": TERMINAL.name},
                    ]
                },
            )
        ],
    )

    text = granted.content[0].text
    assert "Notes (com.apple.Notes): tier full" in text
    assert "Terminal (com.apple.Terminal): tier click" in text
    # And the boundary the desktop teardown draws explicitly: approving
    # applications is not approving screen recording.
    assert "does not include permission to record the screen" in text


def test_a_refusal_is_an_error_result_and_not_a_protocol_error() -> None:
    """The model asked a legitimate question and the answer is no, with
    reasons it is meant to read. A protocol error would give it a stack trace
    instead of a remedy."""

    screen = FakeScreen(focus=TERMINAL)
    _, typed = _session(
        screen,
        [
            (
                "request_access",
                {
                    "applications": [
                        {"bundle_id": TERMINAL.bundle_id, "name": TERMINAL.name}
                    ]
                },
            ),
            ("type", {"text": "rm -rf /"}),
        ],
    )

    assert typed.is_error is True
    message = typed.content[0].text
    assert "sandbox tool" in message
    assert "Do not attempt to work around this restriction" in message
    assert not any(action == "type" for action, _ in screen.actions)


def test_a_screenshot_says_which_space_the_coordinates_are_in() -> None:
    """The most reliable way to get every later click wrong.

    The image is smaller than the display, so a model that measures a button in
    image pixels and clicks there misses by the ratio -- silently, because a
    click that misses is still a click.
    """

    screen = FakeScreen(focus=NOTES)
    _, shot = _session(
        screen,
        [
            (
                "request_access",
                {"applications": [{"bundle_id": NOTES.bundle_id, "name": NOTES.name}]},
            ),
            ("screenshot", {}),
        ],
    )

    assert shot.is_error is False
    assert "Give coordinates in points" in shot.content[0].text
    assert shot.content[1].mime_type == "image/jpeg"
    body = shot.structured_content
    assert body["point_width"] == 1470
    assert body["image_width"] <= 1470


def test_an_ungranted_screen_refuses_before_it_touches_anything() -> None:
    screen = FakeScreen(focus=NOTES)
    [clicked] = _session(screen, [("left_click", {"x": 5, "y": 5})])

    assert clicked.is_error is True
    assert "not in this session's approved list" in clicked.content[0].text
    assert screen.actions == []


def test_health_reports_what_this_process_can_actually_do() -> None:
    app = create_app(host="testserver", screen=FakeScreen())
    with TestClient(app) as client:  # pyright: ignore[reportArgumentType]
        body = client.get(HEALTH_PATH).json()

    assert body["status"] == "ok"
    assert body["displays"] == 1
    # Named rather than assumed: "the window was filtered before the frame was
    # composed" and "the window was painted over afterwards" are different
    # promises, and a caller that needs the first has to be able to ask.
    assert body["capabilities"] == ["exclude_native"]
