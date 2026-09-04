"""Tiers, budgets and refusal text -- all of it without a screen.

That this file needs no display is the design working: every rule that decides
what a model may do to a machine is arithmetic or a table lookup, and the part
that cannot be tested this way is one thin adapter (ADR-070).
"""

from __future__ import annotations

import pytest

from agent_workbench.domain.computer import (
    ApplicationIdentity,
    DisplayFrame,
    ScreenshotBudget,
    activation_did_not_take,
    activation_needs_a_grant,
    activation_would_take_the_screen,
    application_is_not_running,
    focus_lost,
    kind_of,
    off_frame,
    permits,
    refusal,
    tier_for,
)

#: The main display: its own space and the global one are the same space.
MAIN = DisplayFrame(display_id=1, origin_x=0, origin_y=0, width=1470, height=956)

#: A second display, right of the main one and 124 points higher. Both offsets
#: are non-zero, unequal, and one is negative -- so a conversion that drops an
#: origin, swaps the axes or gets a sign wrong fails here rather than passing by
#: symmetry.
SECOND = DisplayFrame(
    display_id=2, origin_x=1470, origin_y=-124, width=1920, height=1080
)


def app(bundle_id: str = "", name: str = "") -> ApplicationIdentity:
    return ApplicationIdentity(bundle_id=bundle_id, name=name)


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (app("com.google.Chrome", "Google Chrome"), "read"),
        (app("com.apple.Safari", "Safari"), "read"),
        (app("com.coinbase.Coinbase", "Coinbase"), "read"),
        (app("com.apple.Terminal", "Terminal"), "click"),
        (app("com.microsoft.VSCode", "Code"), "click"),
        (app("com.apple.dt.Xcode", "Xcode"), "click"),
        (app("com.apple.Notes", "Notes"), "full"),
        (app("com.apple.finder", "Finder"), "full"),
    ],
)
def test_the_tier_is_decided_by_what_the_application_is(
    identity: ApplicationIdentity, expected: str
) -> None:
    assert tier_for(identity) == expected


def test_an_unknown_browser_is_still_a_browser() -> None:
    """The bundle-id table cannot be complete, and completeness is the point.

    A browser released last week has an id nobody has written down. Falling
    through to "full" would make the browsers this project has never heard of
    the *only* ones it types passwords into.
    """

    assert tier_for(app("io.example.NewBrowser", "Example Browser")) == "read"
    assert tier_for(app("com.unknown.thing", "Fancy Wallet")) == "read"


def test_a_longer_name_match_wins_over_a_shorter_one() -> None:
    """Substrings are tried longest-first, or "chrome" decides everything.

    "Chrome Remote Desktop" contains "chrome" and is not a browser; a table
    scanned in declaration order would call it one and lock it to tier read.
    """

    assert kind_of(app("", "Interactive Brokers")) == "trading"
    # And the guard the ordering provides is visible: the longer, more specific
    # phrase is the one that matched.
    assert kind_of(app("", "Visual Studio Code")) == "shell"


def test_the_bundle_id_beats_the_name() -> None:
    """A name is forgeable; a bundle id is what the OS knows it by."""

    assert tier_for(app("com.apple.Terminal", "Notes")) == "click"


# --- what each tier permits ------------------------------------------------


def test_read_permits_nothing_but_looking() -> None:
    for action in ("left_click", "right_click", "type", "key", "scroll"):
        assert not permits("read", action)


def test_click_permits_a_click_and_refuses_a_keystroke() -> None:
    """The whole point of the middle tier.

    A Run button may be pressed and output may be scrolled; nothing may be
    typed, because a keystroke in a terminal runs a command and this project
    has a sandbox tool that runs commands with a gate and an audit trail.
    """

    assert permits("click", "left_click")
    assert permits("click", "scroll")
    assert not permits("click", "type")
    assert not permits("click", "key")
    assert not permits("click", "right_click")


def test_full_permits_the_rest() -> None:
    for action in ("left_click", "right_click", "middle_click", "type", "key"):
        assert permits("full", action)


def test_no_tier_permits_something_this_project_cannot_do() -> None:
    """The table is read as an inventory of what this project does to a screen.

    `mouse_move` and `drag` sat in it until 2026-08-28 with nothing behind
    them: no gate method, no tool, and for `drag` no port method either. That
    broke nothing -- a permission for an action nobody performs refuses
    nothing -- which is why it survived two ADRs. It was still a claim, and it
    still priced the next drag tool at zero (ADR-091 §2.4).

    `tests/apps/test_computer_gate.py` holds the other half of this: that every
    name still in the table is one the gate really produces.
    """

    for action in ("mouse_move", "drag"):
        for tier in ("read", "click", "full"):
            assert not permits(tier, action)


# --- the screenshot budget -------------------------------------------------


def test_a_small_screen_is_sent_as_it_is() -> None:
    budget = ScreenshotBudget()
    assert budget.fit(800, 600) == (800, 600)


def test_the_token_ceiling_binds_before_the_edge_ceiling() -> None:
    """A 1568x1568 image is inside the edge ceiling and twice the token one.

    This is why there are two ceilings rather than one: an implementation that
    only clamped the long edge would send images costing 3136 tokens and be
    correct by its own rule every time.
    """

    budget = ScreenshotBudget()
    assert max(1568, 1568) <= budget.max_edge_px
    assert budget.tokens_for(1568, 1568) > budget.max_tokens

    width, height = budget.fit(1568, 1568)
    assert budget.tokens_for(width, height) <= budget.max_tokens
    assert max(width, height) <= budget.max_edge_px


def test_a_retina_display_is_fitted_and_keeps_its_shape() -> None:
    budget = ScreenshotBudget()
    width, height = budget.fit(2940, 1912)

    assert budget.fits(width, height)
    # Aspect ratio preserved to within a pixel of rounding. A squashed capture
    # would make every coordinate the model derives from it wrong, in a way
    # nothing downstream could detect.
    assert abs((width / height) - (2940 / 1912)) < 0.01


def test_the_fit_is_the_largest_one_that_fits() -> None:
    """Not merely *a* fit. A conservative implementation passes "it fits"
    while throwing away detail the budget would have paid for."""

    budget = ScreenshotBudget()
    width, height = budget.fit(2940, 1912)

    bigger = (int(width * 1.02) + 1, int(height * 1.02) + 1)
    assert not budget.fits(*bigger)


def test_a_screen_with_no_area_is_refused_rather_than_divided_by() -> None:
    with pytest.raises(ValueError):
        ScreenshotBudget().fit(0, 1080)


# --- what a refusal says ---------------------------------------------------


def test_a_refusal_names_the_tier_the_remedy_and_the_prohibition() -> None:
    """Three parts, and the third is the one that does the work.

    A model that is only refused tries the next thing it can think of, and for
    a terminal the next thing is AppleScript -- which this machine has, and
    which would work.
    """

    message = refusal(
        action="type",
        application=app("com.apple.Terminal", "Terminal"),
        tier="click",
    )

    assert '"click"' in message
    assert "sandbox tool" in message
    assert "Do not attempt to work around this restriction" in message
    assert "AppleScript" in message


def test_a_browser_refusal_points_at_the_browser_tools_not_the_shell() -> None:
    message = refusal(
        action="left_click", application=app("com.google.Chrome", "Chrome"), tier="read"
    )

    assert "automation tools" in message
    assert "sandbox tool" not in message


def test_a_money_refusal_asks_the_person_to_do_it() -> None:
    message = refusal(
        action="left_click",
        application=app("com.coinbase.Coinbase", "Coinbase"),
        tier="read",
    )

    assert "Ask the person" in message


def test_losing_focus_mid_string_reports_how_much_landed() -> None:
    """Not "denied". A model told only that retypes the whole string, and the
    half that already landed arrives twice."""

    message = focus_lost(
        approved=app("com.apple.Notes", "Notes"),
        now_frontmost=app("com.apple.Terminal", "Terminal"),
        delivered=7,
        total=20,
    )

    assert "7 of 20" in message
    assert "NOT delivered" in message
    assert "Terminal" in message and "Notes" in message
    assert "screenshot" in message


def test_on_the_main_display_the_conversion_is_the_identity() -> None:
    """Which is the whole reason F-22 survived a year.

    This machine has one screen. On it the display's own space and the space
    events are posted into are the same space, so the bug was unobservable and
    the code that had it looked right.
    """

    assert MAIN.to_global(300, 200) == (300, 200)


def test_a_point_on_a_second_display_is_moved_by_that_display_s_origin() -> None:
    """The regression. Before ADR-090 this coordinate was posted unchanged,
    which named a point on the *main* screen -- and the click succeeded."""

    assert SECOND.to_global(300, 200) == (1770, 76)


def test_the_axes_are_not_swapped_and_the_sign_is_not_dropped() -> None:
    assert SECOND.to_global(0, 0) == (1470, -124)
    assert SECOND.to_global(1919, 1079) == (3389, 955)


def test_a_display_holds_its_own_points_and_not_the_next_one_s() -> None:
    """Half-open at the far edge: a 1920-wide display's last column is 1919,
    and 1920 is already the first column of whatever is arranged beyond it."""

    assert SECOND.contains(0, 0) is True
    assert SECOND.contains(1919, 1079) is True
    assert SECOND.contains(1920, 0) is False
    assert SECOND.contains(0, 1080) is False


def test_a_negative_coordinate_is_not_on_any_display() -> None:
    """Even on a display whose own origin is negative. The two are different
    spaces, and `contains` is asked in the display's own one."""

    assert SECOND.contains(-1, 0) is False
    assert SECOND.contains(0, -1) is False


def test_an_off_display_coordinate_says_which_display_and_how_big_it_is() -> None:
    message = off_frame(x=2000, y=40, frame=MAIN)

    assert "(2000, 40)" in message
    assert "display 1" in message
    assert "1470x956" in message
    assert "display_id" in message


def test_a_coordinate_mistake_is_not_answered_as_a_security_event() -> None:
    """The one part of `refusal` that is deliberately absent here.

    "Never use AppleScript" belongs on a *permission* refusal, because that is
    what invites trying another route to the same window. Being a hundred
    points off is not that, and answering it as though it were teaches a model
    that these warnings are noise -- which is how the ones that matter stop
    being read.
    """

    message = off_frame(x=2000, y=40, frame=MAIN)

    assert "AppleScript" not in message
    assert "work around" not in message


# --- what an activation refusal says ---------------------------------------


def test_activating_something_unapproved_says_nothing_about_the_machine() -> None:
    """Only the allowlist was consulted, so only the allowlist is answered.

    A message that told "not approved" apart from "not installed" would turn
    this refusal into a way to ask which applications exist on somebody's
    machine -- a strictly larger capability than the one being refused.
    """

    message = activation_needs_a_grant(bundle_id="com.apple.Mail")

    assert "com.apple.Mail" in message
    assert "request_access" in message
    assert "never use AppleScript" in message
    for leak in ("running", "installed", "not found"):
        assert leak not in message


def test_a_refusal_for_somebody_elses_window_does_not_name_that_window() -> None:
    """The narrowing this refusal serves is about a person, not an attacker.

    It fires exactly when what is frontmost is something nobody approved --
    so a message that named it would make every refused activation a reading
    of what the person is doing, which is what the allowlist exists to stop.
    """

    message = activation_would_take_the_screen(target=app("com.apple.Notes", "Notes"))

    assert "Notes" in message
    assert "Mail" not in message
    # And it does not read as a temporary condition to spin on.
    assert "Do not poll" in message


def test_an_approved_application_that_is_not_running_is_not_offered_a_launch() -> None:
    """The remedy is a person, and saying so is the whole message.

    A refusal that only said "not running" would send a model looking for
    something that starts applications, and this machine has several.
    """

    message = application_is_not_running(target=app("com.apple.Notes", "Notes"))

    assert "Notes" in message
    assert "not running" in message
    assert "Ask the person to open it" in message


def test_an_activation_that_did_not_take_says_what_is_in_front_instead() -> None:
    """Not a policy refusal -- nothing was denied -- but a failure the model
    has to hear, or its next action is refused for a reason it cannot see."""

    message = activation_did_not_take(
        target=app("com.apple.Notes", "Notes"),
        now_frontmost=app("com.apple.Terminal", "Terminal"),
    )

    assert "Notes" in message and "Terminal" in message
    assert "Nothing was clicked or typed" in message
    assert "screenshot" in message


# --- Windows identities (ADR-0108) ---------------------------------------------


def _exe(name: str, shown: str = "") -> ApplicationIdentity:
    return ApplicationIdentity(bundle_id=name, name=shown)


@pytest.mark.parametrize(
    ("executable", "expected"),
    [
        ("chrome.exe", "read"),
        ("msedge.exe", "read"),
        ("firefox.exe", "read"),
        ("electrum.exe", "read"),
        ("windowsterminal.exe", "click"),
        ("cmd.exe", "click"),
        ("powershell.exe", "click"),
        ("pwsh.exe", "click"),
        ("code.exe", "click"),
        ("devenv.exe", "click"),
        ("idea64.exe", "click"),
        ("notepad.exe", "full"),
        ("winword.exe", "full"),
    ],
)
def test_a_windows_executable_is_classified_by_its_file_name(
    executable: str, expected: str
) -> None:
    assert tier_for(_exe(executable)) == expected


def test_a_windows_executable_is_matched_regardless_of_case() -> None:
    """The adapter lower-cases; a caller that did not still gets the same tier."""

    assert tier_for(_exe("Chrome.EXE", "Google Chrome")) == "read"
    assert tier_for(_exe("WindowsTerminal.exe")) == "click"


def test_an_unknown_windows_browser_is_still_a_browser_by_name() -> None:
    """The second, weaker signal works for a `.exe` nobody listed, as it does
    for a bundle id nobody listed."""

    assert tier_for(_exe("newbrowser.exe", "New Browser")) == "read"
    assert tier_for(_exe("host.exe", "Windows PowerShell")) == "click"
    assert tier_for(_exe("host.exe", "Command Prompt")) == "click"


def test_the_two_exact_tables_never_share_a_key() -> None:
    """A bundle id and an executable name are different identities; one string
    that could be both would be classified by whichever table is read first."""

    from agent_workbench.domain import computer as module

    assert not set(module._KIND_BY_BUNDLE_ID) & set(module._KIND_BY_EXECUTABLE)
    assert all(key == key.casefold() for key in module._KIND_BY_EXECUTABLE)
    assert all(key.endswith(".exe") for key in module._KIND_BY_EXECUTABLE)


def test_a_refusal_names_the_windows_routes_it_forbids_too() -> None:
    """The third part of a refusal lists what the machine actually has, and a
    Windows machine has PowerShell and SendKeys where a Mac has AppleScript."""

    message = refusal(action="type", application=_exe("cmd.exe", "cmd"), tier="click")

    assert "never use AppleScript" in message
    assert "PowerShell" in message and "SendKeys" in message
