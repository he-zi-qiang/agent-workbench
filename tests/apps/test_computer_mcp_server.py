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
from agent_workbench.apps.computer_mcp.gate import ScreenGate
from agent_workbench.apps.computer_mcp.server import (
    HEALTH_PATH,
    SESSION_PATH,
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


def test_no_tool_ever_names_an_unapproved_frontmost_application() -> None:
    """ADR-095 §2's boundary, checked instead of declared.

    The person may be told what is in front of them -- it is their screen, and
    the console panel is where that goes. A model may not: naming it turns a
    refusal into a reading of what the person is doing, which is the thing the
    allowlist exists to prevent.

    That rule held in two of its three places until ADR-095: the activation
    refusal withheld the name and had a test, the read tool refused to
    distinguish "something unapproved is in front" from "nothing is", and
    `_require_frontmost` -- the path every click, keystroke and scroll takes --
    printed it. So the whole rule was worth exactly one refused click.

    This drives **every tool this server declares** against a session where the
    person has approved Notes and then switched to Mail, and asserts the name
    and the bundle id appear in none of the answers. Per-tool assertions would
    have to be remembered for the ninth tool; this one fails on its own.
    """

    screen = FakeScreen(focus=MAIL, installed=(NOTES, TERMINAL, MAIL))
    results = _session(
        screen,
        [
            _ask(NOTES),
            # Every tool the server declares, in the order it declares them.
            # `request_access` is the first call above.
            ("list_granted_applications", {}),
            ("activate_application", {"bundle_id": NOTES.bundle_id}),
            ("screenshot", {}),
            ("left_click", {"x": 10, "y": 10}),
            ("type", {"text": "hello"}),
            ("key", {"key": "return"}),
            ("scroll", {"x": 10, "y": 10, "direction": "down"}),
        ],
    )

    spoken = "\n".join(
        block.text
        for result in results
        for block in result.content
        if getattr(block, "text", None) is not None
    )
    assert "Mail" not in spoken
    assert MAIL.bundle_id not in spoken
    # The control: this session really was in the situation the rule is about,
    # so the assertions above are not passing because nothing was refused.
    assert "not in this session's approved list" in spoken

    # No input was synthesized -- click, type, key and scroll were all refused
    # by check 3 before reaching the port.
    kinds = [kind for kind, _ in screen.actions]
    assert kinds == ["capture"]
    # And the one action that *did* happen is the one the tier table
    # deliberately does not gate: a screenshot is what approving is *for*. It
    # is still narrowed by the allowlist rather than by a tier -- the capture
    # names only the approved application, so the window the person switched to
    # is not in the picture either (ADR-076). Asserted here because this test
    # is about what a model can learn, and a screenshot that included Mail
    # would answer the same question the refusals above refuse to.
    [(_, capture_args)] = screen.actions
    assert capture_args[-1] == (NOTES.bundle_id,)


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


# --- the one route that describes a person ----------------------------------


def test_the_session_route_says_what_was_approved_and_what_is_in_front() -> None:
    """The panel's whole data source (ADR-095 §5).

    `/health` says how many screens this machine has; this one says what a
    person approved and what they are looking at. Different kind of route,
    different name.
    """

    screen = FakeScreen(focus=NOTES, installed=(NOTES, TERMINAL))
    app = create_app(host="testserver", screen=screen, consent=_approves)
    with TestClient(app) as client:  # pyright: ignore[reportArgumentType]
        empty = client.get(SESSION_PATH).json()

    assert empty["granted"] == []
    # Named even before anything is approved: an empty allowlist and an
    # unreadable machine are different states, and the console has to be able
    # to tell them apart.
    assert empty["frontmost"]["name"] == "Notes"
    assert empty["frontmost"]["granted"] is False
    assert empty["actions"] == []


def test_the_session_route_calls_the_allowlist_the_process_and_not_the_session() -> (
    None
):
    """One word, and it is load bearing.

    `ScreenGate` holds one allowlist per **process**, not per MCP session
    (known-gap F-19). A payload captioned "session" would be the first place
    somebody read a session-scoped grant into existence, and the panel above it
    would inherit the mistake.
    """

    app = create_app(host="testserver", screen=FakeScreen(), consent=_approves)
    with TestClient(app) as client:  # pyright: ignore[reportArgumentType]
        body = client.get(SESSION_PATH).json()

    assert body["scope"] == "process"


def test_the_person_is_told_which_window_the_model_was_not() -> None:
    """ADR-095 in one test, from both sides at once.

    The same session, the same moment: the person has approved Notes and has
    switched to Mail. The model is refused with a message that does not say
    what it switched to; the panel's route says "Mail" -- because the reader of
    that route is the person sitting in front of that very window, and the
    decision they have to make is whether to approve it.
    """

    screen = FakeScreen(focus=MAIL, installed=(NOTES, MAIL))
    app = create_app(host="testserver", screen=screen, consent=_approves)

    with TestClient(app) as client:  # pyright: ignore[reportArgumentType]
        body = client.get(SESSION_PATH).json()

    assert body["frontmost"]["name"] == "Mail"
    assert body["frontmost"]["bundle_id"] == MAIL.bundle_id
    assert body["frontmost"]["granted"] is False


def test_the_session_route_is_read_only() -> None:
    """Spelled out rather than trusted.

    There is no shape of request to this route that should change anything, and
    a handler added later would be the kind of change nobody reviews as one.
    """

    app = create_app(host="testserver", screen=FakeScreen(), consent=_approves)
    with TestClient(app) as client:  # pyright: ignore[reportArgumentType]
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)(SESSION_PATH).status_code == 405


def test_the_tools_and_the_session_route_read_one_gate() -> None:
    """The link this whole step exists to make.

    `create_app` builds one gate and hands it to both the MCP server and
    `/session`, so the panel describes the same allowlist the model is being
    judged against. Two gates would be two allowlists, and the console would
    report a session that was not the one running.

    Injected here rather than reached into, because that property is invisible
    from outside when the only instance is one `create_app` keeps to itself.
    What this does **not** cover is the default construction -- one line, whose
    failure mode is `/session` answering empty forever, which is what the
    browser check at the end of this work is for.
    """

    screen = FakeScreen(focus=NOTES, installed=(NOTES,))
    gate = ScreenGate(screen=screen, consent=_approves)  # type: ignore[arg-type]
    app = create_app(host="testserver", screen=screen, gate=gate)

    async def scenario() -> None:
        async with Client(
            create_server(screen, gate=gate), cache=None, raise_exceptions=True
        ) as client:
            await client.call_tool(*_ask(NOTES))
            await client.call_tool("left_click", {"x": 4, "y": 5})

    asyncio.run(scenario())
    with TestClient(app) as client:  # pyright: ignore[reportArgumentType]
        body = client.get(SESSION_PATH).json()

    assert [held["bundle_id"] for held in body["granted"]] == [NOTES.bundle_id]
    # `request_access` is a person answering a dialog, not something done to the
    # screen, so it is not an action. The click is.
    assert [held["action"] for held in body["actions"]] == ["left_click"]
    [action] = body["actions"]
    assert action["allowed"] is True
    assert action["application"]["name"] == "Notes"
    assert "(4, 5)" in action["detail"]


def test_a_refused_action_reaches_the_panel_with_the_window_it_was_refused_on() -> None:
    """The row the panel exists for.

    A task stops, and the reason is that the person switched to something the
    task was never told about. The model's refusal does not say which window;
    this row does, because the person reading it is the one sitting in it.
    """

    screen = FakeScreen(focus=MAIL, installed=(NOTES, MAIL))
    gate = ScreenGate(screen=screen, consent=_approves)  # type: ignore[arg-type]
    app = create_app(host="testserver", screen=screen, gate=gate)

    async def scenario() -> None:
        async with Client(
            create_server(screen, gate=gate), cache=None, raise_exceptions=True
        ) as client:
            await client.call_tool(*_ask(NOTES))
            await client.call_tool("left_click", {"x": 4, "y": 5})

    asyncio.run(scenario())
    with TestClient(app) as client:  # pyright: ignore[reportArgumentType]
        body = client.get(SESSION_PATH).json()

    [action] = body["actions"]
    assert action["allowed"] is False
    assert action["application"]["name"] == "Mail"
    assert "not in this session's approved list" in action["reason"]
