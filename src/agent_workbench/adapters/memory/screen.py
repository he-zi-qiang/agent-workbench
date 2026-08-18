"""A screen that exists only in a list of recorded actions.

Every rule this project has about screens -- which tier an application is at,
how large a capture may be, what happens when focus moves mid-string -- is
decided above the port and can therefore be asserted here, against a double
that never touches a display. That is the whole reason the port exists
(ADR-070).

It is programmable in the one dimension that matters and that a real screen
cannot be asked for: ``frontmost`` can be made to *change between calls*, which
is how the mid-delivery focus check is tested at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from agent_workbench.domain.computer import ApplicationIdentity
from agent_workbench.ports.screen import Capture, Display, MouseButton, ScrollDirection

MAIN_DISPLAY = Display(display_id=1, width=1470, height=956, scale_factor=2.0)

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
        exclude_bundle_ids: tuple[str, ...] = (),
    ) -> Capture:
        self.actions.append(
            ("capture", (display_id, width, height, exclude_bundle_ids))
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

    async def click(
        self, x: int, y: int, *, button: MouseButton = "left", count: int = 1
    ) -> None:
        self.actions.append(("click", (x, y, button, count)))

    async def move(self, x: int, y: int) -> None:
        self.actions.append(("move", (x, y)))

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


__all__ = ["DEFAULT_FOCUS", "MAIN_DISPLAY", "FakeScreen"]
