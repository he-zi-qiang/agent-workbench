"""The protocol surface: what a model sees, and what a refusal looks like to it."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp import Client
from starlette.testclient import TestClient

from agent_workbench.adapters.memory.screen import (
    MAIN_DISPLAY,
    SECOND_DISPLAY,
    FakeScreen,
)
from agent_workbench.apps.computer_mcp.server import (
    HEALTH_PATH,
    create_app,
    create_server,
)
from agent_workbench.domain.computer import ApplicationIdentity

NOTES = ApplicationIdentity(bundle_id="com.apple.Notes", name="Notes")
TERMINAL = ApplicationIdentity(bundle_id="com.apple.Terminal", name="Terminal")
#: Never granted in this file, and that is its whole job here.
MAIL = ApplicationIdentity(bundle_id="com.apple.mail", name="Mail")


def _ask(*applications: ApplicationIdentity) -> tuple[str, dict[str, Any]]:
    return (
        "request_access",
        {
            "applications": [
                {"bundle_id": held.bundle_id, "name": held.name}
                for held in applications
            ]
        },
    )


async def _approves(*_: object, **__: object) -> bool:
    """A person who says yes, without a dialog.

    Every server in this file is built with an explicit answerer. Reaching the
    real one would open a modal window on whoever is running the suite and hold
    the run until they noticed -- and on a machine with no `osascript` it fails
    outright, which is how CI caught this being missing.
    """

    return True


async def _refuses(*_: object, **__: object) -> bool:
    return False


def _session(
    screen: FakeScreen,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    consent: object = None,
) -> list[Any]:
    async def scenario() -> list[Any]:
        results: list[Any] = []
        async with Client(
            create_server(screen, consent=consent or _approves),  # type: ignore[arg-type]
            cache=None,
            raise_exceptions=True,
        ) as client:
            for name, arguments in calls:
                results.append(await client.call_tool(name, arguments))
        return results

    return asyncio.run(scenario())


def test_the_tools_are_the_eight_this_server_declares() -> None:
    """Six until 2026-08-28, and the two that were missing were the two a task
    needed to get past its first application (ADR-091)."""

    async def scenario() -> Any:
        async with Client(
            create_server(FakeScreen(), consent=_approves),
            cache=None,
            raise_exceptions=True,
        ) as client:
            return await client.list_tools()

    names = [tool.name for tool in asyncio.run(scenario()).tools]
    assert names == [
        "request_access",
        "list_granted_applications",
        "activate_application",
        "screenshot",
        "left_click",
        "type",
        "key",
        "scroll",
    ]


def test_no_tool_tells_the_model_the_tool_does_not_work() -> None:
    """The regression for a capability that shipped dead.

    ADR-092 made ``activate_application`` work -- 15/15 on real hardware, in
    the same commit that closed F-30. That commit left the tool's own
    description saying "KNOWN NOT TO WORK ON macOS AS THIS SERVER IS DEPLOYED
    (F-30) ... do not retry it". A tool description is the only thing a model
    reads before deciding whether to call something, so for as long as that
    paragraph stood, the capability was unreachable by the one reader it was
    built for, and nothing anywhere failed.

    Pinned as a prohibition rather than as the current wording, because the
    wording should stay free to change and the claim must not come back. It
    covers every tool: the next one to be fixed will have the same paragraph in
    the same place.
    """

    async def scenario() -> Any:
        async with Client(
            create_server(FakeScreen(), consent=_approves),
            cache=None,
            raise_exceptions=True,
        ) as client:
            return await client.list_tools()

    for tool in asyncio.run(scenario()).tools:
        description = tool.description or ""
        assert "KNOWN NOT TO WORK" not in description, tool.name
        assert "do not retry it" not in description, tool.name


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
    app = create_app(host="testserver", screen=FakeScreen(), consent=_approves)
    with TestClient(app) as client:  # pyright: ignore[reportArgumentType]
        body = client.get(HEALTH_PATH).json()

    assert body["status"] == "ok"
    assert body["displays"] == 1
    # Named rather than assumed: "the window was filtered before the frame was
    # composed" and "the window was painted over afterwards" are different
    # promises, and a caller that needs the first has to be able to ask.
    assert body["capabilities"] == ["exclude_native"]


def test_a_person_saying_no_is_an_error_result_the_model_can_read() -> None:
    """A refused grant is an answer, not a transport failure.

    The model asked for something and is owed a sentence it can act on. A
    protocol error would surface as a broken tool call instead, which reads
    like the server is malfunctioning rather than like a person declining.
    """

    screen = FakeScreen(focus=NOTES)
    results = _session(
        screen,
        [
            (
                "request_access",
                {
                    "applications": [
                        {"bundle_id": NOTES.bundle_id, "name": NOTES.name}
                    ],
                    "reason": "整理今天的会议纪要",
                },
            )
        ],
        consent=_refuses,
    )

    assert results[0].is_error is True
    assert "did not approve" in results[0].content[0].text


def test_nothing_is_touched_after_a_refusal() -> None:
    """The refusal is not advice about how to try harder.

    Asserted on the screen rather than on the message, because the message is
    what a model reads and this is what a person's machine experiences.
    """

    screen = FakeScreen(focus=NOTES)
    results = _session(
        screen,
        [
            (
                "request_access",
                {"applications": [{"bundle_id": NOTES.bundle_id, "name": NOTES.name}]},
            ),
            ("left_click", {"x": 10, "y": 10}),
            ("screenshot", {}),
        ],
        consent=_refuses,
    )

    assert all(result.is_error for result in results)
    assert screen.actions == []


def test_a_click_carries_the_display_it_was_measured_on() -> None:
    """End to end, through the wire format the model actually speaks.

    The gate's own tests prove the arithmetic; this proves the id survives the
    tool schema and the dispatch, which is where an optional field is quietly
    dropped (ADR-090).
    """

    screen = FakeScreen(focus=NOTES, screens=(MAIN_DISPLAY, SECOND_DISPLAY))
    _, clicked = _session(
        screen,
        [
            (
                "request_access",
                {"applications": [{"bundle_id": NOTES.bundle_id, "name": NOTES.name}]},
            ),
            ("left_click", {"x": 300, "y": 200, "display_id": 2}),
        ],
    )

    assert clicked.is_error is False
    assert screen.actions == [("click", (1770, 76, "left", 1))]


def test_a_screenshot_names_the_display_to_send_back_with_coordinates() -> None:
    """Half of the discipline is arithmetic; this is the other half.

    Claude Desktop states this in the tool description -- coordinates refer to
    a named capture, never to whatever was looked at last. A conversion nothing
    tells the model to feed is a conversion it will feed the wrong number.
    """

    screen = FakeScreen(focus=NOTES, screens=(SECOND_DISPLAY, MAIN_DISPLAY))
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
    assert "display_id=2" in shot.content[0].text
    assert shot.structured_content["display_id"] == 2


def test_a_coordinate_off_the_screen_is_an_error_result_and_no_click() -> None:
    screen = FakeScreen(focus=NOTES)
    _, clicked = _session(
        screen,
        [
            (
                "request_access",
                {"applications": [{"bundle_id": NOTES.bundle_id, "name": NOTES.name}]},
            ),
            ("left_click", {"x": 4000, "y": 10}),
        ],
    )

    assert clicked.is_error is True
    assert "not a point on display 1" in clicked.content[0].text
    assert screen.actions == []


def test_reading_the_approved_list_opens_no_dialog() -> None:
    """The reason this tool exists.

    Without it the only way to learn what is approved is to call
    request_access again, which puts a second dialog in front of a person who
    already decided once -- and a person asked twice approves the second one
    without reading it.
    """

    asked: list[object] = []

    async def counting(*args: object, **kwargs: object) -> bool:
        asked.append((args, kwargs))
        return True

    screen = FakeScreen(focus=TERMINAL)
    _, listed = _session(
        screen,
        [_ask(NOTES, TERMINAL), ("list_granted_applications", {})],
        consent=counting,
    )

    assert listed.is_error is False
    text = listed.content[0].text
    assert "Notes (com.apple.Notes): tier full" in text
    assert "Terminal (com.apple.Terminal): tier click" in text
    assert "<- frontmost" in text
    assert len(asked) == 1
    assert screen.actions == []


def test_an_empty_approved_list_is_an_answer_rather_than_an_error() -> None:
    """ "Nothing yet" is the true answer to a legitimate question. An error
    result reads as a broken server and sends a model looking for another
    route."""

    [listed] = _session(FakeScreen(focus=NOTES), [("list_granted_applications", {})])

    assert listed.is_error is False
    assert "No application has been approved" in listed.content[0].text
    assert "request_access" in listed.content[0].text


def test_the_list_does_not_name_a_window_nobody_approved() -> None:
    """It reports *whether* an approved application is in front, never what is
    in front when the answer is no -- that would make this a way to read what
    the person is doing."""

    _, listed = _session(
        FakeScreen(focus=MAIL), [_ask(NOTES), ("list_granted_applications", {})]
    )

    text = listed.content[0].text
    assert "None of them is frontmost" in text
    assert "Mail" not in text
    assert "<- frontmost" not in text


def test_activation_is_how_a_task_reaches_its_second_application() -> None:
    """Before ADR-091 this sequence had no second step: every other tool acts
    on whatever is frontmost, and nothing could change what that was."""

    screen = FakeScreen(focus=NOTES, installed=(NOTES, TERMINAL))
    _, activated = _session(
        screen,
        [
            _ask(NOTES, TERMINAL),
            ("activate_application", {"bundle_id": TERMINAL.bundle_id}),
        ],
    )

    assert activated.is_error is False
    text = activated.content[0].text
    assert "Terminal is frontmost, at tier click" in text
    # The reminder is not decoration: every coordinate the model already holds
    # was measured against a different window.
    assert "Take a screenshot" in text


def test_activation_is_refused_while_somebody_is_using_another_window() -> None:
    screen = FakeScreen(focus=MAIL, installed=(NOTES, MAIL))
    _, activated = _session(
        screen,
        [_ask(NOTES), ("activate_application", {"bundle_id": NOTES.bundle_id})],
    )

    assert activated.is_error is True
    message = activated.content[0].text
    assert "frontmost right now is not in this session's approved list" in message
    assert "Mail" not in message
    assert screen.actions == []


def test_activation_will_not_start_an_application_that_is_not_running() -> None:
    """A refusal rather than a launch, and it says which so the model does not
    go looking for something on this machine that starts applications."""

    screen = FakeScreen(focus=NOTES, installed=(NOTES,))
    _, activated = _session(
        screen,
        [
            _ask(NOTES, TERMINAL),
            ("activate_application", {"bundle_id": TERMINAL.bundle_id}),
        ],
    )

    assert activated.is_error is True
    assert "is not running" in activated.content[0].text
    assert "Ask the person to open it" in activated.content[0].text
