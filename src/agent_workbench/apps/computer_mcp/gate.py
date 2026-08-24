"""The thing that stands between a model and a screen.

Four checks, and the order is the argument (ADR-070):

1. **Is this application granted at all?** A session-scoped allowlist the
   person approved once, by name. Nothing outside it is touched or shown.
2. **Is it granted for *this* action?** The tier, from
   ``domain/computer.tier_for``.
3. **Is it still in front?** Re-read immediately before the action, never
   cached. A permission is about a window, and windows move.
4. **Was it still in front afterwards?** Only typing needs this, and typing is
   exactly the action that can be half-delivered.

Check 3 is the one that is easy to leave out and impossible to add later
without rewriting everything above it. A gate that decides once and then acts
has authorized the screen as it was, and by the time the keystroke lands the
screen is as it is.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import cast

from agent_workbench.apps.computer_mcp.consent import ask as ask_a_person
from agent_workbench.domain.computer import (
    ApplicationIdentity,
    ScreenshotBudget,
    ScreenTier,
    focus_lost,
    permits,
    refusal,
    tier_for,
)
from agent_workbench.ports.screen import Capture, ScreenPort

#: How a person is asked. Named here rather than spelled out at each use so the
#: server and the gate cannot drift into two slightly different callables.
ConsentAsker = Callable[..., Awaitable[bool]]


class ScreenRefusedError(RuntimeError):
    """The gate said no. Carries the whole message the model should read.

    An exception rather than a returned union because every caller does the
    same thing with it -- turns it into an error result -- and a union would
    put a branch in each of the ten tool handlers, nine of which would
    eventually forget it.
    """


@dataclass(frozen=True, slots=True)
class Grant:
    """One approved application, as the person approved it."""

    application: ApplicationIdentity
    tier: ScreenTier


@dataclass
class ScreenGate:
    """A session's grants, and the checks every action passes through."""

    screen: ScreenPort
    budget: ScreenshotBudget = field(default_factory=ScreenshotBudget)
    #: How a person is asked. Injected rather than called directly so a test
    #: can answer without a dialog, and so a deployment that has somewhere
    #: better to ask than a macOS alert can supply it. The default is the
    #: macOS one, because a server that quietly granted itself access when
    #: nobody wired an approver is exactly the state this replaced.
    consent: ConsentAsker = ask_a_person
    #: Keyed by bundle id, which is the identity an application cannot rename
    #: its way out of. An empty allowlist is the starting state and refuses
    #: everything -- there is no "allow by default" here to turn off.
    _granted: dict[str, Grant] = field(
        default_factory=lambda: cast(dict[str, Grant], {}), init=False
    )

    # --- granting --------------------------------------------------------

    async def grant(
        self, applications: tuple[ApplicationIdentity, ...], *, reason: str = ""
    ) -> tuple[Grant, ...]:
        """Ask a person, and record what they approved.

        The asking is the point, and until 2026-08-24 it was the one part of
        ADR-070 §2 that did not exist: this method took the model's own list,
        wrote it into the allowlist, and answered "approved for this session".
        Every other check was real -- the tier table, the frontmost re-check,
        the allowlist starting empty -- and all of them were guarding a consent
        nobody had given. A model that can grant itself access has an allowlist
        with one entry: whatever it just asked for.

        The tier is derived here rather than accepted from the caller. A
        request that could name its own tier is a request that can ask for
        "full" on a browser, and the person approving a list of application
        names is not reading a tier column.

        Denial writes nothing. Not even the applications the person might have
        been willing to approve individually -- the dialog is one decision
        about one set, so a partial grant would be a decision nobody made.
        """

        approved = await self.consent(applications, reason=reason)
        if not approved:
            raise ScreenRefusedError(
                "the person did not approve these applications, so none of "
                "them is available in this session.\n"
                "Do not ask again for the same list without a reason that "
                "answers why it was refused, and do not attempt to reach "
                "these applications another way -- never use AppleScript, "
                "System Events, shell commands, or any other method to send "
                "input to an application."
            )
        given = tuple(
            Grant(application=held, tier=tier_for(held)) for held in applications
        )
        for held in given:
            self._granted[held.application.bundle_id] = held
        return given

    def grants(self) -> tuple[Grant, ...]:
        return tuple(self._granted.values())

    # --- checking --------------------------------------------------------

    def _require_frontmost(self, action: str) -> Grant:
        """Checks 1, 2 and 3, in that order, against a fresh reading."""

        now = self.screen.frontmost()
        held = self._granted.get(now.bundle_id)
        if held is None:
            raise ScreenRefusedError(
                f'"{now.name or "an unidentified application"}" is not in this '
                "session's approved list, and the frontmost application is "
                f"what {action} would reach.\n"
                "Call request_access with the applications you need and wait "
                "for the person to approve them.\n"
                "Do not attempt to work around this restriction -- never use "
                "AppleScript, System Events, shell commands, or any other "
                "method to send input to an application."
            )
        # Re-derived from the live identity rather than read off the stored
        # grant: an application that was granted under one name and is now
        # reporting another is exactly the case the tier table exists for.
        tier = tier_for(now)
        if not permits(tier, action):
            raise ScreenRefusedError(refusal(action=action, application=now, tier=tier))
        return Grant(application=now, tier=tier)

    # --- acting ----------------------------------------------------------

    async def screenshot(self, display_id: int | None = None) -> Capture:
        """A capture of one display, inside the token budget, of the approved
        applications and nothing else.

        Not gated on the *tier*: seeing is what a grant is for, and every tier
        including "read" permits it. It is gated on the allowlist, and the
        gating is the frame itself -- an application nobody approved is not
        drawn into the picture.

        Until 2026-08-24 that last sentence was false in the direction that
        matters. The filter was phrased as "which applications to exclude",
        which needs the list of everything *running*; the port does not expose
        that, so the exclusion list was always empty and a capture with an
        empty allowlist returned the whole desktop (F-18).

        Inverting the question dissolves it. The gate does not need to know
        what is running -- it needs to say what is *approved*, which is the one
        thing it does know, and the adapter resolves that to windows inside
        itself. The model never learns what else was on screen, which was the
        objection to enumerating in the first place.
        """

        displays = self.screen.displays()
        if not displays:
            raise ScreenRefusedError("this machine reports no displays")
        chosen = next(
            (held for held in displays if held.display_id == display_id),
            displays[0],
        )
        included = self._to_include()
        if not included:
            # An empty allowlist is not "show everything", which is what it
            # used to mean here. It is "there is nothing you have been allowed
            # to look at".
            raise ScreenRefusedError(
                "no application has been approved in this session, so there "
                "is nothing to capture.\n"
                "Call request_access with the applications you need and wait "
                "for the person to approve them."
            )
        if "exclude_native" not in self.screen.capabilities():
            # `exclude_native` only, and the narrowing is the point. This check
            # used to accept `exclude_mask` as well -- draw the whole frame,
            # then paint over the rectangles -- which satisfies the letter of
            # "the model did not see it" only until a window moves between the
            # geometry read and the capture, or a rectangle is reported wrong,
            # or the mask is composited under something. The pixels existed.
            # A compositor filter means they never did (F-18's second half).
            raise ScreenRefusedError(
                "this platform cannot keep unapproved windows out of a capture "
                "at the compositor, and painting over them afterwards is not "
                "the same promise. No screenshot was taken."
            )
        width, height = self.budget.fit(chosen.width, chosen.height)
        return await self.screen.capture(
            chosen.display_id,
            width=width,
            height=height,
            include_bundle_ids=included,
        )

    def _to_include(self) -> tuple[str, ...]:
        """The approved bundle ids, which is exactly the allowlist.

        Sorted so two captures of the same session produce the same filter
        argument, which is what makes a difference between two frames mean
        something changed on screen rather than in a dict's iteration order.
        """

        return tuple(sorted(self._granted))

    async def click(
        self, x: int, y: int, *, button: str = "left", count: int = 1
    ) -> Grant:
        action = {
            "left": "left_click",
            "right": "right_click",
            "middle": "middle_click",
        }[button]
        if count == 2:
            action = "double_click"
        elif count == 3:
            action = "triple_click"
        held = self._require_frontmost(action)
        await self.screen.click(x, y, button=button, count=count)  # pyright: ignore[reportArgumentType]
        return held

    async def scroll(self, x: int, y: int, *, direction: str, amount: int) -> Grant:
        held = self._require_frontmost("scroll")
        await self.screen.scroll(x, y, direction=direction, amount=amount)  # pyright: ignore[reportArgumentType]
        return held

    async def key(self, combination: str) -> Grant:
        held = self._require_frontmost("key")
        await self.screen.key(combination)
        return held

    async def type_text(self, text: str) -> Grant:
        """Type, then check that the window it was typed into is still there.

        The check afterwards is not belt-and-braces. Keystrokes follow keyboard
        focus, so a window that comes forward mid-string takes the rest of the
        string with it -- and the two halves land in two applications, only one
        of which was approved. The adapter reports how much it delivered, and
        this refuses with that number rather than with "denied", because a
        model told only "denied" retypes the whole thing and the first half
        arrives twice.
        """

        held = self._require_frontmost("type")
        delivered = await self.screen.type_text(text)
        after = self.screen.frontmost()
        if after.bundle_id != held.application.bundle_id:
            raise ScreenRefusedError(
                focus_lost(
                    approved=held.application,
                    now_frontmost=after,
                    delivered=delivered,
                    total=len(text),
                )
            )
        if delivered < len(text):
            # Focus did not move but the adapter stopped anyway. Same message
            # shape, because the model's problem is identical: it does not know
            # how much is on screen.
            raise ScreenRefusedError(
                focus_lost(
                    approved=held.application,
                    now_frontmost=after,
                    delivered=delivered,
                    total=len(text),
                )
            )
        return held


__all__ = ["Grant", "ScreenGate", "ScreenRefusedError"]
