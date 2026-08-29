"""The gate: four checks, and the one that is easy to leave out.

Driven against `FakeScreen`, which can do the one thing a real screen cannot be
asked to do on cue -- change which application is frontmost *between* two calls.
That is the whole reason the port exists (ADR-070).
"""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.adapters.memory.screen import (
    MAIN_DISPLAY,
    SECOND_DISPLAY,
    FakeScreen,
)
from agent_workbench.apps.computer_mcp.gate import ScreenGate, ScreenRefusedError
from agent_workbench.domain.computer import ApplicationIdentity

NOTES = ApplicationIdentity(bundle_id="com.apple.Notes", name="Notes")
TERMINAL = ApplicationIdentity(bundle_id="com.apple.Terminal", name="Terminal")
CHROME = ApplicationIdentity(bundle_id="com.google.Chrome", name="Google Chrome")
#: Deliberately never granted anywhere in this file. It stands in for the
#: window a person switched to for a reason this task was not told about.
MAIL = ApplicationIdentity(bundle_id="com.apple.mail", name="Mail")


async def _always(*_: object, **__: object) -> bool:
    """A person who says yes, without a dialog.

    Every gate in this file is built with an explicit answerer, and none of
    them is the real one. A test that reached `consent.ask` would open a modal
    dialog on whoever is running the suite and then block for two minutes
    waiting for them to notice.
    """

    return True


async def _never(*_: object, **__: object) -> bool:
    return False


def _gate(
    screen: FakeScreen,
    *granted: ApplicationIdentity,
    answers: object = None,
) -> ScreenGate:
    gate = ScreenGate(screen=screen, consent=answers or _always)  # type: ignore[arg-type]
    if granted:
        asyncio.run(gate.grant(granted))
    return gate


def test_nothing_is_granted_until_somebody_grants_it() -> None:
    """The starting state refuses, and there is no default to turn off."""

    screen = FakeScreen(focus=NOTES)
    gate = _gate(screen)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.click(10, 10))

    assert "not in this session's approved list" in str(refused.value)
    assert screen.actions == []


def test_the_tier_is_derived_at_grant_time_not_asked_for() -> None:
    """A request that could name its own tier could ask for "full" on Chrome,
    and the person approving a list of names is not reading a tier column."""

    gate = _gate(FakeScreen(focus=NOTES))
    given = asyncio.run(gate.grant((NOTES, TERMINAL, CHROME)))

    assert {held.application.name: held.tier for held in given} == {
        "Notes": "full",
        "Terminal": "click",
        "Google Chrome": "read",
    }


def test_a_granted_terminal_can_be_clicked_and_not_typed_into() -> None:
    screen = FakeScreen(focus=TERMINAL)
    gate = _gate(screen, TERMINAL)

    asyncio.run(gate.click(40, 50))
    assert ("click", (40, 50, "left", 1)) in screen.actions

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.type_text("rm -rf /"))

    assert "sandbox tool" in str(refused.value)
    # And nothing was typed. A gate that refuses after acting is not a gate.
    assert not any(action == "type" for action, _ in screen.actions)


def test_a_granted_browser_is_readable_and_not_clickable() -> None:
    screen = FakeScreen(focus=CHROME)
    gate = _gate(screen, CHROME)

    with pytest.raises(ScreenRefusedError):
        asyncio.run(gate.click(10, 10))
    assert screen.actions == []

    # Looking is what the grant is for, and every tier permits it.
    capture = asyncio.run(gate.screenshot())
    assert capture.content


def test_the_tier_is_re_read_against_whatever_is_frontmost_now() -> None:
    """The check that is easy to leave out and impossible to add later.

    Both applications are granted, so an allowlist-only gate would let this
    through. The permission is about a *window*, and the window changed between
    the grant and the keystroke.
    """

    screen = FakeScreen(focus=NOTES)
    gate = _gate(screen, NOTES, TERMINAL)

    asyncio.run(gate.type_text("hello"))
    assert ("type", "hello") in screen.actions

    screen.focus = TERMINAL
    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.type_text("hello"))

    assert '"click"' in str(refused.value)


def test_focus_moving_mid_string_is_reported_with_a_count() -> None:
    """Keystrokes follow focus, so half a string lands somewhere unapproved.

    `type_limit` stands in for the adapter noticing and stopping. What is
    asserted is the reply: a model told only "denied" retypes the whole string
    and the first half arrives twice.
    """

    moved = iter([NOTES, TERMINAL, TERMINAL])
    screen = FakeScreen(focus=lambda: next(moved), type_limit=4)
    gate = _gate(screen, NOTES, TERMINAL)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.type_text("hello world"))

    message = str(refused.value)
    assert "4 of 11" in message
    assert "NOT delivered" in message
    assert "Terminal" in message


def test_a_short_delivery_without_a_focus_change_still_says_how_much() -> None:
    """The model's problem is identical either way: it does not know what is
    on screen. Same message shape rather than a silent success."""

    screen = FakeScreen(focus=NOTES, type_limit=2)
    gate = _gate(screen, NOTES)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.type_text("hello"))

    assert "2 of 5" in str(refused.value)


def test_a_screenshot_is_fitted_to_the_budget_before_it_is_taken() -> None:
    """Fitted, not taken and then resized: the pixels never leave the adapter.

    The default fake display is 1470x956 points, which is inside both ceilings,
    so this uses a larger one to make the clamp observable.
    """

    from agent_workbench.ports.screen import Display

    big = Display(
        display_id=9, width=3840, height=2160, scale_factor=1.0, origin_x=0, origin_y=0
    )
    screen = FakeScreen(focus=NOTES, screens=(big,))
    gate = _gate(screen, NOTES)

    capture = asyncio.run(gate.screenshot())

    assert capture.width < 3840
    assert gate.budget.fits(capture.width, capture.height)
    # And the display's own dimensions still travel with it, because that is
    # the coordinate space every later click is expressed in.
    assert capture.display.width == 3840


def test_a_platform_that_cannot_exclude_is_refused_rather_than_trusted() -> None:
    """There is always something to exclude, so this branch always applies.

    It used to be unreachable: the gate asked "which applications should be
    kept out", which needs the list of everything running, so the answer was
    always none and the check never fired. Asking instead "which are approved"
    makes every unapproved window something to keep out, and a platform that
    cannot do it has nothing safe to return (ADR-076).
    """

    screen = FakeScreen(focus=NOTES, supports=frozenset())
    gate = _gate(screen, NOTES)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.screenshot())

    assert "at the compositor" in str(refused.value)
    assert screen.actions == []


def test_painting_over_a_window_is_not_accepted_as_keeping_it_out() -> None:
    """`exclude_mask` used to satisfy this check. It is a weaker promise.

    Masking draws the frame and then covers rectangles read from a separate
    window list: the pixels were in the buffer, and the geometry can be stale
    by the time the shutter falls. Accepting it here is what left the second
    half of F-18 open -- 「抓屏是遮盖不是合成器过滤」 -- while the first half
    looked closed.
    """

    screen = FakeScreen(focus=NOTES, supports=frozenset({"exclude_mask"}))
    gate = _gate(screen, NOTES)

    with pytest.raises(ScreenRefusedError):
        asyncio.run(gate.screenshot())


def test_a_capture_shows_the_approved_applications_and_says_which() -> None:
    """The allowlist reaches the port, sorted, so two captures agree."""

    screen = FakeScreen(focus=NOTES)
    gate = _gate(screen, TERMINAL, NOTES)

    assert asyncio.run(gate.screenshot()).content
    captures = [call for call in screen.actions if call[0] == "capture"]
    assert len(captures) == 1
    assert captures[0][1][3] == ("com.apple.Notes", "com.apple.Terminal")


def test_a_session_that_approved_nothing_cannot_look_at_anything() -> None:
    """An empty allowlist used to mean "show the whole desktop".

    Measured 2026-08-24 before the change: a gate with no grants returned a
    full 1375x894 capture of everything on screen. Nothing in the gate stopped
    it, and its own docstring said the opposite was happening.
    """

    screen = FakeScreen(focus=NOTES)
    gate = _gate(screen)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.screenshot())

    assert "no application has been approved" in str(refused.value)
    assert screen.actions == []


def test_a_model_cannot_grant_itself_access() -> None:
    """The check that did not exist until 2026-08-24.

    `grant` used to take the model's own list, write it into the allowlist, and
    answer "approved for this session". Every other check in this file was
    real; all of them were guarding a consent nobody had given. An allowlist a
    model can write has exactly one entry -- whatever it just asked for.
    """

    screen = FakeScreen(focus=NOTES)
    gate = _gate(screen, answers=_never)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.grant((NOTES,)))

    assert "did not approve" in str(refused.value)
    assert gate.grants() == ()
    # And the refusal is not advice about how to try harder.
    assert "never use AppleScript" in str(refused.value)


def test_a_refused_list_grants_none_of_it_rather_than_some() -> None:
    """One decision about one set.

    Approving three applications and keeping the two the person might not have
    minded would be a grant nobody made -- the dialog asked one question.
    """

    gate = _gate(FakeScreen(focus=NOTES), answers=_never)

    with pytest.raises(ScreenRefusedError):
        asyncio.run(gate.grant((NOTES, TERMINAL, CHROME)))

    assert gate.grants() == ()


def test_the_reason_reaches_the_person_deciding() -> None:
    """A dialog listing three applications and no reason is a dialog that gets
    approved out of impatience."""

    seen: list[object] = []

    async def recording(applications: object, *, reason: str = "") -> bool:
        seen.append((applications, reason))
        return True

    gate = ScreenGate(screen=FakeScreen(focus=NOTES), consent=recording)
    asyncio.run(gate.grant((NOTES,), reason="整理今天的会议纪要"))

    assert seen == [((NOTES,), "整理今天的会议纪要")]


def test_a_second_request_asks_again_rather_than_topping_up() -> None:
    """Granted applications stay granted; a new list is a new decision.

    The failure this prevents is a model widening its own reach one refused
    application at a time, where each individual ask looks reasonable.
    """

    answers = iter([True, False])

    async def scripted(*_: object, **__: object) -> bool:
        return next(answers)

    gate = ScreenGate(screen=FakeScreen(focus=NOTES), consent=scripted)
    asyncio.run(gate.grant((NOTES,)))

    with pytest.raises(ScreenRefusedError):
        asyncio.run(gate.grant((TERMINAL,)))

    assert [held.application.name for held in gate.grants()] == ["Notes"]


# --- where a click actually lands (ADR-090, closes F-22) -------------------


def test_a_click_on_the_main_display_is_posted_where_it_was_measured() -> None:
    screen = FakeScreen(focus=NOTES)
    gate = _gate(screen, NOTES)

    asyncio.run(gate.click(300, 200))

    assert screen.actions == [("click", (300, 200, "left", 1))]


def test_a_click_measured_on_a_second_display_is_moved_onto_it() -> None:
    """The bug F-22 named, in one assertion.

    Before ADR-090 the gate handed (300, 200) straight to the adapter, which
    posts into the global space -- so a coordinate read off the *second*
    display's screenshot clicked on the **main** one. Nothing failed: the click
    was delivered, to the wrong place, and reported success.
    """

    screen = FakeScreen(focus=NOTES, screens=(MAIN_DISPLAY, SECOND_DISPLAY))
    gate = _gate(screen, NOTES)

    asyncio.run(gate.click(300, 200, display_id=SECOND_DISPLAY.display_id))

    assert screen.actions == [("click", (1770, 76, "left", 1))]


def test_a_scroll_carries_the_same_conversion_as_a_click() -> None:
    """Separate code path, same mistake available. The adapter moves the cursor
    to this point before turning the wheel, so a wrong one scrolls a window
    nobody named."""

    screen = FakeScreen(focus=NOTES, screens=(MAIN_DISPLAY, SECOND_DISPLAY))
    gate = _gate(screen, NOTES)

    asyncio.run(
        gate.scroll(
            10, 20, direction="down", amount=3, display_id=SECOND_DISPLAY.display_id
        )
    )

    assert screen.actions == [("scroll", (1480, -104, "down", 3))]


def test_on_a_one_screen_machine_omitting_the_display_is_still_fine() -> None:
    """A single-screen session never learns this vocabulary and does not have
    to: there is exactly one right answer, so nothing is being guessed."""

    screen = FakeScreen(focus=NOTES, screens=(MAIN_DISPLAY,))
    gate = _gate(screen, NOTES)

    asyncio.run(gate.click(5, 6))

    assert screen.actions == [("click", (5, 6, "left", 1))]


def test_a_point_that_is_not_on_the_named_display_is_refused_untouched() -> None:
    screen = FakeScreen(focus=NOTES)
    gate = _gate(screen, NOTES)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.click(1470, 100))

    assert "not a point on display 1" in str(refused.value)
    assert screen.actions == []


def test_a_display_this_machine_does_not_have_is_refused_not_substituted() -> None:
    """The fallback that would have been convenient here is the original bug
    wearing a default: acting on a screen other than the one named."""

    screen = FakeScreen(focus=NOTES)
    gate = _gate(screen, NOTES)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.click(5, 5, display_id=7))

    assert "no display 7" in str(refused.value)
    assert "1 (1470x956 points)" in str(refused.value)
    assert screen.actions == []


def test_an_ungranted_session_is_told_about_the_grant_not_about_the_screens() -> None:
    """Order matters, and this is what it buys.

    The geometry step runs after the three permission checks, so a session that
    has been granted nothing cannot use an out-of-range coordinate to measure
    somebody's monitors.
    """

    screen = FakeScreen(focus=NOTES, screens=(MAIN_DISPLAY, SECOND_DISPLAY))
    gate = ScreenGate(screen=screen, consent=_always)  # type: ignore[arg-type]

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.click(99999, 99999, display_id=SECOND_DISPLAY.display_id))

    message = str(refused.value)
    assert "not in this session's approved list" in message
    assert "1920" not in message
    assert screen.actions == []


def test_two_displays_and_no_id_is_refused_rather_than_assumed() -> None:
    """The hole the conversion alone would have left open.

    (300, 200) is a point on both of these displays. Converting correctly once
    told which one still accepts this and clicks the main screen when the model
    meant the second -- F-22 reached by omission instead of by arithmetic.
    """

    screen = FakeScreen(focus=NOTES, screens=(MAIN_DISPLAY, SECOND_DISPLAY))
    gate = _gate(screen, NOTES)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.click(300, 200))

    assert "has 2 displays" in str(refused.value)
    assert screen.actions == []


def test_looking_at_a_machine_with_two_screens_needs_no_id() -> None:
    """Otherwise there would be no way to learn the ids in the first place.

    A screenshot has no coordinate to get wrong: it reports which display it
    took, and that report is where the id a later click sends back comes from.
    """

    screen = FakeScreen(focus=NOTES, screens=(MAIN_DISPLAY, SECOND_DISPLAY))
    gate = _gate(screen, NOTES)

    capture = asyncio.run(gate.screenshot())

    assert capture.display.display_id == MAIN_DISPLAY.display_id


def test_screenshotting_a_display_this_machine_lacks_is_refused() -> None:
    """Looking is laxer than landing about an *omitted* id, not about a wrong
    one. Handing back a picture of a different screen than the one asked for is
    the same silent substitution, one step earlier."""

    screen = FakeScreen(focus=NOTES, screens=(MAIN_DISPLAY, SECOND_DISPLAY))
    gate = _gate(screen, NOTES)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.screenshot(7))

    assert "no display 7" in str(refused.value)
    assert screen.actions == []


# --- bringing an application forward (ADR-091) -----------------------------


def test_a_task_can_move_between_the_applications_a_person_approved() -> None:
    """The capability that did not exist until ADR-091.

    Every other tool acts on whatever is frontmost, and nothing could change
    what that was -- so a task spanning two applications could not take its
    second step, whatever the person had approved.
    """

    screen = FakeScreen(focus=NOTES, installed=(NOTES, TERMINAL))
    gate = _gate(screen, NOTES, TERMINAL)

    held = asyncio.run(gate.activate(TERMINAL.bundle_id))

    assert held.application == TERMINAL
    assert ("activate", TERMINAL.bundle_id) in screen.actions
    # And the gate now answers about the window that is really in front.
    assert screen.frontmost() == TERMINAL


def test_the_tier_of_the_window_that_arrived_is_reported_not_the_one_asked_for() -> (
    None
):
    """A model that brought a terminal forward has to hear "click" now, not
    when its next keystroke is refused."""

    screen = FakeScreen(focus=NOTES, installed=(NOTES, TERMINAL))
    gate = _gate(screen, NOTES, TERMINAL)

    assert asyncio.run(gate.activate(TERMINAL.bundle_id)).tier == "click"


def test_activation_is_not_tier_gated_and_still_buys_nothing_extra() -> None:
    """A browser may be brought forward and still not be clicked.

    Activation synthesizes no input, so it is gated by the allowlist rather
    than by the tier -- the same place a screenshot is gated, for the same
    reason. What follows it is gated again, against what is frontmost then.
    """

    screen = FakeScreen(focus=NOTES, installed=(NOTES, CHROME))
    gate = _gate(screen, NOTES, CHROME)

    held = asyncio.run(gate.activate(CHROME.bundle_id))
    assert held.tier == "read"

    with pytest.raises(ScreenRefusedError):
        asyncio.run(gate.click(10, 10))
    assert not any(action == "click" for action, _ in screen.actions)


def test_an_unapproved_application_cannot_be_brought_forward() -> None:
    screen = FakeScreen(focus=NOTES, installed=(NOTES, MAIL))
    gate = _gate(screen, NOTES)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.activate(MAIL.bundle_id))

    assert "not in this session's approved list" in str(refused.value)
    # Refused before the port was asked, so the allowlist is not a filter on
    # something that already happened.
    assert screen.actions == []
    assert screen.frontmost() == NOTES


def test_acting_on_an_unapproved_window_does_not_say_which_one_it_is() -> None:
    """The third path where the no-naming rule lives, and where it did not hold.

    `activation_would_take_the_screen` says naming what is frontmost turns a
    refusal into a reading of what the person is doing -- "a strictly larger
    capability than the one being refused" -- and the test below this one pins
    it for the activation path. This path had the identical situation and
    answered ``"Mail" is not in this session's approved list``, so the
    capability that argument withholds could be had for free: attempt a click
    that is going to be refused anyway, and read the name out of the refusal.
    Cheaper than an activation, which at least needs an approved target.

    Closed by ADR-095, which also decides where the name *does* go: the
    console panel, whose reader is the person looking at that very window.

    The remedy is asserted alongside, because a refusal that withholds the name
    and also withholds what to do next would be a worse refusal rather than a
    narrower one.
    """

    screen = FakeScreen(focus=MAIL, installed=(NOTES, MAIL))
    gate = _gate(screen, NOTES)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.click(10, 10))

    message = str(refused.value)
    assert "not in this session's approved list" in message
    assert "Mail" not in message
    assert "com.apple.mail" not in message
    assert "request_access" in message
    assert screen.actions == []


def test_a_tier_refusal_still_names_the_application_the_person_approved() -> None:
    """The control, and the reason the change above is one branch and not two.

    Typing into a terminal is refused by the *tier*, not by the allowlist --
    that application is one this person put on the list by hand. Naming it
    discloses nothing they did not agree to, and not naming it would make
    "the terminal cannot be typed into" impossible to act on.
    """

    screen = FakeScreen(focus=TERMINAL, installed=(TERMINAL,))
    gate = _gate(screen, TERMINAL)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.type_text("date"))

    message = str(refused.value)
    assert "Terminal" in message
    assert 'tier "click"' in message


def test_the_screen_is_not_taken_from_a_window_nobody_approved() -> None:
    """The narrowing that makes the rest of this safe to grant (ADR-091 §2.2).

    Notes and Terminal are both approved, so the *target* check passes. What
    refuses is that a person has switched to something else -- and pulling the
    screen back from the window they are using is their decision, not a task's.
    """

    screen = FakeScreen(focus=MAIL, installed=(NOTES, TERMINAL, MAIL))
    gate = _gate(screen, NOTES, TERMINAL)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.activate(NOTES.bundle_id))

    message = str(refused.value)
    assert "frontmost right now is not in this session's approved list" in message
    # And it does not say *what* they switched to. This refusal fires exactly
    # when the frontmost application is unapproved, so naming it would make
    # every refusal a reading of what the person is doing.
    assert "Mail" not in message
    assert screen.actions == []


def test_an_approved_application_that_is_not_running_is_not_launched() -> None:
    """Activation reorders windows; it does not start processes.

    Approving a name is not approving a launch -- an application starting is
    whatever that application does on startup, on somebody's machine, and it
    is not what the dialog asked about (ADR-091 §4).
    """

    screen = FakeScreen(focus=NOTES, installed=(NOTES,))
    gate = _gate(screen, NOTES, TERMINAL)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.activate(TERMINAL.bundle_id))

    assert "is not running" in str(refused.value)
    assert "Ask the person to open it" in str(refused.value)


def test_an_activation_the_window_server_did_not_honour_is_reported() -> None:
    """Activation is a request, not a function call.

    `activation_lands=False` stands in for a modal sheet, a full-screen space
    or an application that declines. Nothing was denied and nothing happened,
    and the model has to hear the second half or its next action is refused
    for a reason it cannot see.
    """

    screen = FakeScreen(
        focus=NOTES, installed=(NOTES, TERMINAL), activation_lands=False
    )
    gate = _gate(screen, NOTES, TERMINAL)

    with pytest.raises(ScreenRefusedError) as refused:
        asyncio.run(gate.activate(TERMINAL.bundle_id))

    message = str(refused.value)
    assert "did not" in message and "Notes" in message
    assert "Nothing was clicked or typed" in message


# --- reading the approved list ---------------------------------------------


def test_the_approved_list_can_be_read_without_asking_anybody_again() -> None:
    """The only other way to learn this was to call `grant` a second time,
    which puts a second dialog in front of a person who already decided."""

    asked: list[object] = []

    async def counting(*args: object, **kwargs: object) -> bool:
        asked.append((args, kwargs))
        return True

    screen = FakeScreen(focus=NOTES)
    gate = _gate(screen, NOTES, TERMINAL, answers=counting)

    listed = gate.grants()

    assert [held.application.name for held in listed] == ["Notes", "Terminal"]
    assert [held.tier for held in listed] == ["full", "click"]
    assert len(asked) == 1


def test_reading_the_list_says_whether_one_of_them_is_in_front() -> None:
    """The frontmost check is what every other tool here fails on, so the
    answer to "may I act" is not the list alone."""

    screen = FakeScreen(focus=TERMINAL)
    gate = _gate(screen, NOTES, TERMINAL)

    front = gate.frontmost_grant()

    assert front is not None
    assert front.application == TERMINAL
    assert front.tier == "click"


def test_reading_the_list_never_names_a_window_nobody_approved() -> None:
    """`None` covers both "something unapproved is in front" and "nothing is",
    and the caller is deliberately unable to tell them apart."""

    granted = FakeScreen(focus=MAIL)
    assert _gate(granted, NOTES).frontmost_grant() is None

    nothing = FakeScreen(focus=ApplicationIdentity(bundle_id="", name=""))
    assert _gate(nothing, NOTES).frontmost_grant() is None


# --- the table and the methods behind it -----------------------------------

#: Every action name in `_ALLOWED`, and the gate call that produces it.
#:
#: Written out rather than derived, because deriving it from the gate would
#: make the test agree with whatever the gate does -- which is the thing being
#: checked.
_PERFORMED = {
    "left_click": lambda gate: gate.click(1, 1),
    "right_click": lambda gate: gate.click(1, 1, button="right"),
    "middle_click": lambda gate: gate.click(1, 1, button="middle"),
    "double_click": lambda gate: gate.click(1, 1, count=2),
    "triple_click": lambda gate: gate.click(1, 1, count=3),
    "scroll": lambda gate: gate.scroll(1, 1, direction="down", amount=3),
    "type": lambda gate: gate.type_text("x"),
    "key": lambda gate: gate.key("cmd+c"),
}


def test_every_permitted_action_is_one_the_gate_actually_performs() -> None:
    """The other half of `test_no_tier_permits_something_this_project_cannot_do`.

    The permission table is read as an inventory of what this project may do
    to a screen, and until 2026-08-28 it listed two actions -- `mouse_move`
    and `drag` -- that no gate method and no tool could reach. Nothing failed,
    because a permission for an action nobody performs refuses nothing. It was
    still an answer to "what does this do", and it was wrong (ADR-091 §2.4).

    Driven against a browser rather than asserted against a list: `refusal()`
    names the action it refused, so a gate path that stopped producing its own
    action name would fail here too.
    """

    from agent_workbench.domain.computer import _ALLOWED

    assert set().union(*_ALLOWED.values()) == set(_PERFORMED)

    for action, drive in _PERFORMED.items():
        screen = FakeScreen(focus=CHROME)
        gate = _gate(screen, CHROME)
        with pytest.raises(ScreenRefusedError) as refused:
            asyncio.run(drive(gate))
        assert f"so {action} is not available" in str(refused.value), action
        assert screen.actions == [], action
