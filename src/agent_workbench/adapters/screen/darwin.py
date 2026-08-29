# pyright: reportMissingImports=false, reportMissingTypeStubs=false
# pyright: reportAttributeAccessIssue=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false
# pyright: reportUntypedBaseClass=false
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
# point of keeping the FFI in one thin module (ADR-070) -- and the reason
# ADR-092 put the AppKit run loop in here rather than in the entry point that
# needs it.
#
# `reportUntypedBaseClass` is the newest of them and the narrowest: subclassing
# `NSObject` is how a timer target is written, and the bridge builds that base
# class at runtime, so pyright has nothing to read.
"""The macOS half: Quartz for the screen, CGEvent for the cursor and keyboard.

Everything here is one operating system's answer to a question asked in
``ports/screen.py``, and nothing here decides anything. The tier gate, the
budget and the focus check live above and are tested without a screen; this
module is the part that cannot be, so it is kept as thin as it can be made.

Three properties are worth stating because they are easy to lose:

**Points in, pixels only inside.** The port speaks points -- global ones for
input, this display's own for a capture (ADR-090) -- and this module converts
to pixels once, at the edge. A retina panel reports 1470x956 points over
2940x1912 pixels, and a click computed in the wrong one of those lands at twice
or half the intended place -- silently, because a click that misses is still a
click.

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
import contextlib
import threading
import time
from collections.abc import Callable
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

#: How long to wait for an activation to show up in `frontmost()`, and how
#: often to look.
#:
#: `activateWithOptions:` posts a request and returns; the reorder is the
#: window server's to schedule, so a `frontmost()` read taken on the next line
#: can still report the old application.
#:
#: **These two numbers are not measured**, unlike every other number in this
#: file. The `computer-use` extra is not installed in the checkout this was
#: written in, and the measurement is not one that can be taken quietly: it
#: means repeatedly pulling windows to the front of a screen somebody is
#: looking at. They are therefore chosen to be generous rather than tight --
#: two seconds is far more than a window reorder needs and far less than any
#: client's own timeout, and 20 ms of polling costs nothing next to the
#: ~70 ms a filtered capture takes (ADR-076 §4).
#:
#: Reaching the ceiling is not an error here. It means something else is
#: frontmost, which is a fact the caller has to state either way -- so a wrong
#: guess at these numbers produces a slower honest answer, never a wrong one.
_ACTIVATION_TIMEOUT_SECONDS: Final[float] = 2.0
_ACTIVATION_POLL_SECONDS: Final[float] = 0.02

#: How long a synthesized press is held, and how long between characters.
#:
#: Zero would be faster and is wrong: an application that reads modifier state
#: on key-down and the character on key-up can observe the two in the same
#: event-loop pass and drop one. 8 ms is below human perception and above the
#: pass.
_PRESS_SECONDS: Final[float] = 0.008


def can_change_frontmost() -> str | None:
    """Why this process cannot bring an application forward, or ``None``.

    Four things have to be true at once on this platform, and three of them
    are properties of *how the process was started* rather than of anything a
    caller passes in. Measured 2026-08-29 by holding the others fixed and
    removing one at a time (ADR-092 §2):

    ==========================  ==================================
    missing                     activations that took
    ==========================  ==================================
    bundle identity             0/20  (a bare interpreter)
    code signature              0/10  (grant does not attach)
    Accessibility grant         0/10
    main-thread run loop        0/15
    all four present            15/15
    ==========================  ==================================

    Checked so a misconfigured deployment says *why* instead of timing out.
    Every one of those failures is silent at the API: `activateWithOptions_`
    returns true and the screen does not change, which is the same shape
    ADR-070 §4 refuses for `CGEventPost`.

    The run loop cannot be pre-flighted at construction the way the two grants
    are -- it does not exist yet when the composition root builds the adapter
    -- so this is asked at the moment of use instead.
    """

    if AppKit.NSRunningApplication.currentApplication().bundleIdentifier() is None:
        return (
            "this server is not running as a bundled application, so macOS "
            "will not let it change which application is frontmost. Start it "
            "from the .app built by `scripts/build_computer_app.sh` "
            "(`scripts/dev.sh computer-server` does this)."
        )
    if not AppKit.NSApplication.sharedApplication().isRunning():
        return (
            "this server has no main-thread run loop, so macOS will not let "
            "it change which application is frontmost. This is a defect "
            "rather than a configuration mistake: the entry point is supposed "
            "to hand the main thread to AppKit (ADR-092)."
        )
    return None


def give_main_thread_to_appkit(*, serving: Callable[[], bool]) -> None:
    """Register with the window server and run AppKit's loop until serving stops.

    This is the fourth of the four conditions in :func:`can_change_frontmost`,
    and the one that cost the most to find: with a bundle, a signature and the
    Accessibility grant all in place, activation still failed 15 times out of
    15 without it, and succeeded 15 out of 15 with it.

    ``NSApplicationActivationPolicyAccessory``: registered with the window
    server -- which is what activation needs -- while owning no Dock icon and
    no menu bar. A server that put an icon in somebody's Dock would be
    announcing itself as an application they can switch to, and it is not one.

    The timer is not decoration either. ``run()`` is a C call that never
    yields, so a Ctrl-C would set Python's flag with nothing left to check it
    and the process would only answer SIGKILL. Waking every 250 ms gives the
    interpreter somewhere to notice both the signal and a server thread that
    has stopped on its own -- which is a crash rather than a shutdown, because
    nothing else stops it.
    """

    ns_app = AppKit.NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    class _Ticker(AppKit.NSObject):
        def tick_(self, timer: Any) -> None:
            del timer
            if not serving():
                AppKit.NSApplication.sharedApplication().terminate_(None)

    ticker = _Ticker.alloc().init()
    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.25, ticker, "tick:", None, True
    )
    # Ctrl-C reaches Python only because of the timer above; suppressing it
    # here is what turns it into an ordinary shutdown instead of a traceback
    # out of a run loop.
    with contextlib.suppress(KeyboardInterrupt):  # pragma: no cover - needs a tty
        ns_app.run()


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
        # The second grant, and until 2026-08-29 it was not checked at all --
        # which made this constructor enforce half of what two documents said
        # it enforced. ADR-070 §4 argued the whole principle from this exact
        # API ("`CGEventPost` returns success having done nothing"), and
        # `config.computer-local.toml` promised in as many words that the
        # server "exits at startup with a message naming the one that is
        # missing". Screen Recording was named and checked; Accessibility was
        # named and not.
        #
        # Measured on this machine 2026-08-29, with Screen Recording granted
        # and Accessibility not: `CGPreflightScreenCaptureAccess()` True,
        # `CGPreflightPostEventAccess()` False -- and every input path was a
        # silent no-op. `activateWithOptions_` returned True and left the
        # frontmost application untouched through ten attempts and a
        # ten-second budget; a click or a keystroke would have done the same,
        # reporting "Clicked in Finder." to a model while nothing moved. That
        # is the precise state ADR-070 §4 said this constructor exists to
        # prevent.
        #
        # Screen Recording and this one are separate TCC grants and a process
        # can hold either without the other, so they are two checks rather
        # than one -- and the message names which is missing, because "screen
        # permissions" sends somebody to the wrong pane of System Settings.
        if not Quartz.CGPreflightPostEventAccess():
            # Asks once, like the grant above. macOS shows its own dialog and
            # remembers the answer; it never becomes true within this process,
            # because the grant is read at launch.
            Quartz.CGRequestPostEventAccess()
        if not Quartz.CGPreflightPostEventAccess():
            raise ScreenUnavailableError(
                "this process has no Accessibility permission, so every click "
                "and keystroke it synthesizes would report success and do "
                "nothing, and no application could be brought to the front. "
                "Grant it in System Settings > Privacy & Security > "
                "Accessibility for the terminal or application running the "
                "server, then restart it -- macOS only re-reads the grant at "
                "launch."
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
        # Where this screen sits in the space `CGEventPost` reads, which is the
        # whole of ADR-090 on this side of the port. `CGDisplayBounds` is the
        # only source for it and reports points, same as everything else here.
        # The conversion that uses it is arithmetic and lives in
        # `domain/computer.py`, where it is tested against a second monitor
        # nobody has to own.
        bounds = Quartz.CGDisplayBounds(display_id)
        return Display(
            display_id=int(display_id),
            width=int(points_wide),
            height=int(points_high),
            # Derived rather than assumed to be 1 or 2: an external panel
            # running a scaled mode is neither.
            scale_factor=(pixels_wide / points_wide) if points_wide else 1.0,
            origin_x=int(bounds.origin.x),
            origin_y=int(bounds.origin.y),
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

    # --- bringing forward ------------------------------------------------

    async def activate(self, bundle_id: str) -> ApplicationIdentity | None:
        """Ask the window server to bring one running application forward.

        `runningApplicationsWithBundleIdentifier:` is the whole of the lookup,
        and it is also the whole of the launching story: it finds processes,
        and an empty result means there is nothing to bring forward. This
        adapter never reaches for `launchApplication` or `openApplicationAtURL`
        (ADR-091 §4).

        `NSApplicationActivateAllWindows` rather than bare activation, because
        an application with a document window and an inspector should arrive
        with both -- a task that brought forward one window of two would be
        looking at a screen the person would not recognise.

        The polling afterwards is not defensive padding. `activate` returns as
        soon as the request is *posted*, and the reorder happens on the window
        server's own schedule -- so a `frontmost()` read on the next line may
        still report the old application, and a gate that trusted it would
        refuse a request it had just granted. Polling turns that race into a
        bounded wait with an honest answer at the end of it: what is frontmost,
        whether or not it is what was asked for.
        """

        # Asked before anything is attempted, because every way this can be
        # wrong fails *silently*: the call returns true and the screen does
        # not change. A two-second timeout that says "it did not take" would
        # be true and useless -- the caller cannot tell a stubborn window from
        # a server nobody bundled (ADR-092 §3).
        blocked = can_change_frontmost()
        if blocked is not None:
            raise ScreenUnavailableError(blocked)
        running = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            bundle_id
        )
        if not running:
            return None
        running[0].activateWithOptions_(AppKit.NSApplicationActivateAllWindows)
        deadline = time.monotonic() + _ACTIVATION_TIMEOUT_SECONDS
        while True:
            now = self.frontmost()
            if now.bundle_id == bundle_id or time.monotonic() >= deadline:
                return now
            await asyncio.sleep(_ACTIVATION_POLL_SECONDS)

    # --- acting ------------------------------------------------------------

    async def click(
        self, x: int, y: int, *, button: MouseButton = "left", count: int = 1
    ) -> None:
        # Global points, as the port promises. This module never learns which
        # display they came from and must not try to: the one conversion
        # happened in the gate, and a second one here would undo it.
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

    async def _move(self, x: int, y: int) -> None:
        """Put the cursor somewhere. Private, and the privacy is the point.

        This was a port method until 2026-08-28 and had exactly one caller:
        `scroll`, below, reaching its own implementation. Nothing above the
        port ever called it, no tool exposed it, and `_ALLOWED` listed
        `mouse_move` for a gate method that did not exist -- a capability
        claimed at three layers and performed at none (ADR-091 §2.4).

        A scroll still has to position the cursor first: CGEvent's scroll wheel
        event carries no location, so it lands wherever the cursor is.
        """

        event = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, Quartz.CGPointMake(float(x), float(y)), 0
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    async def scroll(
        self, x: int, y: int, *, direction: ScrollDirection, amount: int
    ) -> None:
        await self._move(x, y)
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
