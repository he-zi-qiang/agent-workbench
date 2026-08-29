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
    can_change_frontmost,
)
from agent_workbench.ports.screen import ScreenUnavailableError  # noqa: E402

# --- the two grants, which is the one thing `_screen()` below cannot see ----
#
# Every other test in this file bypasses the constructor, and that is exactly
# how a missing check survived from ADR-070 to 2026-08-29: nothing ever ran it.
# These two do run it, with the platform's answers faked, because the real ones
# are a property of whoever is running the suite.


def test_a_process_without_accessibility_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check ADR-070 §4 argued for and nobody wrote.

    Measured on this machine 2026-08-29 with Screen Recording granted and
    Accessibility not: the server started, and every click, keystroke and
    activation reported success while nothing on screen moved. macOS does not
    fail loudly here -- `CGEventPost` returns success having done nothing --
    so the only place this can be caught is at startup.
    """

    monkeypatch.setattr(Quartz, "CGPreflightScreenCaptureAccess", lambda: True)
    monkeypatch.setattr(Quartz, "CGPreflightPostEventAccess", lambda: False)
    monkeypatch.setattr(Quartz, "CGRequestPostEventAccess", lambda: False)

    with pytest.raises(ScreenUnavailableError) as refused:
        DarwinScreen()

    message = str(refused.value)
    assert "Accessibility" in message
    # Names which grant, because "screen permissions" sends somebody to the
    # wrong pane of System Settings -- these are two separate TCC grants and a
    # process can hold either without the other.
    assert "Screen Recording" not in message
    assert "report success and do nothing" in message


def test_both_grants_present_is_the_only_way_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Quartz, "CGPreflightScreenCaptureAccess", lambda: True)
    monkeypatch.setattr(Quartz, "CGPreflightPostEventAccess", lambda: True)

    assert isinstance(DarwinScreen(), DarwinScreen)


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


# --- what this process is allowed to do to the front of the screen ---------


def test_a_process_with_no_bundle_cannot_change_the_frontmost_application() -> None:
    """Asserted against *this* process, which is the point.

    pytest runs from an interpreter with no bundle identity, which is exactly
    the shape the server had until ADR-092 -- so this reads the real answer
    from the real platform rather than a faked one. Every way this can be
    wrong fails silently at the API (`activateWithOptions_` returns true and
    the screen does not change), so the check has to happen before the call.
    """

    reason = can_change_frontmost()

    assert reason is not None
    assert "not running as a bundled application" in reason
    # Names the way out, like every other refusal this project writes.
    assert "build_computer_app.sh" in reason


def test_activation_refuses_with_that_reason_instead_of_timing_out() -> None:
    """A two-second "it did not take" would be true and useless.

    It cannot distinguish a window that declined from a server nobody
    bundled, and only one of those is fixed by trying again.
    """

    with pytest.raises(ScreenUnavailableError) as refused:
        asyncio.run(_screen().activate("com.apple.finder"))

    assert "not running as a bundled application" in str(refused.value)
