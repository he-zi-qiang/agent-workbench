"""The seam between deciding what may be done to a screen and doing it.

Everything above this line is arithmetic and policy (``domain/computer.py``)
and runs anywhere. Everything below it is one operating system's idea of a
window, a cursor and a key code, and runs on exactly one.

The split is not tidiness. It is what makes the gate testable: every rule about
tiers, budgets and focus can be exercised against a programmable double, so the
assertions are about the rules rather than about whether a click landed. A
project whose screen policy could only be tested by moving a real mouse would
have a screen policy nobody tests.

Coordinates are **points, never pixels**, always. A retina display reports half
the pixels it has; a screenshot may be scaled down again to fit a token budget.
Two conversions, either of which silently doubles or halves a click. The port
therefore refuses to speak pixels at all -- the adapter converts once, at the
edge, and everything above works in points.

**Points in which space** is the other half of that same question, and it went
unstated until ADR-090 -- which is not a documentation gap, because two
different answers were already in the code. ``capture`` is of one display and
the image it returns is measured from *that display's* top-left; ``click``,
``move`` and ``scroll`` post events into the **global** space spanning every
attached display, where only the main screen's top-left is the origin. One
monitor makes those the same space and the disagreement invisible; a second one
makes every coordinate read off its screenshot name a point on the main screen
instead (F-22).

So the port states the split instead of leaving each side to assume one. A
``Display`` carries its own origin, the gate converts a display-local point
exactly once using it, and **everything below this line is given global points
only**.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from agent_workbench.domain.computer import ApplicationIdentity, DisplayFrame

MouseButton = Literal["left", "right", "middle"]
ScrollDirection = Literal["up", "down", "left", "right"]


class ScreenUnavailableError(RuntimeError):
    """This process cannot reach a screen, and why.

    Raised at construction rather than on first use, so a deployment without a
    display, without the optional dependency, or without the operating
    system's permission finds out at startup instead of halfway through a turn.
    """


@dataclass(frozen=True, slots=True)
class Display:
    """One screen, in points, and where it sits among the others."""

    display_id: int
    width: int
    height: int
    #: Points to pixels. 2.0 on a retina panel. Carried so a caller can reason
    #: about how much detail a capture can actually hold, never so it can do
    #: coordinate arithmetic -- see the module note.
    scale_factor: float
    #: This display's top-left in the global point space, which is the space
    #: every synthesized event is posted into.
    #:
    #: **No default, deliberately.** (0, 0) is right for the main display and
    #: wrong for every other one, so a default would let an implementation that
    #: never considered the question answer it correctly on the machine it was
    #: written on and incorrectly everywhere else. That is the exact shape F-22
    #: had for a year.
    origin_x: int
    #: Signed, like ``origin_x``: a screen arranged above or to the left of the
    #: main one has a negative origin, and that arrangement is ordinary.
    origin_y: int

    def frame(self) -> DisplayFrame:
        """This display as the arithmetic in ``domain/computer.py`` wants it.

        A method rather than a field so there is one description of this
        display and not two that can disagree.
        """

        return DisplayFrame(
            display_id=self.display_id,
            origin_x=self.origin_x,
            origin_y=self.origin_y,
            width=self.width,
            height=self.height,
        )


@dataclass(frozen=True, slots=True)
class Capture:
    """One screenshot, already encoded.

    ``width`` and ``height`` describe *this image*, which is generally smaller
    than the display it came from -- the budget cut it. ``display`` says what
    it was cut from, and the pair is what lets a caller state the ratio rather
    than have every reader guess it.
    """

    media_type: str
    content: bytes
    width: int
    height: int
    display: Display


@runtime_checkable
class ScreenPort(Protocol):
    """Look at a screen, and act on one."""

    def displays(self) -> tuple[Display, ...]:
        """Every attached display. The first is the main one."""
        ...

    def frontmost(self) -> ApplicationIdentity:
        """Which application has focus *right now*.

        Called before every action and again during a long one, never cached.
        The whole tier model rests on this being a fresh reading: a cached
        answer is a permission granted for a window that is no longer there.
        """
        ...

    async def capture(
        self,
        display_id: int,
        *,
        width: int,
        height: int,
        include_bundle_ids: tuple[str, ...] = (),
    ) -> Capture:
        """A screenshot of one display, scaled to ``width`` x ``height``,
        containing the named applications and nothing else.

        ``include_bundle_ids`` is an **allowlist**, and the direction is the
        security decision rather than a matter of taste. Phrased as an exclude
        list, the caller has to name everything that must not appear -- which
        means knowing everything that is running, and being wrong in the
        direction that leaks. It is also unwriteable: a window owned by
        WindowServer reports an empty bundle id and cannot be named at all,
        while it is on screen the whole time. An allowlist has to name only
        what the person approved, and anything it fails to name is left out.

        Resolving those ids to windows belongs to the implementation. A caller
        that had to do it would need the list of running applications, which is
        a strictly more interesting capability than the one it is being handed.

        An implementation must filter before the frame is composed, so an
        unapproved window is never rendered. One that can only paint over
        regions afterwards must not claim ``exclude_native``: the two are
        different promises, and only the first survives a window moving
        between the geometry read and the shutter.
        """
        ...

    def capabilities(self) -> frozenset[str]:
        """What this implementation can actually do.

        ``"exclude_native"`` -- unapproved windows are filtered before the
        frame is composed, so their pixels never existed. ``"exclude_mask"`` --
        they are painted over afterwards, so their pixels existed and were
        covered. The gate accepts only the first and refuses a capture
        otherwise; the second is kept in this vocabulary so an implementation
        can say which of the two it is rather than having to lie by omission.
        """
        ...

    async def click(
        self, x: int, y: int, *, button: MouseButton = "left", count: int = 1
    ) -> None:
        """Click at a **global** point. See the module note on spaces."""
        ...

    async def move(self, x: int, y: int) -> None:
        """Move the cursor to a **global** point."""
        ...

    async def scroll(
        self, x: int, y: int, *, direction: ScrollDirection, amount: int
    ) -> None:
        """Scroll at a **global** point."""
        ...

    async def type_text(self, text: str) -> int:
        """Type ``text``, returning how many characters were delivered.

        The return value is not decoration. Typing is the one action that can
        be interrupted halfway -- keystrokes follow keyboard focus, and focus
        can move mid-string -- so the caller has to be able to say *how much*
        arrived. An implementation that stops early returns the count it
        reached rather than raising, because what was delivered was delivered.
        """
        ...

    async def key(self, combination: str) -> None:
        """Press one chord, e.g. ``"cmd+shift+4"`` or ``"Return"``."""
        ...


__all__ = [
    "Capture",
    "Display",
    "MouseButton",
    "ScreenPort",
    "ScreenUnavailableError",
    "ScrollDirection",
]
