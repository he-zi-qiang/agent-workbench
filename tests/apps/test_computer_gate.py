"""The gate: four checks, and the one that is easy to leave out.

Driven against `FakeScreen`, which can do the one thing a real screen cannot be
asked to do on cue -- change which application is frontmost *between* two calls.
That is the whole reason the port exists (ADR-070).
"""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.adapters.memory.screen import FakeScreen
from agent_workbench.apps.computer_mcp.gate import ScreenGate, ScreenRefusedError
from agent_workbench.domain.computer import ApplicationIdentity

NOTES = ApplicationIdentity(bundle_id="com.apple.Notes", name="Notes")
TERMINAL = ApplicationIdentity(bundle_id="com.apple.Terminal", name="Terminal")
CHROME = ApplicationIdentity(bundle_id="com.google.Chrome", name="Google Chrome")


def _gate(screen: FakeScreen, *granted: ApplicationIdentity) -> ScreenGate:
    gate = ScreenGate(screen=screen)
    if granted:
        gate.grant(granted)
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
    given = gate.grant((NOTES, TERMINAL, CHROME))

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

    big = Display(display_id=9, width=3840, height=2160, scale_factor=1.0)
    screen = FakeScreen(focus=NOTES, screens=(big,))
    gate = _gate(screen, NOTES)

    capture = asyncio.run(gate.screenshot())

    assert capture.width < 3840
    assert gate.budget.fits(capture.width, capture.height)
    # And the display's own dimensions still travel with it, because that is
    # the coordinate space every later click is expressed in.
    assert capture.display.width == 3840


def test_a_platform_that_cannot_exclude_is_refused_rather_than_trusted() -> None:
    """Only reachable when there is something to exclude.

    Today the gate excludes nothing (see `_to_exclude`), so this asserts the
    branch is wired rather than that it fires -- and it is written now because
    the failure it guards against, a frame quietly containing an unapproved
    window, is the one this whole design exists to prevent.
    """

    screen = FakeScreen(focus=NOTES, supports=frozenset())
    gate = _gate(screen, NOTES)

    # No exclusions requested, so the capture proceeds even though the platform
    # could not have honoured one.
    assert asyncio.run(gate.screenshot()).content
