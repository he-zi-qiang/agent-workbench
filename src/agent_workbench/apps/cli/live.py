"""Stage lines that move while a run is moving.

Written against plain ANSI rather than a TUI library. What this needs is one
line rewritten in place and finished lines left alone; a full-screen framework
would take over the scrollback, and the scrollback is where a terminal user
expects the last ten questions to still be.

Everything degrades when stdout is not a terminal. Redirected to a file or a
pipe there is no cursor to move and no colour worth writing, so the spinner is
dropped and each stage is written once, when it finishes -- which is also what
makes the output diffable in a test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, TextIO

#: Braille frames: one cell wide in every terminal font, unlike emoji.
SPINNER: Final[tuple[str, ...]] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

#: A settled step, and the line hanging under it that says what it produced.
STEP_MARK: Final[str] = "⏺"
DETAIL_MARK: Final[str] = "⎿"
FAIL_MARK: Final[str] = "✗"

_CLEAR_LINE: Final[str] = "\r\x1b[2K"
_DIM: Final[str] = "\x1b[2m"
#: The one colour this surface uses for anything it wants looked at.
ACCENT: Final[str] = "\x1b[38;5;215m"
_ACCENT = ACCENT
_GREEN: Final[str] = "\x1b[38;5;108m"
_RED: Final[str] = "\x1b[38;5;174m"
_RESET: Final[str] = "\x1b[0m"


def colour_enabled(stream: TextIO) -> bool:
    """Whether to write escape sequences at all.

    Decided from the stream alone. The obvious extra input is ``NO_COLOR``, and
    it is deliberately not read here: only bootstrap reads the process
    environment in this codebase, and a display detail is not worth routing
    through settings. ``--no-color`` turns it off explicitly instead.
    """

    return bool(getattr(stream, "isatty", lambda: False)())


def paint(text: str, colour: str, *, enabled: bool) -> str:
    return f"{colour}{text}{_RESET}" if enabled and text else text


def banner(title: str, subtitle: str, *, colour: bool) -> str:
    """The opening frame, sized to what it holds."""

    width = max(_width(title) + 2, _width(subtitle)) + 4
    top = "╭" + "─" * width + "╮"
    bottom = "╰" + "─" * width + "╯"
    mark = paint("✳", _ACCENT, enabled=colour)
    first = f"│ {mark} {title}{' ' * (width - _width(title) - 4)} │"
    second = f"│   {paint(subtitle, _DIM, enabled=colour)}"
    second += " " * (width - _width(subtitle) - 4) + " │"
    frame = paint(top, _DIM, enabled=colour), paint(bottom, _DIM, enabled=colour)
    return f"{frame[0]}\n{first}\n{second}\n{frame[1]}"


def _width(text: str) -> int:
    """Display columns, counting CJK as two.

    Without this the frame's right edge lands mid-character on any line with
    Chinese in it, which is every line here.
    """

    return sum(2 if _wide(char) else 1 for char in text)


def _wide(char: str) -> bool:
    code = ord(char)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
    )


@dataclass(slots=True)
class LiveStages:
    """Prints step lines, rewriting only the one still running.

    ``interactive`` is the whole switch: with a terminal the active line is
    redrawn in place and finished steps are left above it; without one nothing
    is redrawn and a step prints once, on completion.
    """

    stream: TextIO
    interactive: bool
    colour: bool = False
    _tick: int = 0
    _open_line: bool = False
    _printed: set[str] = field(default_factory=set[str])

    def active(self, title: str, note: str = "", elapsed: float = 0.0) -> None:
        """Redraw the line for the step currently running."""

        if not self.interactive:
            return
        frame = SPINNER[self._tick % len(SPINNER)]
        self._tick += 1
        head = paint(frame, _ACCENT, enabled=self.colour)
        tail = f"{note} · {elapsed:.0f}s" if note else f"{elapsed:.0f}s"
        line = f"{head} {title} {paint(f'({tail})', _DIM, enabled=self.colour)}"
        self._rewrite(line)

    def done(
        self, key: str, title: str, note: str = "", *, failed: bool = False
    ) -> None:
        """Settle a step. Printed once, however many times it is reported."""

        if key in self._printed:
            return
        self._printed.add(key)
        mark = FAIL_MARK if failed else STEP_MARK
        colour = _RED if failed else _GREEN
        head = paint(mark, colour, enabled=self.colour)
        self._commit(f"{head} {title}")
        if note:
            detail = paint(f"  {DETAIL_MARK}  {note}", _DIM, enabled=self.colour)
            self._commit(detail)

    def clear(self) -> None:
        """Drop a half-drawn active line, so what follows starts clean."""

        if self.interactive and self._open_line:
            self.stream.write(_CLEAR_LINE)
            self.stream.flush()
        self._open_line = False

    def _rewrite(self, line: str) -> None:
        if self._open_line:
            self.stream.write(_CLEAR_LINE)
        self.stream.write(line)
        self._open_line = True
        self.stream.flush()

    def _commit(self, line: str) -> None:
        if self.interactive and self._open_line:
            self.stream.write(_CLEAR_LINE)
        self.stream.write(line + "\n")
        self._open_line = False
        self.stream.flush()


__all__ = [
    "ACCENT",
    "DETAIL_MARK",
    "FAIL_MARK",
    "SPINNER",
    "STEP_MARK",
    "LiveStages",
    "banner",
    "colour_enabled",
    "paint",
]
