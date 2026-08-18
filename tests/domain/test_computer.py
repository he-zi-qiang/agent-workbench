"""Tiers, budgets and refusal text -- all of it without a screen.

That this file needs no display is the design working: every rule that decides
what a model may do to a machine is arithmetic or a table lookup, and the part
that cannot be tested this way is one thin adapter (ADR-070).
"""

from __future__ import annotations

import pytest

from agent_workbench.domain.computer import (
    ApplicationIdentity,
    ScreenshotBudget,
    focus_lost,
    kind_of,
    permits,
    refusal,
    tier_for,
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
    for action in ("left_click", "type", "key", "scroll", "drag"):
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
    for action in ("left_click", "right_click", "type", "key", "drag"):
        assert permits("full", action)


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
