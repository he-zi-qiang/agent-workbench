"""The Windows adapter, for the parts that can be asserted without Windows.

Most of `adapters/screen/win32.py` cannot be tested here and is not: a test
that asserts a click landed needs a desktop, a window and something to click,
and this repository's tests run on POSIX. What *can* be tested is the pure
half the module was split to expose -- the chord parser, which decides which
keystroke reaches a real application, and the frame composer, which is the
one security-relevant property of the Windows capture: that the canvas holds
nothing but the windows it was handed (ADR-0108).

Nothing here touches `user32`. `Win32Screen` itself is asserted to refuse
construction off Windows rather than to pretend.
"""

from __future__ import annotations

import io
import sys

import pytest

from agent_workbench.adapters.screen import win32
from agent_workbench.adapters.screen.win32 import (
    Win32Screen,
    WindowSlice,
    compose_frame,
    parse_chord,
)
from agent_workbench.domain.computer import ScreenshotBudget
from agent_workbench.ports.screen import ScreenUnavailableError

Image = pytest.importorskip("PIL.Image", reason="the composer needs Pillow")

# --- chords ------------------------------------------------------------------


def test_a_named_key_resolves_to_its_virtual_key_code() -> None:
    assert parse_chord("Return") == ((), 0x0D)
    assert parse_chord("ctrl+shift+Tab") == ((0x11, 0x10), 0x09)


def test_a_letter_is_left_for_the_layout_to_resolve() -> None:
    """Unlike the macOS half, no US-only table: `VkKeyScanW` answers at press
    time against whatever layout is active, so the parser hands the character
    through rather than guessing a code for it."""

    assert parse_chord("ctrl+c") == ((0x11,), "c")
    assert parse_chord("z") == ((), "z")


def test_cmd_is_the_windows_key_and_not_control() -> None:
    """A model told 'cmd+c' as the example chord could mean either; the word
    stays 'the platform's command key' rather than silently becoming Control
    and turning `cmd+l` into a browser's address bar."""

    modifiers, _ = parse_chord("cmd+c")
    assert modifiers == (0x5B,)
    assert parse_chord("win+d") == parse_chord("super+d") == ((0x5B,), "d")


@pytest.mark.parametrize("chord", ["", "ctrl+", "hyper+c", "ctrl+bogus", "+"])
def test_a_chord_this_adapter_does_not_understand_is_refused(chord: str) -> None:
    with pytest.raises(ValueError):
        parse_chord(chord)


def test_the_named_key_table_agrees_with_what_windows_defines() -> None:
    """A few anchors from the platform's own header, so a typo in the table is
    a red test rather than a wrong keystroke in somebody's editor."""

    assert win32._KEY_CODES["escape"] == 0x1B
    assert win32._KEY_CODES["backspace"] == 0x08
    assert win32._KEY_CODES["forwarddelete"] == 0x2E
    assert win32._KEY_CODES["f12"] == 0x7B
    assert win32._MODIFIERS["alt"] == 0x12


# --- the frame ---------------------------------------------------------------


def _solid(width: int, height: int, colour: tuple[int, int, int]) -> object:
    return Image.new("RGB", (width, height), colour)


def _decoded(frame: bytes) -> object:
    return Image.open(io.BytesIO(frame)).convert("RGB")


def _pixel(frame: object, at: tuple[int, int]) -> tuple[int, int, int]:
    return frame.getpixel(at)  # pyright: ignore[reportAttributeAccessIssue]


def _near(pixel: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    """JPEG is lossy, and a solid block comes back within a few levels of
    itself; what these tests ask is which window a pixel belongs to."""

    return all(abs(a - b) <= 16 for a, b in zip(pixel, expected, strict=True))


def test_the_canvas_holds_only_what_it_was_handed() -> None:
    """The whole capture promise on this platform, as an assertion: an
    unapproved window is never a slice, so it is never a pixel."""

    approved = WindowSlice(left=10, top=10, image=_solid(20, 20, (255, 0, 0)))
    frame = _decoded(compose_frame(60, 40, (approved,), width=60, height=40))

    assert _near(_pixel(frame, (15, 15)), (255, 0, 0))
    # Outside the approved window: the blank canvas, not a desktop.
    assert _near(_pixel(frame, (50, 30)), win32._CANVAS_GREY)
    assert _near(_pixel(frame, (0, 0)), win32._CANVAS_GREY)


def test_windows_are_pasted_bottom_most_first_so_the_top_one_wins() -> None:
    below = WindowSlice(left=0, top=0, image=_solid(30, 30, (0, 0, 255)))
    above = WindowSlice(left=10, top=10, image=_solid(30, 30, (0, 255, 0)))
    frame = _decoded(compose_frame(50, 50, (below, above), width=50, height=50))

    assert _near(_pixel(frame, (5, 5)), (0, 0, 255))
    assert _near(_pixel(frame, (20, 20)), (0, 255, 0))


def test_a_window_half_off_the_display_is_clipped_not_refused() -> None:
    hanging = WindowSlice(left=-10, top=-10, image=_solid(20, 20, (255, 255, 255)))
    frame = _decoded(compose_frame(40, 40, (hanging,), width=40, height=40))

    assert _near(_pixel(frame, (5, 5)), (255, 255, 255))
    assert _near(_pixel(frame, (15, 15)), win32._CANVAS_GREY)


def test_an_empty_allowlist_is_a_picture_of_nothing() -> None:
    frame = _decoded(compose_frame(30, 20, (), width=30, height=20))

    assert frame.size == (30, 20)  # pyright: ignore[reportAttributeAccessIssue]
    assert _near(_pixel(frame, (15, 10)), win32._CANVAS_GREY)


def test_the_frame_is_encoded_at_the_budgeted_size_and_keeps_its_shape() -> None:
    """The budget's answer goes straight in, as it does on macOS: one resize,
    to exactly the size the gate computed, aspect ratio kept."""

    width, height = ScreenshotBudget().fit(2560, 1440)
    frame = _decoded(compose_frame(2560, 1440, (), width=width, height=height))

    assert frame.size == (width, height)  # pyright: ignore[reportAttributeAccessIssue]
    assert abs(width / height - 2560 / 1440) < 0.01


# --- the platform half -------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32", reason="this asserts the refusal off Windows"
)
def test_the_adapter_refuses_to_exist_off_windows() -> None:
    with pytest.raises(ScreenUnavailableError, match=sys.platform):
        Win32Screen()


def test_the_module_imports_everywhere_so_the_pure_half_is_tested_somewhere() -> None:
    """The reason the structures are declared by hand rather than taken from
    `ctypes.wintypes`, and the reason `_Native` is resolved lazily."""

    assert win32.is_supported_platform() == (sys.platform == "win32")
