# pyright: reportAttributeAccessIssue=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false
#
# Four rules, and each is the same absence: `ctypes.WinDLL` exists only on
# Windows, so on the machine CI runs on every function reached through it is
# an attribute the module does not have (the first rule) with a type nobody
# can name (the other three) -- `SendInput`, `PrintWindow` and the rest are
# resolved from a DLL at run time and carry no type information anywhere.
# Everything else strict mode can say about this file -- an undefined name, a
# wrong argument count against its own functions, a Structure field typo --
# still fails the gate, which is the bargain `darwin.py` strikes with its
# eight suppressions and this file with four.
"""The Windows half: Win32 through ctypes for the screen, the cursor and the
keyboard (ADR-0108).

Everything here is one operating system's answer to a question asked in
``ports/screen.py``, and nothing here decides anything. The tier gate, the
budget and the focus check live above and are tested without a screen; this
module is the part that cannot be, and it is kept as thin as the platform
allows -- which is thicker than the macOS half, because Win32 has no
compositor filter to hand a window list to, and the frame has to be composed
here.

Four properties are worth stating because they are easy to lose:

**Physical pixels are the points.** The process declares itself per-monitor
DPI aware (v2) at construction, so every rectangle Windows reports and every
coordinate it accepts is in physical pixels of the monitor it belongs to. That
makes "point" and "pixel" the same unit on this platform, and the port's
``scale_factor`` a description rather than something anybody divides by. A
process that left the default awareness would be handed *virtualised*
coordinates on a scaled monitor -- 1280 wide for a 1920 panel at 150 % -- and
would click at two thirds of the intended place, silently.

**The frame is composed from approved windows only.** Win32 has no
``SCContentFilter``. What it has is ``PrintWindow`` with
``PW_RENDERFULLCONTENT``, which asks the compositor to render *one window* into
a bitmap the caller owns, occluded or not. So the adapter enumerates the
top-level windows, keeps those owned by an approved executable, renders each
into its own buffer and pastes them onto a blank canvas in z-order. An
unapproved window is never rendered, never read, never in any buffer this
process holds -- which is the promise ``exclude_native`` makes, kept by
composition rather than by filtering. The cost is real and is stated in
`capabilities`: an approved window that is *behind* an unapproved one shows
in full, where on macOS it would be covered.

**Input is counted, not assumed.** ``SendInput`` returns how many events it
inserted, and Windows refuses input to a window of higher integrity (UIPI --
an elevated application in front of a non-elevated sender) by inserting
nothing and returning zero. That is an error here, never a success. On macOS
the same situation is silent; here it is at least countable, and the count is
checked after every call.

**Nothing is cached.** ``frontmost`` in particular is read on every call,
through the frame host when a packaged application is in front. The whole
tier model rests on that being a fresh reading (ADR-070).
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

from agent_workbench.domain.computer import SCREENSHOT_QUALITY, ApplicationIdentity
from agent_workbench.ports.screen import (
    Capture,
    Display,
    MouseButton,
    ScreenUnavailableError,
    ScrollDirection,
)

_JPEG: Final[str] = "image/jpeg"

# --- the parts of Win32 this module speaks ------------------------------------

_MONITORINFOF_PRIMARY: Final[int] = 0x1
_MDT_EFFECTIVE_DPI: Final[int] = 0
#: `DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2`, a pseudo-handle.
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: Final[int] = -4

_PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
_DWMWA_CLOAKED: Final[int] = 14
_DWMWA_EXTENDED_FRAME_BOUNDS: Final[int] = 9
_PW_RENDERFULLCONTENT: Final[int] = 0x2
_SW_RESTORE: Final[int] = 9
_GWL_EXSTYLE: Final[int] = -20
_WS_EX_TOOLWINDOW: Final[int] = 0x0000_0080

_INPUT_MOUSE: Final[int] = 0
_INPUT_KEYBOARD: Final[int] = 1
_MOUSEEVENTF_LEFTDOWN: Final[int] = 0x0002
_MOUSEEVENTF_LEFTUP: Final[int] = 0x0004
_MOUSEEVENTF_RIGHTDOWN: Final[int] = 0x0008
_MOUSEEVENTF_RIGHTUP: Final[int] = 0x0010
_MOUSEEVENTF_MIDDLEDOWN: Final[int] = 0x0020
_MOUSEEVENTF_MIDDLEUP: Final[int] = 0x0040
_MOUSEEVENTF_WHEEL: Final[int] = 0x0800
_MOUSEEVENTF_HWHEEL: Final[int] = 0x1000
_WHEEL_DELTA: Final[int] = 120
_KEYEVENTF_KEYUP: Final[int] = 0x0002
_KEYEVENTF_UNICODE: Final[int] = 0x0004

_BI_RGB: Final[int] = 0
_DIB_RGB_COLORS: Final[int] = 0

#: Win32's own button flags: (down, up).
_BUTTON: Final[dict[MouseButton, tuple[int, int]]] = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}

#: Which chord words are modifiers, and the virtual key each is.
#:
#: `cmd` is the Windows key here, not Control. A model reading the tool
#: description sees "cmd+c" as an example and could mean either; making it
#: Control would turn `cmd+l` into "focus the address bar" on a browser the
#: gate has already refused to type into, and making it Win keeps the word
#: meaning "the platform's command key", which is what it means on macOS.
_MODIFIERS: Final[dict[str, int]] = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "option": 0x12,
    "win": 0x5B,
    "windows": 0x5B,
    "super": 0x5B,
    "cmd": 0x5B,
    "command": 0x5B,
}

#: The named keys a chord may end in, as virtual key codes. Layout-independent
#: on this platform, unlike letters -- `VkKeyScanW` resolves those against the
#: active layout at press time, which is a thing the macOS half cannot do and
#: is why it carries a US-only table.
_KEY_CODES: Final[dict[str, int]] = {
    "return": 0x0D,
    "enter": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x08,
    "forwarddelete": 0x2E,
    "escape": 0x1B,
    "esc": 0x1B,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}

#: Where the frontmost window of a packaged application actually lives. The
#: window Windows reports as foreground for a Store application belongs to
#: this host process; the application is a child window inside it, and it is
#: the child's process that the tier table has to be asked about.
_FRAME_HOST: Final[str] = "applicationframehost.exe"

#: How long to wait for an activation to show up in `frontmost()`, and how
#: often to look. `SetForegroundWindow` returns before the switch is visible,
#: and a `frontmost()` read on the next line can report the old window.
#:
#: **These numbers are not measured**, for the same reason `darwin.py` says
#: its own pair is not: no Windows machine took part in writing this file. A
#: wrong guess produces a slower honest answer, never a wrong one.
_ACTIVATION_TIMEOUT_SECONDS: Final[float] = 2.0
_ACTIVATION_POLL_SECONDS: Final[float] = 0.02

#: How long a synthesized press is held, and how long between characters. Same
#: reasoning as the macOS half: zero lets an application see a key-down and a
#: key-up in one message-loop pass and drop one; 8 ms is below perception.
_PRESS_SECONDS: Final[float] = 0.008

#: What the canvas is before any approved window is pasted onto it: a flat
#: grey, not the wallpaper. The wallpaper is not a window and no allowlist
#: names it, and a person's desktop picture is theirs. Mid-grey rather than
#: black so a window with a dark theme keeps an edge.
_CANVAS_GREY: Final[tuple[int, int, int]] = (64, 64, 64)


# --- structures ---------------------------------------------------------------
#
# Declared here rather than imported from `ctypes.wintypes` so that this
# module imports on every platform: the chord parser and the frame composer
# below are pure and are tested where CI runs, and a module that could only be
# imported on Windows would leave them tested nowhere.


class _RECT(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    )


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_uint32),
        ("szDevice", ctypes.c_wchar * 32),
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT))


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", ctypes.c_uint32), ("u", _INPUTUNION))


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    )


class _BITMAPINFO(ctypes.Structure):
    _fields_ = (("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3))


# --- pure parts, tested everywhere --------------------------------------------


def parse_chord(combination: str) -> tuple[tuple[int, ...], int | str]:
    """A chord as ``(modifier virtual keys, key)``.

    The key is a virtual key code for a named key and a single character
    otherwise; a character is resolved against the active keyboard layout at
    press time (`VkKeyScanW`), or typed as text when nothing modifies it. Every
    refusal here is a `ValueError` naming the part that was not understood,
    because a chord this adapter mis-parses sends the wrong keystroke to a
    real application.
    """

    parts = [part.strip().casefold() for part in combination.split("+")]
    if not parts or not parts[-1]:
        raise ValueError(f"{combination!r} names no key")
    modifiers: list[int] = []
    for modifier in parts[:-1]:
        code = _MODIFIERS.get(modifier)
        if code is None:
            raise ValueError(f"{modifier!r} is not a modifier")
        modifiers.append(code)
    name = parts[-1]
    named = _KEY_CODES.get(name)
    if named is not None:
        return tuple(modifiers), named
    if len(name) != 1:
        raise ValueError(f"{name!r} is not a key this adapter knows")
    return tuple(modifiers), name


@dataclass(frozen=True, slots=True)
class WindowSlice:
    """One approved window's render, and where it sits on the display.

    ``left`` and ``top`` are the window's position in the display's own
    pixels; the image is the window at its natural size. Negative offsets are
    ordinary -- a window half off the left edge -- and the composer clips.
    """

    left: int
    top: int
    image: Any


def compose_frame(
    display_width: int,
    display_height: int,
    slices: tuple[WindowSlice, ...],
    *,
    width: int,
    height: int,
) -> bytes:
    """Paste the approved windows onto a blank canvas, bottom-most first, and
    encode the result at the budgeted size.

    Pure, given the slices, so that the one security-relevant property of the
    Windows capture -- that the canvas holds nothing but what it was handed --
    is asserted on every platform rather than only on one machine.
    """

    image_module = _pillow()
    canvas = image_module.new("RGB", (display_width, display_height), _CANVAS_GREY)
    for held in slices:
        canvas.paste(held.image, (held.left, held.top))
    if (width, height) != (display_width, display_height):
        canvas = canvas.resize((width, height), image_module.LANCZOS)
    import io

    encoded = io.BytesIO()
    canvas.save(encoded, format="JPEG", quality=int(SCREENSHOT_QUALITY * 100))
    return encoded.getvalue()


def _pillow() -> Any:
    try:
        from PIL import Image  # pyright: ignore[reportMissingImports]
    except ImportError as missing:
        raise ScreenUnavailableError(
            "computer use on Windows needs Pillow, which is behind the "
            "`computer-use` extra: uv sync --extra computer-use"
        ) from missing
    return Image


# --- the platform ---------------------------------------------------------------


class _Native:
    """The four libraries, with the signatures this module calls them by.

    Resolved once, at first use, and never at import: `ctypes.WinDLL` exists
    only on Windows, and this module has to import everywhere so the pure half
    above can be tested where CI runs.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ScreenUnavailableError(
                f"the Windows screen adapter cannot run on {sys.platform}"
            )
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        self.shcore = ctypes.WinDLL("shcore", use_last_error=True)
        self.version = ctypes.WinDLL("version", use_last_error=True)
        u, g, k, d, s, v = (
            self.user32,
            self.gdi32,
            self.kernel32,
            self.dwmapi,
            self.shcore,
            self.version,
        )
        # BOOL is a 32-bit int, and a nonzero one is TRUE. `c_bool` would read
        # only the low byte of the return register, so a function that answers
        # TRUE as 0x100 -- documented as possible for every BOOL -- would read
        # as False. Every BOOL in and out is therefore a `c_int`, and callers
        # test truthiness.
        p, i, b = ctypes.c_void_p, ctypes.c_int, ctypes.c_int

        # Windows 10 1703+ for the v2 context; the older call is the fallback
        # for anything before it and exists on every Windows this could run
        # on. Resolved through `getattr` because a missing export raises at
        # attribute access, before the fallback in `Win32Screen.__init__`
        # could run -- which was how the fallback did not exist at all until
        # a review read this constructor.
        self.set_dpi_context: Any | None = getattr(
            u, "SetProcessDpiAwarenessContext", None
        )
        if self.set_dpi_context is not None:
            self.set_dpi_context.argtypes = (p,)
            self.set_dpi_context.restype = b
        u.SetProcessDPIAware.argtypes = ()
        u.SetProcessDPIAware.restype = b
        self.monitor_enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_int, p, p, ctypes.POINTER(_RECT), ctypes.c_void_p
        )
        u.EnumDisplayMonitors.argtypes = (p, p, self.monitor_enum_proc, ctypes.c_void_p)
        u.EnumDisplayMonitors.restype = b
        u.GetMonitorInfoW.argtypes = (p, ctypes.POINTER(_MONITORINFOEXW))
        u.GetMonitorInfoW.restype = b
        # Windows 8.1+. Absent, every monitor reports 96 dpi below, which is a
        # description that is wrong on a scaled panel and nothing more: the
        # port's `scale_factor` is carried, never divided by (ADR-0108 §3.2).
        self.get_dpi_for_monitor: Any | None = getattr(s, "GetDpiForMonitor", None)
        if self.get_dpi_for_monitor is not None:
            self.get_dpi_for_monitor.argtypes = (
                p,
                i,
                ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_uint),
            )
            self.get_dpi_for_monitor.restype = ctypes.c_long
        u.GetClassNameW.argtypes = (p, ctypes.c_wchar_p, i)
        u.GetClassNameW.restype = i
        u.GetForegroundWindow.argtypes = ()
        u.GetForegroundWindow.restype = p
        u.GetWindowThreadProcessId.argtypes = (p, ctypes.POINTER(ctypes.c_uint32))
        u.GetWindowThreadProcessId.restype = ctypes.c_uint32
        k.OpenProcess.argtypes = (ctypes.c_uint32, b, ctypes.c_uint32)
        k.OpenProcess.restype = p
        k.CloseHandle.argtypes = (p,)
        k.CloseHandle.restype = b
        k.QueryFullProcessImageNameW.argtypes = (
            p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        )
        k.QueryFullProcessImageNameW.restype = b
        k.GetCurrentThreadId.argtypes = ()
        k.GetCurrentThreadId.restype = ctypes.c_uint32
        self.window_enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, p, ctypes.c_void_p)
        u.EnumWindows.argtypes = (self.window_enum_proc, ctypes.c_void_p)
        u.EnumWindows.restype = b
        u.EnumChildWindows.argtypes = (p, self.window_enum_proc, ctypes.c_void_p)
        u.EnumChildWindows.restype = b
        u.IsWindowVisible.argtypes = (p,)
        u.IsWindowVisible.restype = b
        u.IsIconic.argtypes = (p,)
        u.IsIconic.restype = b
        u.GetWindowLongW.argtypes = (p, i)
        u.GetWindowLongW.restype = ctypes.c_long
        u.GetWindowRect.argtypes = (p, ctypes.POINTER(_RECT))
        u.GetWindowRect.restype = b
        u.GetWindowTextW.argtypes = (p, ctypes.c_wchar_p, i)
        u.GetWindowTextW.restype = i
        d.DwmGetWindowAttribute.argtypes = (p, ctypes.c_uint32, p, ctypes.c_uint32)
        d.DwmGetWindowAttribute.restype = ctypes.c_long
        u.PrintWindow.argtypes = (p, p, ctypes.c_uint)
        u.PrintWindow.restype = b
        u.GetDC.argtypes = (p,)
        u.GetDC.restype = p
        u.ReleaseDC.argtypes = (p, p)
        u.ReleaseDC.restype = i
        g.CreateCompatibleDC.argtypes = (p,)
        g.CreateCompatibleDC.restype = p
        g.CreateDIBSection.argtypes = (
            p,
            ctypes.POINTER(_BITMAPINFO),
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
            p,
            ctypes.c_uint32,
        )
        g.CreateDIBSection.restype = p
        g.SelectObject.argtypes = (p, p)
        g.SelectObject.restype = p
        g.DeleteObject.argtypes = (p,)
        g.DeleteObject.restype = b
        g.DeleteDC.argtypes = (p,)
        g.DeleteDC.restype = b
        u.SetForegroundWindow.argtypes = (p,)
        u.SetForegroundWindow.restype = b
        u.ShowWindow.argtypes = (p, i)
        u.ShowWindow.restype = b
        u.BringWindowToTop.argtypes = (p,)
        u.BringWindowToTop.restype = b
        u.AttachThreadInput.argtypes = (ctypes.c_uint32, ctypes.c_uint32, b)
        u.AttachThreadInput.restype = b
        u.SetCursorPos.argtypes = (i, i)
        u.SetCursorPos.restype = b
        u.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), i)
        u.SendInput.restype = ctypes.c_uint
        u.VkKeyScanW.argtypes = (ctypes.c_wchar,)
        u.VkKeyScanW.restype = ctypes.c_short
        v.GetFileVersionInfoSizeW.argtypes = (
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        )
        v.GetFileVersionInfoSizeW.restype = ctypes.c_uint32
        v.GetFileVersionInfoW.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            p,
        )
        v.GetFileVersionInfoW.restype = b
        v.VerQueryValueW.argtypes = (
            p,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint),
        )
        v.VerQueryValueW.restype = b


@dataclass(frozen=True, slots=True)
class _Window:
    handle: int
    application: ApplicationIdentity
    frame: _RECT


class Win32Screen:
    """A :class:`ScreenPort` backed by user32, gdi32 and the desktop window
    manager."""

    def __init__(self) -> None:
        self._native = _Native()
        # Declared before any window is touched, because it can only be
        # declared once per process and the default is the one that lies
        # about coordinates on a scaled monitor (see the module docstring).
        # The v2 context is Windows 10 1703+; the older call is the fallback
        # for anything before it, and gives system-wide rather than
        # per-monitor awareness -- correct on one monitor, approximate across
        # two at different scales.
        context = self._native.set_dpi_context
        if context is None or not context(
            ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        ):
            self._native.user32.SetProcessDPIAware()
        # Pre-flighted at construction, not at first screenshot, for the reason
        # the macOS constructor pre-flights its two grants: a startup problem
        # must not become a turn's problem.
        _pillow()
        if not self.displays():
            raise ScreenUnavailableError(
                "this Windows session reports no display; a service session "
                "(session 0) has no desktop to act on"
            )

    # --- looking -------------------------------------------------------------

    def displays(self) -> tuple[Display, ...]:
        found: list[tuple[bool, Display]] = []
        user32, dpi_of = self._native.user32, self._native.get_dpi_for_monitor

        def visit(monitor: int, _dc: int, _rect: Any, _data: int) -> int:
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return 1
            dpi_x, dpi_y = ctypes.c_uint(96), ctypes.c_uint(96)
            if (
                dpi_of is None
                or dpi_of(
                    monitor,
                    _MDT_EFFECTIVE_DPI,
                    ctypes.byref(dpi_x),
                    ctypes.byref(dpi_y),
                )
                != 0
            ):
                dpi_x.value = 96
            rect = info.rcMonitor
            found.append(
                (
                    bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                    Display(
                        display_id=int(monitor),
                        width=int(rect.right - rect.left),
                        height=int(rect.bottom - rect.top),
                        # A description, not a divisor: points *are* physical
                        # pixels here (module docstring). Carried so a caller
                        # can reason about how much detail a capture holds.
                        scale_factor=dpi_x.value / 96.0,
                        origin_x=int(rect.left),
                        origin_y=int(rect.top),
                    ),
                )
            )
            return 1

        callback = self._native.monitor_enum_proc(visit)
        user32.EnumDisplayMonitors(None, None, callback, None)
        # Primary first, which the port promises. A caller that takes `[0]`
        # is asking for "the screen the person is looking at".
        found.sort(key=lambda held: not held[0])
        return tuple(display for _, display in found)

    def frontmost(self) -> ApplicationIdentity:
        handle = self._native.user32.GetForegroundWindow()
        if not handle:
            # No focused window at all -- the lock screen, or a moment between
            # two. Treated as an unknown application, which the allowlist
            # refuses because an empty identity is in nobody's allowlist.
            return ApplicationIdentity(bundle_id="", name="")
        return self._identity_of_window(int(handle))

    def capabilities(self) -> frozenset[str]:
        # `exclude_native`, and the word is kept honestly rather than
        # borrowed: an unapproved window is never rendered into any buffer
        # this process holds, because the frame is *composed* from per-window
        # renders of approved windows only (`compose_frame`). It is not the
        # compositor filtering a full-screen capture; it is this adapter never
        # asking for one. The visible difference from macOS is that an
        # approved window behind an unapproved one shows in full here, where
        # SCContentFilter would show it covered -- more of an approved window,
        # never any of an unapproved one.
        return frozenset({"exclude_native"})

    async def capture(
        self,
        display_id: int,
        *,
        width: int,
        height: int,
        include_bundle_ids: tuple[str, ...] = (),
    ) -> Capture:
        display = next(
            (held for held in self.displays() if held.display_id == display_id), None
        )
        if display is None:
            raise ScreenUnavailableError(f"this machine has no display {display_id}")
        # Off the event loop: a `PrintWindow` per approved window, each a
        # compositor round trip, and this server answers other calls while a
        # capture is in flight.
        content = await asyncio.to_thread(
            self._render, display, width, height, frozenset(include_bundle_ids)
        )
        return Capture(
            media_type=_JPEG,
            content=content,
            width=width,
            height=height,
            display=display,
        )

    def _render(
        self, display: Display, width: int, height: int, wanted: frozenset[str]
    ) -> bytes:
        slices: list[WindowSlice] = []
        # `_windows` yields top-most first; the canvas wants bottom-most
        # pasted first so the top-most ends up on top.
        for window in reversed(tuple(self._windows())):
            if window.application.bundle_id not in wanted:
                continue
            frame = window.frame
            if (
                frame.right <= display.origin_x
                or frame.left >= display.origin_x + display.width
                or frame.bottom <= display.origin_y
                or frame.top >= display.origin_y + display.height
            ):
                continue
            image = self._print_window(window.handle)
            if image is None:
                # The compositor would not render it (a DirectX exclusive
                # surface, a window mid-destruction). Left out rather than
                # painted as a placeholder: a grey box where a window should be
                # is a model clicking on nothing, and an absence it can see is
                # better than a shape it cannot read.
                continue
            slices.append(
                WindowSlice(
                    left=int(frame.left - display.origin_x),
                    top=int(frame.top - display.origin_y),
                    image=image,
                )
            )
        return compose_frame(
            display.width, display.height, tuple(slices), width=width, height=height
        )

    def _print_window(self, handle: int) -> Any | None:
        """One window, rendered by the compositor into a buffer this process
        owns, as a Pillow image -- or ``None`` when it would not render."""

        user32, gdi32 = self._native.user32, self._native.gdi32
        rect = _RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(rect)):
            return None
        w, h = int(rect.right - rect.left), int(rect.bottom - rect.top)
        if w <= 0 or h <= 0:
            return None
        screen_dc = user32.GetDC(None)
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = w
        # Negative height: a top-down DIB, so row 0 is the top row and the
        # buffer can be handed to Pillow without flipping.
        info.bmiHeader.biHeight = -h
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = _BI_RGB
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            memory_dc, ctypes.byref(info), _DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        try:
            if not bitmap or not bits.value:
                return None
            previous = gdi32.SelectObject(memory_dc, bitmap)
            rendered = user32.PrintWindow(handle, memory_dc, _PW_RENDERFULLCONTENT)
            gdi32.SelectObject(memory_dc, previous)
            if not rendered:
                return None
            raw = ctypes.string_at(bits.value, w * h * 4)
            image_module = _pillow()
            # Copied out of the DIB before the section is deleted below; a
            # `frombuffer` view over freed memory would be a picture of
            # whatever the allocator did next.
            return image_module.frombuffer(
                "RGB", (w, h), raw, "raw", "BGRX", 0, 1
            ).copy()
        finally:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(None, screen_dc)

    def _windows(self) -> Iterator[_Window]:
        """Top-level windows a person could see, top-most first, each with the
        application that owns it and its extended frame."""

        user32, dwmapi = self._native.user32, self._native.dwmapi
        handles: list[int] = []

        def visit(handle: int, _data: int) -> int:
            handles.append(int(handle))
            return 1

        user32.EnumWindows(self._native.window_enum_proc(visit), None)
        for handle in handles:
            if not user32.IsWindowVisible(handle) or user32.IsIconic(handle):
                continue
            if user32.GetWindowLongW(handle, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
                continue
            cloaked = ctypes.c_uint32(0)
            dwmapi.DwmGetWindowAttribute(
                handle, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
            )
            if cloaked.value:
                # Cloaked: a window that exists but is not drawn (another
                # virtual desktop, a suspended Store app). Not on screen, so
                # not in the picture -- and not something to bring forward.
                continue
            frame = _RECT()
            if (
                dwmapi.DwmGetWindowAttribute(
                    handle,
                    _DWMWA_EXTENDED_FRAME_BOUNDS,
                    ctypes.byref(frame),
                    ctypes.sizeof(frame),
                )
                != 0
            ):
                user32.GetWindowRect(handle, ctypes.byref(frame))
            if frame.right <= frame.left or frame.bottom <= frame.top:
                continue
            yield _Window(
                handle=handle, application=self._identity_of_window(handle), frame=frame
            )

    def _identity_of_window(self, handle: int) -> ApplicationIdentity:
        """The application behind a window, seen through the frame host.

        For a packaged application the foreground window belongs to
        `ApplicationFrameHost.exe`, and the application itself is a child
        window with its own process. The tier table has to hear the child's
        name: the frame host is the same executable in front of every Store
        application, a browser included, and would be tier "full".
        """

        user32 = self._native.user32
        pid = ctypes.c_uint32(0)
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        executable, path = self._executable_of(pid.value)
        if executable == _FRAME_HOST:
            children: list[int] = []

            def visit(child: int, _data: int) -> int:
                children.append(int(child))
                return 1

            user32.EnumChildWindows(handle, self._native.window_enum_proc(visit), None)
            # The application's own window is the child of class
            # `Windows.UI.Core.CoreWindow`; the frame host can hold other
            # foreign children (a splash, an input host), so the class is
            # preferred and "the first child of another process" is only the
            # fallback for a host that shows no CoreWindow at all.
            foreign: list[int] = []
            chosen: int | None = None
            for child in children:
                child_pid = ctypes.c_uint32(0)
                user32.GetWindowThreadProcessId(child, ctypes.byref(child_pid))
                if not child_pid.value or child_pid.value == pid.value:
                    continue
                foreign.append(child_pid.value)
                buffer = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(child, buffer, 256)
                if buffer.value == "Windows.UI.Core.CoreWindow":
                    chosen = child_pid.value
                    break
            if chosen is None and foreign:
                chosen = foreign[0]
            if chosen is not None:
                executable, path = self._executable_of(chosen)
        return ApplicationIdentity(
            bundle_id=executable, name=self._name_of(path, handle, executable)
        )

    def _executable_of(self, pid: int) -> tuple[str, str]:
        """``(lower-cased file name, full path)`` of a process, or empties."""

        kernel32 = self._native.kernel32
        process = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return "", ""
        try:
            size = ctypes.c_uint32(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(size)
            ):
                return "", ""
        finally:
            kernel32.CloseHandle(process)
        path = buffer.value
        return os.path.basename(path).casefold(), path

    def _name_of(self, path: str, handle: int, executable: str) -> str:
        """A name a person recognises: the file's description, else the window
        title, else the executable's stem."""

        described = self._file_description(path) if path else ""
        if described:
            return described
        buffer = ctypes.create_unicode_buffer(512)
        self._native.user32.GetWindowTextW(handle, buffer, 512)
        if buffer.value:
            return buffer.value
        return os.path.splitext(executable)[0]

    def _file_description(self, path: str) -> str:
        version = self._native.version
        handle = ctypes.c_uint32(0)
        size = version.GetFileVersionInfoSizeW(path, ctypes.byref(handle))
        if not size:
            return ""
        block = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, block):
            return ""
        translation = ctypes.c_void_p()
        length = ctypes.c_uint(0)
        if (
            not version.VerQueryValueW(
                block,
                "\\VarFileInfo\\Translation",
                ctypes.byref(translation),
                ctypes.byref(length),
            )
            or not translation.value
            or length.value < 4
        ):
            return ""
        pair = ctypes.cast(translation, ctypes.POINTER(ctypes.c_uint16 * 2)).contents
        key = f"\\StringFileInfo\\{pair[0]:04x}{pair[1]:04x}\\FileDescription"
        text = ctypes.c_void_p()
        if (
            not version.VerQueryValueW(
                block, key, ctypes.byref(text), ctypes.byref(length)
            )
            or not text.value
        ):
            return ""
        return ctypes.wstring_at(text.value).strip()

    # --- bringing forward ------------------------------------------------------

    async def activate(self, bundle_id: str) -> ApplicationIdentity | None:
        """Ask Windows to bring one running application's window forward.

        Windows guards the foreground: a process may take it only if it is the
        foreground process, was started by it, or received the last input.
        `SetForegroundWindow` from a server that is none of those returns
        without moving anything -- the same silent shape ADR-092 found on
        macOS. What this tries, in order, synthesizes no input (ADR-091 §2.3:
        activation is not tier-gated *because* it types nothing): a plain
        request, then one with this thread's input queue attached to the
        foreground thread's, which is the documented way for a process to
        act as if it were that thread. What it never does is the "tap Alt"
        trick that folklore recommends, because that is a keystroke into a
        window nobody approved this call to type into.

        The polling afterwards is the same bounded wait `darwin.py` makes,
        for the same reason, and ends with the same honest answer: what is
        frontmost, whether or not it is what was asked for.
        """

        target = next(
            (
                window
                for window in self._windows()
                if window.application.bundle_id == bundle_id
            ),
            None,
        )
        if target is None:
            return None
        user32, kernel32 = self._native.user32, self._native.kernel32
        if user32.IsIconic(target.handle):
            user32.ShowWindow(target.handle, _SW_RESTORE)
        user32.SetForegroundWindow(target.handle)
        now = await self._settle(bundle_id)
        if now.bundle_id == bundle_id:
            return now
        foreground = user32.GetForegroundWindow()
        if foreground:
            theirs = user32.GetWindowThreadProcessId(foreground, None)
            ours = kernel32.GetCurrentThreadId()
            if (
                theirs
                and theirs != ours
                and user32.AttachThreadInput(ours, theirs, True)
            ):
                try:
                    user32.BringWindowToTop(target.handle)
                    user32.SetForegroundWindow(target.handle)
                finally:
                    user32.AttachThreadInput(ours, theirs, False)
        return await self._settle(bundle_id)

    async def _settle(self, bundle_id: str) -> ApplicationIdentity:
        deadline = time.monotonic() + _ACTIVATION_TIMEOUT_SECONDS
        while True:
            now = self.frontmost()
            if now.bundle_id == bundle_id or time.monotonic() >= deadline:
                return now
            await asyncio.sleep(_ACTIVATION_POLL_SECONDS)

    # --- acting ----------------------------------------------------------------

    async def click(
        self, x: int, y: int, *, button: MouseButton = "left", count: int = 1
    ) -> None:
        # Global pixels, as the port promises. This module never learns which
        # display they came from and must not try to: the one conversion
        # happened in the gate, and a second one here would undo it.
        self._native.user32.SetCursorPos(int(x), int(y))
        down, up = _BUTTON[button]
        for _ in range(count):
            self._send(_mouse(down), _mouse(up))
            await asyncio.sleep(_PRESS_SECONDS)

    async def scroll(
        self, x: int, y: int, *, direction: ScrollDirection, amount: int
    ) -> None:
        # A wheel event lands wherever the cursor is; it carries no location.
        self._native.user32.SetCursorPos(int(x), int(y))
        if direction in ("up", "down"):
            delta = _WHEEL_DELTA * amount * (1 if direction == "up" else -1)
            self._send(_mouse(_MOUSEEVENTF_WHEEL, data=delta))
        else:
            delta = _WHEEL_DELTA * amount * (1 if direction == "right" else -1)
            self._send(_mouse(_MOUSEEVENTF_HWHEEL, data=delta))

    async def type_text(self, text: str) -> int:
        """Type by unicode payload rather than by key code.

        `KEYEVENTF_UNICODE` puts the character on the event directly, which
        sidesteps the keyboard layout the way `CGEventKeyboardSetUnicodeString`
        does on macOS. A character outside the Basic Multilingual Plane is two
        UTF-16 units and two events; it is counted once, because the caller
        counts characters.

        Returns the count delivered, and stops at the first event Windows
        refuses (`SendInput` inserting nothing -- an elevated window took the
        focus mid-string), which is exactly the half-delivered case
        `domain.computer.focus_lost` is written for.
        """

        delivered = 0
        for character in text:
            events: list[_INPUT] = []
            units = character.encode("utf-16-le")
            for index in range(0, len(units), 2):
                code = int.from_bytes(units[index : index + 2], "little")
                events.append(_key(0, _KEYEVENTF_UNICODE, scan=code))
                events.append(_key(0, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, scan=code))
            if not self._send(*events, strict=False):
                return delivered
            delivered += 1
            await asyncio.sleep(_PRESS_SECONDS)
        return delivered

    async def key(self, combination: str) -> None:
        modifiers, key = parse_chord(combination)
        if isinstance(key, str):
            if not modifiers:
                # A bare letter is text, and typing it is layout-independent.
                await self.type_text(key)
                return
            scanned = int(self._native.user32.VkKeyScanW(key))
            if scanned == -1:
                raise ValueError(
                    f"{key!r} is not on the active keyboard layout, so it cannot "
                    "be pressed as a chord"
                )
            code = scanned & 0xFF
            # `VkKeyScanW` also says which modifiers the character needs on
            # this layout: bit 0 Shift (a capital, `:` on a US layout), bit 1
            # Control and bit 2 Alt -- both set for an AltGr character such
            # as `@` on a German layout. Each is added as a modifier so the
            # chord presses what the person would press; a review caught
            # that only the Shift bit was read.
            for bit, virtual_key in ((0x1, 0x10), (0x2, 0x11), (0x4, 0x12)):
                if (scanned >> 8) & bit and virtual_key not in modifiers:
                    modifiers = (*modifiers, virtual_key)
        else:
            code = key
        events = [_key(modifier, 0) for modifier in modifiers]
        events.append(_key(code, 0))
        events.append(_key(code, _KEYEVENTF_KEYUP))
        events.extend(
            _key(modifier, _KEYEVENTF_KEYUP) for modifier in reversed(modifiers)
        )
        self._send(*events)
        await asyncio.sleep(_PRESS_SECONDS)

    def _send(self, *events: _INPUT, strict: bool = True) -> bool:
        """`SendInput`, with its count checked.

        Windows inserts the events it will and reports how many. Fewer than
        asked is UIPI -- the window in front runs at a higher integrity level
        than this server, and Windows declined on the person's behalf. That is
        reported as unavailability rather than as a click that landed; with
        ``strict`` off (typing) it is returned, so the caller can say how far
        it got.
        """

        array = (_INPUT * len(events))(*events)
        inserted = int(
            self._native.user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
        )
        if inserted == len(events):
            return True
        if strict:
            raise ScreenUnavailableError(
                f"Windows inserted {inserted} of {len(events)} input events. The "
                "window in front is most likely elevated (running as "
                "administrator), and Windows refuses input to it from a process "
                "that is not; nothing was delivered."
            )
        return False


def _mouse(flags: int, *, data: int = 0) -> _INPUT:
    event = _INPUT()
    event.type = _INPUT_MOUSE
    event.mi.dx = 0
    event.mi.dy = 0
    # `mouseData` is a DWORD carrying a signed wheel delta; two's complement
    # is what the API reads back.
    event.mi.mouseData = data & 0xFFFF_FFFF
    event.mi.dwFlags = flags
    event.mi.time = 0
    event.mi.dwExtraInfo = None
    return event


def _key(virtual_key: int, flags: int, *, scan: int = 0) -> _INPUT:
    event = _INPUT()
    event.type = _INPUT_KEYBOARD
    event.ki.wVk = virtual_key
    event.ki.wScan = scan
    event.ki.dwFlags = flags
    event.ki.time = 0
    event.ki.dwExtraInfo = None
    return event


#: Exposed for the entry point's benefit only: a way to ask whether this
#: module's platform half can be constructed without constructing it.
def is_supported_platform() -> bool:
    return sys.platform == "win32"


def message_box_with_timeout(
    title: str, body: str, *, style: int, milliseconds: int
) -> int:
    """`MessageBoxTimeoutW`, which is `MessageBoxW` with a countdown.

    The consent dialog (`apps/computer_mcp/consent.py`) decides what an answer
    means; this puts the box up. Chosen over a `MessageBoxW` on a thread
    because that one cannot be dismissed from outside, so a request nobody was
    at the machine for would hold a tool call open until the client's own
    timeout killed it with no explanation -- the failure the macOS path's
    `giving up after` exists to prevent. It is an exported but undocumented
    `user32` entry point, present since Windows XP and used by the shell
    itself; if a future Windows drops it the answer is a refusal to ask, never
    a different dialog.

    Every string is an argument, never a script: there is no interpreter
    between this call and the screen for a model-chosen name to be code in.
    """

    if sys.platform != "win32":
        raise ScreenUnavailableError(
            f"there is no Windows message box on {sys.platform}"
        )
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        timed_box = user32.MessageBoxTimeoutW
    except AttributeError as missing:
        raise ScreenUnavailableError(
            "this Windows has no MessageBoxTimeoutW, so nothing can be approved"
        ) from missing
    timed_box.argtypes = (
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_ushort,
        ctypes.c_uint32,
    )
    timed_box.restype = ctypes.c_int
    return int(timed_box(None, body, title, style, 0, milliseconds))


_ = Callable  # re-exported types keep the import graph explicit for pyright

__all__ = [
    "Win32Screen",
    "WindowSlice",
    "compose_frame",
    "is_supported_platform",
    "parse_chord",
]
