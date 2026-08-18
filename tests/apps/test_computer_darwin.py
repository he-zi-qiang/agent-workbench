"""The thin macOS adapter, for the parts that can be asserted without a screen.

Most of this module cannot be tested here and is not: a test that asserts a
click landed needs a display, a TCC grant and something to click. What *can* be
tested is the part that rejects input before it ever reaches CGEvent -- and
that part matters, because a chord this adapter silently mis-parses sends the
wrong keystroke to a real application (ADR-070).

Skipped off macOS and without the `computer-use` extra, which is the honest
shape: CI installs neither.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="the macOS adapter needs macOS"
)

Quartz = pytest.importorskip("Quartz", reason="needs the computer-use extra")

from agent_workbench.adapters.screen.darwin import (  # noqa: E402
    _KEY_CODES,
    _MODIFIERS,
    DarwinScreen,
)


def _screen() -> DarwinScreen:
    # Bypasses only the permission pre-flight. Every method reached below
    # raises before it posts anything, which is what makes this safe to run on
    # a machine somebody is using.
    return object.__new__(DarwinScreen)


def test_the_real_machine_reports_its_displays_in_points() -> None:
    displays = _screen().displays()

    assert displays, "a Mac running tests has at least one display"
    main = displays[0]
    assert main.width > 0 and main.height > 0
    # Points, not pixels: a retina panel reports half what it has, and a click
    # computed in the other space lands at twice the intended place.
    assert main.scale_factor >= 1.0


def test_the_frontmost_application_is_read_fresh_and_identified_both_ways() -> None:
    identity = _screen().frontmost()

    # Whatever is in front while tests run, it has to answer both fields --
    # the tier table needs the bundle id and falls back to the name.
    assert isinstance(identity.bundle_id, str)
    assert isinstance(identity.name, str)


def test_an_unknown_modifier_is_refused_before_anything_is_posted() -> None:
    with pytest.raises(ValueError, match="not a modifier"):
        asyncio.run(_screen().key("hyper+c"))


def test_an_unknown_key_name_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="not a key this adapter knows"):
        asyncio.run(_screen().key("cmd+launchpad"))


def test_an_empty_chord_is_refused() -> None:
    with pytest.raises(ValueError, match="names no key"):
        asyncio.run(_screen().key("cmd+"))


def test_a_modified_letter_outside_the_us_layout_table_says_so() -> None:
    """Rather than sending a different letter.

    Unmodified text is typed as unicode and is layout-independent; a *chord*
    has to be a virtual key code, and the only table available is the US one.
    Saying so is the difference between a documented limit and a wrong key.
    """

    with pytest.raises(ValueError, match="US layout"):
        asyncio.run(_screen().key("cmd+é"))


def test_the_tables_agree_with_what_the_platform_defines() -> None:
    """Guards a class of typo that is invisible until something is pressed."""

    assert _MODIFIERS["cmd"] == Quartz.kCGEventFlagMaskCommand
    assert _MODIFIERS["shift"] == Quartz.kCGEventFlagMaskShift
    # Return and Escape are the two every flow uses; wrong codes here would be
    # wrong in a way that looks like the application ignoring the key.
    assert _KEY_CODES["return"] == 36
    assert _KEY_CODES["escape"] == 53
