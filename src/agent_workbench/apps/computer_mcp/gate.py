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

Since ADR-091 one method -- ``activate`` -- **changes** the answer to check 3
instead of being judged by it, and that is a different kind of thing to have
here. It is bounded by a second question nobody had to ask before: not just
"is the target approved", but "is what is in front *right now* approved too".
The first makes the model's choice a choice among windows a person agreed to;
the second keeps it from taking the screen away from the window that person is
actually using.

Between check 3 and the action there is one more step, and it is deliberately
**not** called a check: the point a model measured on a picture of one display
is converted into the global space the event is posted into (ADR-090). It
refuses a coordinate that is not on that display at all -- which is a mistake,
not a permission problem, and is answered as one. It runs *after* the three
checks rather than before them, so a session that has been granted nothing
cannot use a coordinate refusal to measure this machine's screens.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from agent_workbench.apps.computer_mcp.consent import ask as ask_a_person
from agent_workbench.domain.computer import (
    ApplicationIdentity,
    ScreenshotBudget,
    ScreenTier,
    activation_did_not_take,
    activation_needs_a_grant,
    activation_would_take_the_screen,
    application_is_not_running,
    focus_lost,
    frontmost_is_not_approved,
    off_frame,
    permits,
    refusal,
    tier_for,
)
from agent_workbench.ports.screen import Capture, Display, ScreenPort

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


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Grant:
    """One approved application, as the person approved it."""

    application: ApplicationIdentity
    tier: ScreenTier


@dataclass(frozen=True, slots=True)
class ScreenAction:
    """One attempt, and what the gate decided about it.

    **The person's record, not the model's.** It names the application that was
    in front even when nobody approved it -- which is the thing every refusal a
    model reads goes to lengths to withhold (ADR-095 §2). The two are consistent
    because the reader is: this is reachable only through the read path a
    console on this machine uses, and that console is being looked at by the
    person whose screen it describes.

    ``reason`` is the **first line** of the refusal the model was given, not a
    category invented here. The refusals in ``domain/computer.py`` are written
    in three parts whose first is "what was refused and why", so the sentence
    already exists and is already curated. Tagging each of the fifteen raise
    sites with a code instead would put the categorisation in fifteen places and
    make the panel's vocabulary drift from the model's -- and the panel would
    still be showing a worse sentence than the one already written.
    """

    at: datetime
    action: str
    #: What was in front when the gate decided. ``None`` only where the gate
    #: never got as far as reading it.
    application: ApplicationIdentity | None
    allowed: bool
    #: Empty when allowed.
    reason: str
    #: Anything the row is unreadable without -- how much of a string was
    #: delivered, which display a point was measured on. Empty when there is
    #: nothing to add.
    detail: str


@dataclass(slots=True)
class _Note:
    """What the body of one attempt tells the recorder about itself.

    Mutable and short-lived, because the two facts worth recording are only
    known *inside* the attempt: which application the live read found (check 3
    reads it, and a refusal there is exactly where the panel most wants it), and
    how much of a string got delivered.
    """

    application: ApplicationIdentity | None = None
    detail: str = ""


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
    #: Where "when" comes from. Injected for the same reason `consent` is: a
    #: test that had to wait for the wall clock to move would be a slow test
    #: about the wrong thing.
    clock: Callable[[], datetime] = _utc_now
    #: How many attempts are kept.
    #:
    #: Bounded because this hangs on a long-lived process. An unbounded list of
    #: actions is, on a machine left running for a day, a structure that only
    #: grows and that records which windows this person used and when -- which
    #: is a different object from "the last few things the task did", and not
    #: one anybody asked for.
    history_limit: int = 200
    #: Attempts, oldest first. Same lifetime as `_granted`, and deliberately:
    #: ADR-070 refused to persist the grants because an authorization that
    #: outlives the person watching the screen is a different authorization. A
    #: log of what was done to that screen, kept across restarts, is the same
    #: object under another name -- so it lives here, in memory, and goes when
    #: the process does.
    _history: deque[ScreenAction] = field(
        default_factory=lambda: cast("deque[ScreenAction]", deque()), init=False
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

    # --- recording ---------------------------------------------------------

    @contextmanager
    def _record(self, action: str) -> Generator[_Note]:
        """Record one attempt, whatever it turns into.

        Wraps the whole public method rather than sitting inside
        ``_require_frontmost``, because checks 1--3 are not the only way an
        attempt ends: a coordinate can be off the display it named, and a string
        can be half delivered after the gate had already said yes. An entry
        written at the gate would call both of those "allowed".

        Re-raises unchanged. This observes; it decides nothing.
        """

        note = _Note()
        try:
            yield note
        except ScreenRefusedError as refused:
            # The refusal's own first sentence. Those messages are written in
            # three parts whose first is "what was refused and why"; taking the
            # first line keeps the panel's words and the model's words the same
            # words, which a category invented here could not.
            self._remember(
                action, note, allowed=False, reason=str(refused).split("\n")[0]
            )
            raise
        self._remember(action, note, allowed=True, reason="")

    def _remember(
        self, action: str, note: _Note, *, allowed: bool, reason: str
    ) -> None:
        self._history.append(
            ScreenAction(
                at=self.clock(),
                action=action,
                application=note.application,
                allowed=allowed,
                reason=reason,
                detail=note.detail,
            )
        )
        # Trimmed here rather than by giving the deque a maxlen, so the bound is
        # the field a reader can see and change rather than an argument buried
        # in a default_factory.
        while len(self._history) > self.history_limit:
            self._history.popleft()

    def actions(self) -> tuple[ScreenAction, ...]:
        """What has been attempted, oldest first.

        Not sorted and not filtered: the order attempts were made in is the
        order they are worth reading in, and a panel that showed only the
        refusals would answer "did anything go wrong" while hiding "what has
        this been doing".
        """

        return tuple(self._history)

    def grants(self) -> tuple[Grant, ...]:
        """Everything approved in this session, as the person approved it.

        Sorted by nothing: insertion order is grant order, which is the order
        the person read them in.
        """

        return tuple(self._granted.values())

    def frontmost_grant(self) -> Grant | None:
        """The approved application in front right now, or ``None``.

        ``None`` covers two situations the caller is deliberately not allowed
        to tell apart: something unapproved is frontmost, or nothing is. Both
        mean the same thing to a model -- the next action will be refused by
        check 3 -- and distinguishing them would require naming a window
        nobody approved, which is the reading the allowlist exists to prevent.

        Live, like every other reading of the front of the screen, so an
        answer is about the moment it was asked and not the moment it is used.
        """

        now = self.screen.frontmost()
        if now.bundle_id not in self._granted:
            return None
        # Re-derived from the live identity, for the same reason
        # `_require_frontmost` does it: the stored grant records what a person
        # approved, and what is on screen is what an action would reach.
        return Grant(application=now, tier=tier_for(now))

    # --- checking --------------------------------------------------------

    def _require_frontmost(self, action: str, note: _Note | None = None) -> Grant:
        """Checks 1, 2 and 3, in that order, against a fresh reading.

        ``note`` is how the live reading reaches the person's record. It is
        filled *before* the checks run, so a refusal by check 1 -- the case
        where the panel most wants the name, because that is the one where the
        model is told nothing -- still records which window was in front.
        """

        now = self.screen.frontmost()
        if note is not None:
            note.application = now
        held = self._granted.get(now.bundle_id)
        if held is None:
            # Composed in `domain/computer.py` like the other seven, and it was
            # the only one still spelled out here. That is not tidying: the
            # argument for *not naming what is in front* lives in that module's
            # docstrings, and a refusal written somewhere else is a refusal
            # nobody checks against them -- which is exactly how this one came
            # to be the third path where the rule did not hold (ADR-095 §1).
            raise ScreenRefusedError(frontmost_is_not_approved(action=action))
        # Re-derived from the live identity rather than read off the stored
        # grant: an application that was granted under one name and is now
        # reporting another is exactly the case the tier table exists for.
        tier = tier_for(now)
        if not permits(tier, action):
            raise ScreenRefusedError(refusal(action=action, application=now, tier=tier))
        return Grant(application=now, tier=tier)

    def _display_for(self, display_id: int | None, *, must_name: bool) -> Display:
        """The display a call is about.

        ``must_name`` is the difference between looking and landing, and it is
        the only difference: a screenshot has no coordinate to get wrong -- it
        *reports* which display it took, and that report is where a later click
        gets the id it sends back -- so requiring one there would leave no way
        to learn the ids at all. A coordinate has everything to get wrong.

        Given that, three answers, and the middle one is worth arguing about.

        **One display, no id given:** the main one. A single-screen session is
        the ordinary case and should not have to learn this vocabulary to
        click; there is also exactly one right answer, so nothing is guessed.

        **More than one display, no id given, and a coordinate rides on it:
        refused.** This is the part that makes the rest of ADR-090 worth
        anything. Converting correctly once told which screen a point came from
        still leaves the failure intact when the model simply does not say: a
        coordinate measured on the second display that happens to fall inside
        the main one's bounds would be accepted and clicked, in the wrong
        place, silently -- F-22 again, reached by omission rather than by
        arithmetic. So on a machine where the two spaces actually differ,
        saying which one is mandatory.

        **An id no display answers to: refused.** Falling back to the main
        display here would be the same bug wearing a default.
        """

        displays = self.screen.displays()
        if not displays:
            raise ScreenRefusedError("this machine reports no displays")
        # Read before the count is tested rather than after: `len(...) > 1`
        # narrows this tuple to "empty or one", and pyright then reads the
        # index in the `return` below as out of range on the empty half.
        main = displays[0]
        if display_id is None:
            if must_name and len(displays) > 1:
                raise ScreenRefusedError(
                    f"this machine has {len(displays)} displays, so a "
                    "coordinate has to say which one it was measured on. "
                    f"Attached: {self._attached(displays)}.\n"
                    "Take a screenshot and pass back the display_id it "
                    "reports."
                )
            return main
        chosen = next(
            (held for held in displays if held.display_id == display_id), None
        )
        if chosen is None:
            raise ScreenRefusedError(
                f"this machine has no display {display_id}. Attached: "
                f"{self._attached(displays)}.\n"
                "Take a screenshot and use the display_id it reports."
            )
        return chosen

    @staticmethod
    def _attached(displays: tuple[Display, ...]) -> str:
        """The displays, named the way a model has to name one back."""

        return ", ".join(
            f"{held.display_id} ({held.width}x{held.height} points)"
            for held in displays
        )

    def _landing(self, x: int, y: int, display_id: int | None) -> tuple[int, int]:
        """Where a display-local point is, in the space events are posted in.

        Called after :meth:`_require_frontmost` and never before it. A session
        that has been granted nothing should be told that, not told how wide
        this machine's screens are: the arrangement of somebody's monitors is a
        fact about their desk, and a refusal is not a place to hand it out.
        """

        frame = self._display_for(display_id, must_name=True).frame()
        if not frame.contains(x, y):
            raise ScreenRefusedError(off_frame(x=x, y=y, frame=frame))
        return frame.to_global(x, y)

    # --- looking, and choosing what to look at ---------------------------

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

        # `must_name=False`: looking at an unnamed display is the main one
        # even on a machine with several, because this call is where the ids
        # come from. Naming one that does not exist is still refused -- a
        # picture of a different screen than the one asked for is the same
        # class of silent substitution this whole change is about.
        with self._record("screenshot") as note:
            return await self._screenshot(display_id, note)

    async def _screenshot(self, display_id: int | None, note: _Note) -> Capture:
        chosen = self._display_for(display_id, must_name=False)
        note.detail = f"display {chosen.display_id}"
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

    async def activate(self, bundle_id: str) -> Grant:
        """Bring an approved application to the front.

        The one tool that changes the answer to check 3 rather than being
        judged by it, which is why it needed its own ADR (ADR-091). Before it
        existed, "which window is in front" was a fact about what the person
        had chosen and the gate only ever read it. After it, the model picks --
        and so the question check 3 answers changes from *"did the person put
        this window in front"* to *"is this window one of the ones the person
        approved"*.

        Two checks, and the second is the narrowing that makes the first one
        safe to grant:

        1. **The target is approved.** Same allowlist, same starting state of
           empty. Activation is not tier-gated, for the same reason a
           screenshot is not: it synthesizes no input, and everything that
           happens afterwards is gated again against whatever is frontmost
           then. Bringing a browser forward buys exactly the ability to look
           at it.
        2. **Whatever is frontmost right now is also approved.** Without this
           the tool takes the screen away from somebody: a person who has
           switched to their mail to read something would have the window
           pulled out from under them by a task that was told about neither
           the mail nor the switch. Rearranging *within* the set a person
           approved is a choice they delegated; taking the screen back from
           the window they are actually using is not.

        There is no check 4 here, and the absence is deliberate. Typing needs
        one because it can be half-delivered; an activation either took or did
        not, and this reports which -- from the identity the port hands back
        after its own bounded wait, not from a second read that would race it.
        """

        with self._record("activate") as note:
            # The target, not the incumbent: this row is about the window the
            # task asked for. Which window it was taken *from* is on the rows
            # above it, where that reading was actually made.
            note.detail = bundle_id
            return await self._activate(bundle_id, note)

    async def _activate(self, bundle_id: str, note: _Note) -> Grant:
        held = self._granted.get(bundle_id)
        if held is None:
            raise ScreenRefusedError(activation_needs_a_grant(bundle_id=bundle_id))
        incumbent = self.frontmost_grant()
        if incumbent is None:
            # Read for the record even though the refusal withholds it. The
            # model is told only that something unapproved is in front; the
            # person is told which, because it is the window they are sitting
            # in and the one they would have to approve to get past this
            # (ADR-095 §2). Two readers, two answers, one reading.
            note.application = self.screen.frontmost()
            raise ScreenRefusedError(
                activation_would_take_the_screen(target=held.application)
            )
        note.application = incumbent.application
        after = await self.screen.activate(bundle_id)
        if after is None:
            raise ScreenRefusedError(
                application_is_not_running(target=held.application)
            )
        if after.bundle_id != bundle_id:
            note.application = after
            raise ScreenRefusedError(
                activation_did_not_take(target=held.application, now_frontmost=after)
            )
        note.application = after
        # From the live identity, like every other answer this gate gives about
        # the front of the screen: an application that was approved under one
        # name and now reports another is exactly what the tier table is for.
        return Grant(application=after, tier=tier_for(after))

    # --- acting ----------------------------------------------------------

    async def click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        count: int = 1,
        display_id: int | None = None,
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
        with self._record(action) as note:
            held = self._require_frontmost(action, note)
            at_x, at_y = self._landing(x, y, display_id)
            note.detail = f"({at_x}, {at_y})"
            await self.screen.click(at_x, at_y, button=button, count=count)  # pyright: ignore[reportArgumentType]
            return held

    async def scroll(
        self,
        x: int,
        y: int,
        *,
        direction: str,
        amount: int,
        display_id: int | None = None,
    ) -> Grant:
        with self._record("scroll") as note:
            held = self._require_frontmost("scroll", note)
            at_x, at_y = self._landing(x, y, display_id)
            note.detail = f"({at_x}, {at_y}) {direction}"
            await self.screen.scroll(at_x, at_y, direction=direction, amount=amount)  # pyright: ignore[reportArgumentType]
            return held

    async def key(self, combination: str) -> Grant:
        with self._record("key") as note:
            held = self._require_frontmost("key", note)
            # The combination, not the text: a chord is a command, and it is
            # what makes this row readable. `type` deliberately records only a
            # count -- see there.
            note.detail = combination
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

        with self._record("type") as note:
            held = self._require_frontmost("type", note)
            delivered = await self.screen.type_text(text)
            # How much, never what. The panel's reader is the person whose
            # keyboard this is, but the string itself is the one thing on this
            # row that could be a password -- and "7/23 characters" answers the
            # question the row exists for without carrying it.
            note.detail = f"{delivered}/{len(text)} characters"
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


__all__ = ["Grant", "ScreenAction", "ScreenGate", "ScreenRefusedError"]
