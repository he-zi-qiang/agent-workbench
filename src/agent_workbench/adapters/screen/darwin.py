# pyright: reportMissingImports=false, reportMissingTypeStubs=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false
#
# pyobjc is behind the macOS-only `computer-use` extra, so on the machine CI
# runs on it is not installed at all and the two imports below do not
# resolve -- `reportMissingImports` is off for that, and the `try/except
# ImportError` under it is what turns the absence into a message naming the
# extra. Where it *is* installed it still ships no type information: every
# Quartz and AppKit symbol is an
# untyped bridge into a C API, so strict mode reports 186 unknowns in this file
# and none of them is a defect this project can act on. The suppressions are
# listed one rule at a time rather than switched to basic mode, so everything
# strict mode *can* still say here -- an undefined name, an unreachable branch,
# a wrong argument count against this module's own functions -- still fails the
# gate. This is the only file in the repository with any of them, which is the
# point of keeping the FFI in one thin module (ADR-070).
"""The macOS half: Quartz for the screen, CGEvent for the cursor and keyboard.

Everything here is one operating system's answer to a question asked in
``ports/screen.py``, and nothing here decides anything. The tier gate, the
budget and the focus check live above and are tested without a screen; this
module is the part that cannot be, so it is kept as thin as it can be made.

Three properties are worth stating because they are easy to lose:

**Points in, pixels only inside.** The port speaks the display's own point
space; this module converts once, at the edge. A retina panel reports 1470x956
points over 2940x1912 pixels, and a click computed in the wrong one of those
lands at twice or half the intended place -- silently, because a click that
misses is still a click.

**Permission is checked, not assumed.** macOS gates screen capture and event
synthesis behind separate TCC grants, and a process without them does not fail
loudly: `CGDisplayCreateImage` returns a picture of the desktop wallpaper with
every window missing, and `CGEventPost` returns success having done nothing.
Both are worse than an error, so both are pre-flighted.

**Nothing is cached.** ``frontmost`` in particular is read on every call. The
whole tier model rests on that being a fresh reading (ADR-070).
"""

from __future__ import annotations

import asyncio
from typing import Any, Final, cast

from agent_workbench.domain.computer import SCREENSHOT_QUALITY, ApplicationIdentity
from agent_workbench.ports.screen import (
    Capture,
    Display,
    MouseButton,
    ScreenUnavailableError,
    ScrollDirection,
)

try:  # pragma: no cover - the import is the feature test
    import AppKit
    import Quartz
except ImportError as error:  # pragma: no cover
    raise ScreenUnavailableError(
        "computer use needs the `computer-use` extra: uv sync --extra computer-use"
    ) from error

_JPEG: Final[str] = "image/jpeg"

#: CGEvent's own button numbers, and its own name for a scroll unit.
_BUTTON: Final[dict[MouseButton, tuple[int, int, int]]] = {
    # (down, up, button number)
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, 0),
    "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, 1),
    "middle": (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp, 2),
}

#: Which chord words are modifiers, and the flag each contributes.
_MODIFIERS: Final[dict[str, int]] = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "ctrl": Quartz.kCGEventFlagMaskControl,
    "control": Quartz.kCGEventFlagMaskControl,
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}

#: The named keys a chord may end in. Virtual key codes are a fixed table on
#: this platform; letters and digits are typed as text instead, because their
#: codes depend on the active keyboard layout and a hard-coded table would send
#: the wrong letter on a Dvorak or AZERTY machine.
_KEY_CODES: Final[dict[str, int]] = {
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "delete": 51,
    "backspace": 51,
    "escape": 53,
    "esc": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
    "forwarddelete": 117,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
}

#: How long a synthesized press is held, and how long between characters.
#:
#: Zero would be faster and is wrong: an application that reads modifier state
#: on key-down and the character on key-up can observe the two in the same
#: event-loop pass and drop one. 8 ms is below human perception and above the
#: pass.
_PRESS_SECONDS: Final[float] = 0.008


class DarwinScreen:
    """A :class:`ScreenPort` backed by Quartz."""

    def __init__(self) -> None:
        # Pre-flighted at construction, not at first use. A composition root
        # that assembles successfully and then fails on the first screenshot
        # has moved a startup problem into a turn.
        if not Quartz.CGPreflightScreenCaptureAccess():
            # Asks once; macOS shows its own dialog and remembers the answer.
            Quartz.CGRequestScreenCaptureAccess()
        if not Quartz.CGPreflightScreenCaptureAccess():
            raise ScreenUnavailableError(
                "this process has no Screen Recording permission. Grant it in "
                "System Settings > Privacy & Security > Screen Recording for "
                "the terminal or application running the server, then restart "
                "it -- macOS only re-reads the grant at launch."
            )

    # --- looking ---------------------------------------------------------

    def displays(self) -> tuple[Display, ...]:
        error, ids, _ = Quartz.CGGetActiveDisplayList(16, None, None)
        if error != 0:
            raise ScreenUnavailableError(f"CGGetActiveDisplayList failed with {error}")
        main = Quartz.CGMainDisplayID()
        found = [self._display(display_id) for display_id in ids]
        # Main first, which the port promises. A caller that takes `[0]` is
        # asking for "the screen the person is looking at".
        found.sort(key=lambda display: display.display_id != main)
        return tuple(found)

    def _display(self, display_id: int) -> Display:
        points_wide = Quartz.CGDisplayPixelsWide(display_id)
        points_high = Quartz.CGDisplayPixelsHigh(display_id)
        mode = Quartz.CGDisplayCopyDisplayMode(display_id)
        pixels_wide = Quartz.CGDisplayModeGetPixelWidth(mode)
        return Display(
            display_id=int(display_id),
            width=int(points_wide),
            height=int(points_high),
            # Derived rather than assumed to be 1 or 2: an external panel
            # running a scaled mode is neither.
            scale_factor=(pixels_wide / points_wide) if points_wide else 1.0,
        )

    def frontmost(self) -> ApplicationIdentity:
        running = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if running is None:
            # No focused application at all -- the login window, or a moment
            # between two. Treated as an unknown application, which the tier
            # table maps to "full"; the gate's allowlist is what refuses it,
            # because an empty bundle id is in nobody's allowlist.
            return ApplicationIdentity(bundle_id="", name="")
        return ApplicationIdentity(
            bundle_id=str(running.bundleIdentifier() or ""),
            name=str(running.localizedName() or ""),
        )

    def capabilities(self) -> frozenset[str]:
        # `exclude_mask`, not `exclude_native`, and the difference is a promise
        # rather than a detail. A compositor-level filter (ScreenCaptureKit's
        # SCContentFilter) means an unapproved window is never drawn into the
        # frame; what this does is draw the frame and then paint over the
        # window's rectangle. The pixels existed. A caller that needs the
        # stronger guarantee must read this and refuse.
        return frozenset({"exclude_mask"})

    async def capture(
        self,
        display_id: int,
        *,
        width: int,
        height: int,
        exclude_bundle_ids: tuple[str, ...] = (),
    ) -> Capture:
        display = self._display(display_id)
        masks = self._window_rects(exclude_bundle_ids) if exclude_bundle_ids else []
        # Quartz is synchronous and a full-screen grab is tens of milliseconds.
        # Off the event loop, because this server answers other calls while a
        # capture is in flight.
        content = await asyncio.to_thread(_render, display_id, width, height, masks)
        return Capture(
            media_type=_JPEG,
            content=content,
            width=width,
            height=height,
            display=display,
        )

    def _window_rects(
        self, bundle_ids: tuple[str, ...]
    ) -> list[tuple[float, float, float, float]]:
        """Where the excluded applications' windows are, in points."""

        wanted = {
            int(app.processIdentifier())
            for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
            if str(app.bundleIdentifier() or "") in bundle_ids
        }
        if not wanted:
            return []
        listing = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        rects: list[tuple[float, float, float, float]] = []
        for entry in listing or []:
            window = cast(dict[str, Any], entry)
            if int(window.get("kCGWindowOwnerPID", -1)) not in wanted:
                continue
            bounds = cast(dict[str, Any], window.get("kCGWindowBounds", {}))
            rects.append(
                (
                    float(bounds.get("X", 0)),
                    float(bounds.get("Y", 0)),
                    float(bounds.get("Width", 0)),
                    float(bounds.get("Height", 0)),
                )
            )
        return rects

    # --- acting ----------------------------------------------------------

    async def click(
        self, x: int, y: int, *, button: MouseButton = "left", count: int = 1
    ) -> None:
        down, up, number = _BUTTON[button]
        point = Quartz.CGPointMake(float(x), float(y))
        for press in range(1, count + 1):
            for kind in (down, up):
                event = Quartz.CGEventCreateMouseEvent(None, kind, point, number)
                # Without this a double click is two single clicks: the click
                # *state* is what makes the second one a double, not the timing.
                Quartz.CGEventSetIntegerValueField(
                    event, Quartz.kCGMouseEventClickState, press
                )
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            await asyncio.sleep(_PRESS_SECONDS)

    async def move(self, x: int, y: int) -> None:
        event = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, Quartz.CGPointMake(float(x), float(y)), 0
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    async def scroll(
        self, x: int, y: int, *, direction: ScrollDirection, amount: int
    ) -> None:
        await self.move(x, y)
        vertical = (
            amount if direction == "up" else -amount if direction == "down" else 0
        )
        horizontal = (
            amount if direction == "left" else -amount if direction == "right" else 0
        )
        event = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 2, vertical, horizontal
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    async def type_text(self, text: str) -> int:
        """Type by unicode payload rather than by key code.

        `CGEventKeyboardSetUnicodeString` puts the character on the event
        directly, which sidesteps the keyboard layout entirely -- there is no
        table mapping "é" or "中" to a virtual key, and on a non-US layout
        there is no table mapping "z" to one either.

        Returns the count delivered, which is the whole reason this is not
        `None`: the caller re-checks focus between characters and stops, and
        it has to be able to say how far it got (see `domain.computer.
        focus_lost`).
        """

        delivered = 0
        for character in text:
            for pressed in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(None, 0, pressed)
                Quartz.CGEventKeyboardSetUnicodeString(event, 1, character)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            delivered += 1
            await asyncio.sleep(_PRESS_SECONDS)
        return delivered

    async def key(self, combination: str) -> None:
        parts = [part.strip().casefold() for part in combination.split("+")]
        if not parts or not parts[-1]:
            raise ValueError(f"{combination!r} names no key")
        flags = 0
        for modifier in parts[:-1]:
            flag = _MODIFIERS.get(modifier)
            if flag is None:
                raise ValueError(f"{modifier!r} is not a modifier")
            flags |= flag
        name = parts[-1]
        code = _KEY_CODES.get(name)
        if code is None:
            if len(name) != 1:
                raise ValueError(f"{name!r} is not a key this adapter knows")
            # A bare letter with no modifiers is text, and typing it is
            # layout-independent. With modifiers it has to be a key code, and
            # the US layout is the only table available -- documented rather
            # than silently wrong.
            if not flags:
                await self.type_text(name)
                return
            code = _US_LETTER_CODES.get(name)
            if code is None:
                raise ValueError(
                    f"{name!r} has no virtual key code; chords are resolved "
                    "against the US layout"
                )
        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, code, pressed)
            Quartz.CGEventSetFlags(event, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        await asyncio.sleep(_PRESS_SECONDS)


#: US-layout virtual key codes, used only for modified chords (cmd+c and its
#: relatives). See `key`: unmodified text never comes through here.
_US_LETTER_CODES: Final[dict[str, int]] = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "9": 25,
    "7": 26,
    "8": 28,
    "0": 29,
    "o": 31,
    "u": 32,
    "i": 34,
    "p": 35,
    "l": 37,
    "j": 38,
    "k": 40,
    "n": 45,
    "m": 46,
}


def _render(
    display_id: int,
    width: int,
    height: int,
    masks: list[tuple[float, float, float, float]],
) -> bytes:
    """Grab, scale, mask and encode. Runs on a worker thread."""

    image = Quartz.CGDisplayCreateImage(display_id)
    if image is None:
        raise ScreenUnavailableError(
            f"CGDisplayCreateImage returned nothing for display {display_id}"
        )
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0, space, Quartz.kCGImageAlphaNoneSkipLast
    )
    if context is None:
        raise ScreenUnavailableError("could not allocate a bitmap for the capture")
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    if masks:
        source_width = float(Quartz.CGImageGetWidth(image))
        source_height = float(Quartz.CGImageGetHeight(image))
        # The window list is in points with a top-left origin; the bitmap is in
        # its own scaled pixels with a bottom-left one. Both conversions, in
        # one place.
        ratio_x = width / source_width if source_width else 1.0
        ratio_y = height / source_height if source_height else 1.0
        Quartz.CGContextSetRGBFillColor(context, 0.0, 0.0, 0.0, 1.0)
        for x, y, box_width, box_height in masks:
            Quartz.CGContextFillRect(
                context,
                Quartz.CGRectMake(
                    x * ratio_x,
                    height - (y + box_height) * ratio_y,
                    box_width * ratio_x,
                    box_height * ratio_y,
                ),
            )
    scaled = Quartz.CGBitmapContextCreateImage(context)
    data = Quartz.CFDataCreateMutable(None, 0)
    destination = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    if destination is None:
        raise ScreenUnavailableError("could not create a JPEG encoder")
    Quartz.CGImageDestinationAddImage(
        destination,
        scaled,
        {Quartz.kCGImageDestinationLossyCompressionQuality: SCREENSHOT_QUALITY},
    )
    if not Quartz.CGImageDestinationFinalize(destination):
        raise ScreenUnavailableError("the capture could not be encoded")
    return bytes(data)


__all__ = ["DarwinScreen"]
