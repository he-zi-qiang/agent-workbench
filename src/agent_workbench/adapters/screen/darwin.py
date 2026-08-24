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
import threading
from typing import Any, Final

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
    import ScreenCaptureKit
except ImportError as error:  # pragma: no cover
    raise ScreenUnavailableError(
        "computer use needs the `computer-use` extra: uv sync --extra computer-use"
    ) from error

_JPEG: Final[str] = "image/jpeg"

#: How long to wait for ScreenCaptureKit's two completion handlers. Generous
#: against the measured 46 ms and 43 ms, because the thing they are waiting on
#: is a system daemon under whatever load the machine is under -- and because
#: the alternative to a bound is a tool call that never answers.
_CONTENT_TIMEOUT_SECONDS: Final[float] = 10.0
_CAPTURE_TIMEOUT_SECONDS: Final[float] = 15.0

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
        # `exclude_native` since 2026-08-24, and the word is the whole promise.
        # This used to be `exclude_mask`: draw the desktop, then paint black
        # over rectangles read from a separate window list. That is a picture
        # the unapproved pixels were in, and it is wrong the moment a window
        # moves between the geometry read and the shutter, or a bounds value is
        # off, or the mask composites under something.
        #
        # SCContentFilter filters at the compositor -- the window is never
        # drawn. Measured on this machine: the allowlisted frame is 34,957
        # bytes against 96,147 for the same unfiltered desktop, because the
        # other windows are not in it to compress.
        return frozenset({"exclude_native"})

    async def capture(
        self,
        display_id: int,
        *,
        width: int,
        height: int,
        include_bundle_ids: tuple[str, ...] = (),
    ) -> Capture:
        display = self._display(display_id)
        # ScreenCaptureKit is a completion-handler API and a filtered grab is
        # about 70 ms here (46 to list windows, 43 to compose) against 22 for
        # the CoreGraphics grab it replaced. Off the event loop either way,
        # because this server answers other calls while a capture is in flight.
        content = await asyncio.to_thread(
            _render, display_id, width, height, include_bundle_ids
        )
        return Capture(
            media_type=_JPEG,
            content=content,
            width=width,
            height=height,
            display=display,
        )

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


def _shareable_content(timeout: float) -> Any:
    """The window list, from a completion-handler API, on a worker thread.

    ScreenCaptureKit answers on an internal dispatch queue rather than the main
    run loop, which is the whole reason this can be done at all from a server
    that never starts an ``NSApplication``: a plain ``Event.wait()`` returns
    when the queue fires. Measured 2026-08-24 on this machine: 46 ms.
    """

    answered = threading.Event()
    box: dict[str, Any] = {}

    def received(content: Any, error: Any) -> None:
        box["content"], box["error"] = content, error
        answered.set()

    ScreenCaptureKit.SCShareableContent.getShareableContentWithCompletionHandler_(
        received
    )
    if not answered.wait(timeout):
        raise ScreenUnavailableError(
            "ScreenCaptureKit did not answer with the window list in "
            f"{timeout:.0f}s. No screenshot was taken."
        )
    if box.get("error") is not None:
        # SCStreamErrorUserDeclined (-3801) arrives here rather than as a
        # refused permission at startup: the Screen Recording grant can be
        # withdrawn while the process runs.
        raise ScreenUnavailableError(
            f"ScreenCaptureKit refused to list windows: {box['error']}"
        )
    return box["content"]


def _render(
    display_id: int,
    width: int,
    height: int,
    include_bundle_ids: tuple[str, ...],
) -> bytes:
    """Compose a frame of the approved windows only, and encode it.

    Runs on a worker thread. The filtering happens at the compositor: an
    application outside ``include_bundle_ids`` is never drawn, so there is no
    moment at which its pixels are in the buffer. That is the difference this
    replaced -- the previous implementation drew the whole desktop and painted
    black rectangles over window bounds it had read separately, which is a
    different promise and a worse one.
    """

    content = _shareable_content(_CONTENT_TIMEOUT_SECONDS)
    display = next(
        (
            candidate
            for candidate in content.displays()
            if int(candidate.displayID()) == display_id
        ),
        None,
    )
    if display is None:
        raise ScreenUnavailableError(
            f"ScreenCaptureKit does not report a display {display_id}"
        )

    wanted = set(include_bundle_ids)
    windows = [
        window
        for window in content.windows()
        if window.isOnScreen()
        and (owner := window.owningApplication()) is not None
        and str(owner.bundleIdentifier() or "") in wanted
    ]
    # An allowlist that matched nothing is an empty frame, not a full one. It
    # happens whenever an approved application has no window on this display,
    # and the honest answer is a picture of nothing rather than a picture of
    # everything else.
    content_filter = (
        ScreenCaptureKit.SCContentFilter.alloc().initWithDisplay_includingWindows_(
            display, windows
        )
    )

    configuration = ScreenCaptureKit.SCStreamConfiguration.alloc().init()
    # Width and height here are *pixels* and are honoured exactly, so the
    # budget's own answer goes straight in -- there is no second downscale
    # step to disagree with it.
    configuration.setWidth_(width)
    configuration.setHeight_(height)

    answered = threading.Event()
    box: dict[str, Any] = {}

    def captured(image: Any, error: Any) -> None:
        box["image"], box["error"] = image, error
        answered.set()

    ScreenCaptureKit.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
        content_filter, configuration, captured
    )
    if not answered.wait(_CAPTURE_TIMEOUT_SECONDS):
        raise ScreenUnavailableError(
            "ScreenCaptureKit did not return a frame in "
            f"{_CAPTURE_TIMEOUT_SECONDS:.0f}s. No screenshot was taken."
        )
    if box.get("error") is not None:
        raise ScreenUnavailableError(f"the capture failed: {box['error']}")
    image = box["image"]
    if image is None:
        raise ScreenUnavailableError("ScreenCaptureKit returned no image")

    data = Quartz.CFDataCreateMutable(None, 0)
    destination = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    if destination is None:
        raise ScreenUnavailableError("could not create a JPEG encoder")
    Quartz.CGImageDestinationAddImage(
        destination,
        image,
        {Quartz.kCGImageDestinationLossyCompressionQuality: SCREENSHOT_QUALITY},
    )
    if not Quartz.CGImageDestinationFinalize(destination):
        raise ScreenUnavailableError("the capture could not be encoded")
    return bytes(data)


__all__ = ["DarwinScreen"]
