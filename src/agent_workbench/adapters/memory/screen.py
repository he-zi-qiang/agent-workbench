"""A screen that exists only in a list of recorded actions.

Every rule this project has about screens -- which tier an application is at,
how large a capture may be, what happens when focus moves mid-string -- is
decided above the port and can therefore be asserted here, against a double
that never touches a display. That is the whole reason the port exists
(ADR-070).

It is programmable in the two dimensions that matter and that a real screen
cannot be asked for: ``frontmost`` can be made to *change between calls*, which
is how the mid-delivery focus check is tested at all, and an activation can be
made **not to take**, which is how a case that depends on a real window server
declining -- a modal sheet, a full-screen space, an application that does not
come forward -- is exercised on every run (ADR-091).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from agent_workbench.domain.computer import ApplicationIdentity
from agent_workbench.ports.screen import Capture, Display, MouseButton, ScrollDirection

MAIN_DISPLAY = Display(
    display_id=1, width=1470, height=956, scale_factor=2.0, origin_x=0, origin_y=0
)

#: A second screen, arranged to the right of the main one and slightly higher.
#:
#: It exists so the one thing that cannot be observed on this project's machine
#: can still be asserted: a display whose origin is not (0, 0) (F-22, ADR-090).
#: Neither offset is a multiple of the other's dimensions, so a conversion that
#: happened to be right by symmetry -- or that swapped x for y -- fails rather
#: than passes.
SECOND_DISPLAY = Display(
    display_id=2,
    width=1920,
    height=1080,
    scale_factor=1.0,
    origin_x=1470,
    origin_y=-124,
)

#: The default focused application: something at tier "full", so a test that is
#: not about tiers does not have to set one up.
DEFAULT_FOCUS = ApplicationIdentity(bundle_id="com.apple.Notes", name="Notes")


@dataclass
class FakeScreen:
    """Records what it was asked to do, and answers what it was told to."""

    #: Who is in front. A callable is read afresh on every `frontmost()`, which
    #: is what lets a test move focus part-way through a `type_text`.
    focus: ApplicationIdentity | Callable[[], ApplicationIdentity] = DEFAULT_FOCUS
    screens: tuple[Display, ...] = (MAIN_DISPLAY,)
    supports: frozenset[str] = frozenset({"exclude_native"})
    #: `(action, details)` in order, which is what most assertions read.
    actions: list[tuple[str, object]] = field(
        default_factory=lambda: cast(list[tuple[str, object]], [])
    )
    #: How many characters `type_text` delivers before stopping, or None for
    #: all of them. Stands in for focus moving away mid-string.
    type_limit: int | None = None
    #: Which applications this fake machine has *running*, by identity rather
    #: than by bundle id: `activate` has to answer with one, and a name it
    #: invented would let a test pass while the real adapter echoed the model's
    #: own string back at it. Empty is a machine with nothing running, which is
    #: how "approved but not launched" is exercised.
    installed: tuple[ApplicationIdentity, ...] = ()
    #: Whether an activation actually reorders this fake screen. False stands
    #: in for the case the real one cannot be asked to produce on cue: the
    #: window server was asked, and something else is still in front.
    activation_lands: bool = True

    def displays(self) -> tuple[Display, ...]:
        return self.screens

    def frontmost(self) -> ApplicationIdentity:
        held = self.focus
        return held() if callable(held) else held

    def capabilities(self) -> frozenset[str]:
        return self.supports

    async def capture(
        self,
        display_id: int,
        *,
        width: int,
        height: int,
        include_bundle_ids: tuple[str, ...] = (),
    ) -> Capture:
        self.actions.append(
            ("capture", (display_id, width, height, include_bundle_ids))
        )
        display = next(
            (held for held in self.screens if held.display_id == display_id),
            self.screens[0],
        )
        return Capture(
            media_type="image/jpeg",
            # Not empty: a caller that base64-encodes this should produce
            # something, so a test can assert on the encoding rather than on a
            # special case for zero bytes.
            content=b"\xff\xd8\xff\xdb jpeg-ish",
            width=width,
            height=height,
            display=display,
        )

    async def activate(self, bundle_id: str) -> ApplicationIdentity | None:
        self.actions.append(("activate", bundle_id))
        found = next(
            (held for held in self.installed if held.bundle_id == bundle_id), None
        )
        if found is None:
            return None
        if self.activation_lands:
            self.focus = found
        return self.frontmost()

    async def click(
        self, x: int, y: int, *, button: MouseButton = "left", count: int = 1
    ) -> None:
        self.actions.append(("click", (x, y, button, count)))

    async def scroll(
        self, x: int, y: int, *, direction: ScrollDirection, amount: int
    ) -> None:
        self.actions.append(("scroll", (x, y, direction, amount)))

    async def type_text(self, text: str) -> int:
        delivered = (
            len(text) if self.type_limit is None else min(self.type_limit, len(text))
        )
        self.actions.append(("type", text[:delivered]))
        return delivered

    async def key(self, combination: str) -> None:
        self.actions.append(("key", combination))


__all__ = ["DEFAULT_FOCUS", "MAIN_DISPLAY", "SECOND_DISPLAY", "FakeScreen"]
