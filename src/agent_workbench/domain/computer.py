"""What a model may do to a screen, and how much of one it may be shown.

Four questions live here, and all four are answered without touching a screen
so that all four are testable without one (ADR-070, ADR-090):

* **Which tier is an application at.** Not every window is equally safe to type
  into. A browser is a place credentials get entered; a terminal is a place a
  keystroke runs a command. The answer is a function of the application alone,
  with no exceptions branch -- an exception here is a way past the gate.
* **How large a screenshot may be.** An image is tokens, and a screen is far
  more of them than a turn can afford. The budget is arithmetic, not a
  judgement call.
* **What a refusal says.** A refusal that only says "no" gets worked around;
  the model tries AppleScript next. So a refusal here carries three parts, and
  the third one is the part that matters. The activation refusals added by
  ADR-091 are the same shape with one deliberate omission: the one that fires
  because an *unapproved* window is in front does not name it, because a
  refusal that named it would be a way to read what the person is doing.
* **Where a coordinate lands.** A model measures a point off a picture of one
  display; the event it asks for is posted into one space spanning all of them.
  On a one-screen machine those are the same space, which is why the difference
  survived a year unnoticed (F-22). It is arithmetic, so it lives here rather
  than in the one module that cannot be tested.

Nothing in this module knows what a CGEvent is, and that is the point: the
platform adapter can be replaced or absent, and every rule above still holds
and is still under test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

#: What a granted application may have done to it.
#:
#: Ordered by increasing capability, and every value is a *ceiling*: a tier
#: never grants something a lower tier forbids.
ScreenTier = Literal["read", "click", "full"]

#: What an application is, for the purpose of deciding its tier.
#:
#: Three values rather than a bundle-id allowlist per tier, because the
#: dangerous property is the *kind* of program, not the specific program: a
#: browser nobody has heard of is still a place passwords are typed.
ApplicationKind = Literal["browser", "trading", "terminal", "shell", "other"]

#: Bundle identifiers, matched exactly. The strongest signal available: an
#: application cannot change it without being a different application to the
#: operating system.
_KIND_BY_BUNDLE_ID: Final[dict[str, ApplicationKind]] = {
    # Browsers. Tier "read": visible in a screenshot, never clicked or typed
    # into. Anything a browser is *for* -- signing in, paying, submitting a
    # form -- is the class of action this project will not synthesize.
    "com.apple.Safari": "browser",
    "com.apple.SafariTechnologyPreview": "browser",
    "com.google.Chrome": "browser",
    "com.google.Chrome.canary": "browser",
    "com.google.Chrome.beta": "browser",
    "com.microsoft.edgemac": "browser",
    "org.mozilla.firefox": "browser",
    "org.mozilla.firefoxdeveloperedition": "browser",
    "company.thebrowser.Browser": "browser",
    "com.brave.Browser": "browser",
    "com.vivaldi.Vivaldi": "browser",
    "com.operasoftware.Opera": "browser",
    "com.kagi.kagimacOS": "browser",
    # Money. Same tier as a browser and for a sharper version of the same
    # reason: a misplaced click here is not a wrong page, it is an order.
    "com.charlesschwab.Schwab": "trading",
    "com.fidelity.fidelity": "trading",
    "com.robinhood.Robinhood": "trading",
    "com.if.Amoeba": "trading",
    "com.coinbase.Coinbase": "trading",
    "com.binance.desktop": "trading",
    "com.ledger.live": "trading",
    "com.trezor.suite": "trading",
    "com.electrum.electrum": "trading",
    # Terminals. Tier "click": a Run button may be pressed, output may be
    # scrolled, and nothing may be typed -- because this project has a Bash
    # tool that runs commands through a sandbox, a policy gateway and an audit
    # trail, and a keystroke sent to a terminal window has none of those.
    "com.apple.Terminal": "terminal",
    "com.googlecode.iterm2": "terminal",
    "com.github.wez.wezterm": "terminal",
    "com.mitchellh.ghostty": "terminal",
    "net.kovidgoyal.kitty": "terminal",
    "co.zeit.hyper": "terminal",
    "dev.warp.Warp-Stable": "terminal",
    "com.tabby.app": "terminal",
    # IDEs, which contain terminals and are otherwise the same argument.
    "com.microsoft.VSCode": "shell",
    "com.microsoft.VSCodeInsiders": "shell",
    "com.vscodium": "shell",
    "com.todesktop.230313mzl4w4u92": "shell",  # Cursor
    "com.exafunction.windsurf": "shell",
    "com.jetbrains.intellij": "shell",
    "com.jetbrains.intellij.ce": "shell",
    "com.jetbrains.pycharm": "shell",
    "com.jetbrains.WebStorm": "shell",
    "com.jetbrains.goland": "shell",
    "com.jetbrains.rider": "shell",
    "com.apple.dt.Xcode": "shell",
    "dev.zed.Zed": "shell",
    "com.sublimetext.4": "shell",
    "com.neovide.neovide": "shell",
}

#: Windows executables, matched exactly against the lower-cased file name of
#: the process that owns the foreground window (ADR-0108).
#:
#: The same table as the one above, for the other platform's idea of an exact
#: identity. Windows has no bundle id; what a process cannot rename its way
#: out of is the file it was started from, and `adapters/screen/win32.py`
#: reports that file's name, lower-cased, as the identity. Kept as a second
#: dictionary rather than folded into the first so a reader auditing "what
#: does this project refuse to type into" finds one list per platform, and
#: so a bundle id and an executable name can never be confused for one
#: another in a diff.
#:
#: Packaged (Store) applications are hosted by `applicationframehost.exe`;
#: the adapter looks through that frame to the process inside it, so the
#: name that reaches this table is the application's own.
_KIND_BY_EXECUTABLE: Final[dict[str, ApplicationKind]] = {
    # Browsers, tier "read".
    "chrome.exe": "browser",
    "msedge.exe": "browser",
    "msedgewebview2.exe": "browser",
    "firefox.exe": "browser",
    "brave.exe": "browser",
    "vivaldi.exe": "browser",
    "opera.exe": "browser",
    "opera_gx.exe": "browser",
    "chromium.exe": "browser",
    "iexplore.exe": "browser",
    "tor.exe": "browser",
    "arc.exe": "browser",
    # Money, tier "read".
    "ledger live.exe": "trading",
    "trezor suite.exe": "trading",
    "electrum.exe": "trading",
    "coinbase.exe": "trading",
    "binance.exe": "trading",
    "thinkorswim.exe": "trading",
    "tws.exe": "trading",  # Interactive Brokers Trader Workstation
    # Terminals, tier "click". `conhost.exe` is the console host every
    # classic console window lives in; `openconsole.exe` is Windows
    # Terminal's copy of it.
    "windowsterminal.exe": "terminal",
    "wt.exe": "terminal",
    "cmd.exe": "terminal",
    "conhost.exe": "terminal",
    "openconsole.exe": "terminal",
    "powershell.exe": "terminal",
    "powershell_ise.exe": "shell",
    "pwsh.exe": "terminal",
    "mintty.exe": "terminal",
    "alacritty.exe": "terminal",
    "wezterm-gui.exe": "terminal",
    "hyper.exe": "terminal",
    "tabby.exe": "terminal",
    "conemu64.exe": "terminal",
    "cmder.exe": "terminal",
    "putty.exe": "terminal",
    # IDEs, tier "click", the same argument as on macOS.
    "code.exe": "shell",
    "code - insiders.exe": "shell",
    "vscodium.exe": "shell",
    "cursor.exe": "shell",
    "windsurf.exe": "shell",
    "idea64.exe": "shell",
    "pycharm64.exe": "shell",
    "webstorm64.exe": "shell",
    "goland64.exe": "shell",
    "rider64.exe": "shell",
    "clion64.exe": "shell",
    "studio64.exe": "shell",  # Android Studio
    "devenv.exe": "shell",  # Visual Studio
    "zed.exe": "shell",
    "sublime_text.exe": "shell",
    "neovide.exe": "shell",
    "emacs.exe": "shell",
}

#: Lower-cased substrings of an application's *name*, tried when the bundle id
#: is unknown.
#:
#: A second, weaker signal, and it exists because the first one cannot be
#: complete: a browser built last week has a bundle id nobody has written down,
#: and defaulting it to "full" would make an unknown browser more dangerous
#: than a known one. Substrings are checked longest-first so that "chrome
#: remote desktop" cannot be decided by "chrome".
_KIND_BY_NAME_SUBSTRING: Final[tuple[tuple[str, ApplicationKind], ...]] = tuple(
    sorted(
        (
            ("safari", "browser"),
            ("chrome", "browser"),
            ("chromium", "browser"),
            ("firefox", "browser"),
            ("edge", "browser"),
            ("brave", "browser"),
            ("vivaldi", "browser"),
            ("opera", "browser"),
            ("browser", "browser"),
            ("tor browser", "browser"),
            ("schwab", "trading"),
            ("fidelity", "trading"),
            ("robinhood", "trading"),
            ("e*trade", "trading"),
            ("etrade", "trading"),
            ("interactive brokers", "trading"),
            ("thinkorswim", "trading"),
            ("coinbase", "trading"),
            ("binance", "trading"),
            ("kraken", "trading"),
            ("ledger live", "trading"),
            ("trezor", "trading"),
            ("metamask", "trading"),
            ("electrum", "trading"),
            ("wallet", "trading"),
            ("terminal", "terminal"),
            ("iterm", "terminal"),
            ("ghostty", "terminal"),
            ("wezterm", "terminal"),
            ("kitty", "terminal"),
            ("alacritty", "terminal"),
            ("hyper", "terminal"),
            ("warp", "terminal"),
            ("tabby", "terminal"),
            ("console", "terminal"),
            ("powershell", "terminal"),
            ("command prompt", "terminal"),
            ("visual studio", "shell"),
            ("visual studio code", "shell"),
            ("vscode", "shell"),
            ("vscodium", "shell"),
            ("cursor", "shell"),
            ("windsurf", "shell"),
            ("intellij", "shell"),
            ("pycharm", "shell"),
            ("webstorm", "shell"),
            ("goland", "shell"),
            ("rider", "shell"),
            ("android studio", "shell"),
            ("xcode", "shell"),
            ("zed", "shell"),
            ("sublime text", "shell"),
            ("neovide", "shell"),
            ("emacs", "shell"),
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


@dataclass(frozen=True, slots=True)
class ApplicationIdentity:
    """Who is in front, as the platform reports it.

    Both fields, because neither alone is enough: a bundle id is exact and
    incomplete, a name is complete and forgeable. An application chosen by name
    alone could call itself "Notes" and be a terminal.
    """

    bundle_id: str
    name: str


def kind_of(application: ApplicationIdentity) -> ApplicationKind:
    """Classify one application, bundle id first."""

    known = _KIND_BY_BUNDLE_ID.get(application.bundle_id)
    if known is not None:
        return known
    # The Windows identity is an executable name and arrives lower-cased from
    # the adapter; `casefold` here is for a caller that built one by hand.
    known = _KIND_BY_EXECUTABLE.get(application.bundle_id.casefold())
    if known is not None:
        return known
    lowered = application.name.casefold()
    for substring, kind in _KIND_BY_NAME_SUBSTRING:
        if substring in lowered:
            return kind
    return "other"


def tier_for(application: ApplicationIdentity) -> ScreenTier:
    """The ceiling this application is granted at.

    One expression with no exceptions branch, deliberately. Every "except
    when..." that could be added here is a way past the gate, and the gate is
    the only thing standing between a model and a password field.
    """

    kind = kind_of(application)
    if kind in ("browser", "trading"):
        return "read"
    if kind in ("terminal", "shell"):
        return "click"
    return "full"


#: What each tier permits, as the set of action names the gate compares against.
#:
#: **Every name here has a tool that can reach it, and keeping that true is
#: part of what this table is.** Until 2026-08-28 it also listed ``mouse_move``
#: (in ``click`` and ``full``) and ``drag`` (in ``full``), and neither had
#: anywhere to be called from: ``ScreenGate`` has no method for either, the
#: server declares no tool for either, and ``drag`` had no port method either.
#: Nothing was broken by that -- a permission for an action nobody performs
#: refuses nothing -- which is exactly why it survived two ADRs.
#:
#: It is still wrong in two directions. Read forwards it is a claim: somebody
#: auditing what this project may do to a screen finds ``drag`` at ``full`` and
#: concludes this project drags. Read backwards it is a pre-payment: the next
#: person to add a drag tool finds it already permitted here and never makes
#: the argument for it, because the table appears to have made it already
#: (ADR-091 §2.4).
_ALLOWED: Final[dict[ScreenTier, frozenset[str]]] = {
    # Neither screenshots nor activation appears in any of these. Both are
    # gated by the allowlist instead of by the tier, and for the same reason:
    # seeing is what a grant is *for*, and bringing an approved window forward
    # reaches no further than seeing it does -- it synthesizes no input, and
    # every action that follows it is gated again, against whatever is
    # frontmost then (ADR-091 §2.3).
    "read": frozenset(),
    "click": frozenset({"left_click", "scroll"}),
    "full": frozenset(
        {
            "left_click",
            "right_click",
            "double_click",
            "triple_click",
            "middle_click",
            "scroll",
            "type",
            "key",
        }
    ),
}


def permits(tier: ScreenTier, action: str) -> bool:
    """Whether ``tier`` allows ``action``."""

    return action in _ALLOWED[tier]


@dataclass(frozen=True, slots=True)
class DisplayFrame:
    """Where one display sits in the space a synthesized event is posted into.

    Two coordinate spaces meet here, and until 2026-08-28 only one of them was
    written down:

    * the **display's own** space, top-left of *that screen* being (0, 0),
      which is what a model can measure -- a screenshot is of one display, and
      the picture has no way to express where that display sits;
    * the **global** space every synthesized event is posted into, where only
      the main display's top-left is (0, 0).

    On a machine with one screen these are the same space, and the identity
    function is a correct implementation of the conversion. That is precisely
    why this was wrong for a year with nothing to show for it: plug in a second
    monitor, and a coordinate read off *its* screenshot names a point on the
    **main** one. The click then succeeds, lands somewhere nobody asked for,
    and reports nothing -- there is no failure mode here, only a wrong place
    (F-22).

    Points, never pixels, for the reason ``ports/screen.py`` gives at length.
    """

    display_id: int
    #: The display's top-left in global points. (0, 0) for the main display,
    #: and negative for a screen arranged above or to the left of it -- which
    #: is ordinary, not exotic, and is why these are signed.
    origin_x: int
    origin_y: int
    width: int
    height: int

    def contains(self, x: int, y: int) -> bool:
        """Whether a display-local point is on this display at all.

        Half-open at the far edge on purpose: a 1470-point-wide display has its
        rightmost column at 1469, and ``x == 1470`` is already the first column
        of whatever is arranged to the right of it.
        """

        return 0 <= x < self.width and 0 <= y < self.height

    def to_global(self, x: int, y: int) -> tuple[int, int]:
        """One of this display's own points, in the space events are posted in."""

        return (self.origin_x + x, self.origin_y + y)


@dataclass(frozen=True, slots=True)
class ScreenshotBudget:
    """How large an image of a screen may be, in the units that actually bind.

    Two ceilings, and both are real. ``max_edge_px`` is what the vision encoder
    accepts before it downsamples on its own -- sending more is paying to
    transfer pixels that are discarded. ``max_tokens`` is what the *turn* can
    afford, and it binds first on any wide screen: a 1568x1568 image is inside
    the edge ceiling and costs 3136 tokens, twice the allowance.

    ``px_per_token`` is the encoder's own ratio, one token per 28x28 block.
    """

    px_per_token: int = 28
    max_edge_px: int = 1568
    max_tokens: int = 1568

    def tokens_for(self, width: int, height: int) -> int:
        block = self.px_per_token * self.px_per_token
        return math.ceil((width * height) / block)

    def fits(self, width: int, height: int) -> bool:
        return (
            max(width, height) <= self.max_edge_px
            and self.tokens_for(width, height) <= self.max_tokens
        )

    def fit(self, width: int, height: int) -> tuple[int, int]:
        """The largest proportional size within both ceilings.

        Binary search on the scale rather than algebra on the area, because the
        two ceilings bind on different screens and the rounding to whole pixels
        makes the closed form wrong at the boundary -- a size the arithmetic
        says fits can round up to one that does not, which would be an image
        rejected after being encoded.

        Aspect ratio is preserved, always. A screenshot is a coordinate system
        the model then clicks in; a squashed one would make every coordinate it
        derives wrong in a way nothing downstream could detect.
        """

        if width <= 0 or height <= 0:
            raise ValueError("a screen has a positive width and height")
        if self.fits(width, height):
            return width, height

        low, high = 0.0, 1.0
        best = (1, 1)
        for _ in range(_FIT_ITERATIONS):
            middle = (low + high) / 2
            candidate = (max(1, int(width * middle)), max(1, int(height * middle)))
            if self.fits(*candidate):
                best = candidate
                low = middle
            else:
                high = middle
        return best


#: Enough halvings to place the scale to well under one pixel on a 6K display,
#: and few enough that this is not a loop worth thinking about. 40 gives a
#: resolution of 6016 / 2^40, which is meaninglessly small.
_FIT_ITERATIONS: Final[int] = 40

#: The JPEG quality a screenshot is encoded at.
#:
#: Not 1.0, and not a rounder number. Text on a screen survives 0.75 legibly
#: while the file is a fraction of the size, and the file size is the thing
#: being spent -- this image crosses a process boundary on every look.
SCREENSHOT_QUALITY: Final[float] = 0.75


def frontmost_is_not_approved(*, action: str) -> str:
    """What the model is told when the window in front is one nobody approved.

    **What is frontmost is not named**, and until ADR-095 it was. This refusal
    fires precisely when the frontmost application is one nobody approved, so
    naming it turns every refused action into a reading of what the person is
    doing -- the thing the allowlist exists to prevent, and, in
    `activation_would_take_the_screen`'s own words about the identical
    situation, "a strictly larger capability than the one being refused".

    That sentence was written for the *activation* path and made true there,
    with a test asserting the name is absent. This path is the third one, and
    it had the same argument available and did not use it: it answered
    ``"Mail" is not in this session's approved list``, which meant the
    capability the activation refusal goes to lengths to withhold could be had
    for free by attempting a click that was going to be refused anyway --
    cheaper than an activation, which at least needs an approved target.

    A person looking at their own screen is a different reader and gets the
    name; that is what the console panel is for (ADR-095 §2). The boundary is
    checkable rather than merely declared: the name reaches the person's read
    path and no path a model can read.

    The remedy does not depend on the name, which is why it can be dropped: a
    model told which window is in front does not do anything different from one
    told only that it is unapproved. Both wait, or ask.
    """

    return (
        "The frontmost application is not in this session's approved list, and "
        f"the frontmost application is what {action} would reach.\n"
        "Call request_access with the applications you need and wait for the "
        "person to approve them, or wait until an approved application is in "
        "front.\n"
        "Do not attempt to work around this restriction -- never use "
        "AppleScript, System Events, PowerShell, SendKeys, shell commands, or "
        "any other method to "
        "send input to an application."
    )


def refusal(
    *,
    action: str,
    application: ApplicationIdentity,
    tier: ScreenTier,
) -> str:
    """What the model is told when the gate says no.

    Three parts, and the third is the one that does the work:

    1. **What was refused and why**, naming the tier, so the refusal is a fact
       about this application rather than an unexplained failure.
    2. **What to do instead.** A model that is only refused will try the next
       thing it can think of, and for a terminal the next thing is AppleScript.
       Naming the sanctioned route is what makes the refusal terminal.
    3. **An explicit prohibition on working around it.** Without this the
       previous sentence reads as a hint rather than a boundary -- and every
       route it forbids is one this project has: a shell tool, an osascript
       call, a second MCP server.
    """

    remedy = _REMEDY.get(kind_of(application), _DEFAULT_REMEDY)
    return (
        f'"{application.name}" is granted at tier "{tier}", '
        f"so {action} is not available for it.\n"
        f"{remedy}\n"
        "Do not attempt to work around this restriction -- never use "
        "AppleScript, System Events, PowerShell, SendKeys, shell commands, or "
        "any other method to "
        "send input to this application."
    )


_REMEDY: Final[dict[ApplicationKind, str]] = {
    "browser": (
        "It is visible in screenshots and can be read. For navigation, "
        "clicking or filling forms, drive the browser through its own "
        "automation tools rather than through the screen."
    ),
    "trading": (
        "Applications that move money are readable and never driven. Ask the "
        "person to perform the action themselves."
    ),
    "terminal": (
        "Keystrokes would go to this application's command line. For shell "
        "commands, use the sandbox tool, which runs them with a policy gate "
        "and an audit trail."
    ),
    "shell": (
        "Keystrokes would go to this application's editor or integrated "
        "terminal. To change a file use the workspace tools; to run a command "
        "use the sandbox tool."
    ),
}

_DEFAULT_REMEDY: Final[str] = (
    "Take a screenshot to see the current state, and ask the person to "
    "perform the action if it is needed."
)


def off_frame(*, x: int, y: int, frame: DisplayFrame) -> str:
    """What the model is told when a coordinate is not on the display it named.

    Two parts, where :func:`refusal` carries three, and dropping the third is
    the decision rather than an oversight. That third part -- "do not work
    around this, never use AppleScript" -- exists because a *permission*
    refusal is exactly what invites trying another route to the same window.
    This is not a permission refusal. It is a coordinate that does not name a
    place, and answering it with an anti-circumvention warning would teach a
    model that being a hundred points off is a security event, which is both
    false and the kind of noise that gets refusals ignored wholesale.
    """

    return (
        f"({x}, {y}) is not a point on display {frame.display_id}, which is "
        f"{frame.width}x{frame.height} points.\n"
        "Take a screenshot of the display you mean and read the coordinates "
        "off what it reports -- in that display's own points, and pass the "
        "same display_id back. A point measured on one screen names a "
        "different place on another."
    )


def focus_lost(
    *,
    approved: ApplicationIdentity,
    now_frontmost: ApplicationIdentity,
    delivered: int,
    total: int,
) -> str:
    """What the model is told when the window changed while input was going in.

    A distinct message from :func:`refusal`, because it describes a distinct
    and much worse situation: some of the input **was** delivered, and to the
    approved application, and the rest was not. A model told only "denied"
    would retype the whole string, and the first part would arrive twice.

    So it says how much landed, where the remainder did not go, and that a
    screenshot is the only way to find out what the screen now holds. It does
    not guess -- by the time this is written, this process no longer knows what
    has keyboard focus.
    """

    return (
        f'"{now_frontmost.name}" became frontmost while input was being '
        f'delivered to "{approved.name}".\n'
        f"{delivered} of {total} characters were delivered before the change; "
        "the remainder was NOT delivered, because it was approved for "
        f'"{approved.name}" only.\n'
        "Take a screenshot to see where focus went and what was actually typed."
    )


def activation_needs_a_grant(*, bundle_id: str) -> str:
    """What the model is told when it asks to bring forward something unapproved.

    Deliberately says nothing about whether that application exists or is
    running. The allowlist is the only thing consulted, so the only thing this
    process knows about the id is that a person did not approve it -- and an
    answer that distinguished "not approved" from "not installed" would make
    this tool a way to ask which applications are on the machine.
    """

    return (
        f'"{bundle_id}" is not in this session\'s approved list, so it cannot '
        "be brought to the front.\n"
        "Call request_access with the applications you need and wait for the "
        "person to approve them.\n"
        "Do not attempt to work around this restriction -- never use "
        "AppleScript, System Events, PowerShell, SendKeys, shell commands, or "
        "any other method to "
        "activate an application."
    )


def activation_would_take_the_screen(*, target: ApplicationIdentity) -> str:
    """What the model is told when something unapproved is in front right now.

    The narrowing ADR-091 §2.2 is about, and the message has to carry the
    reason or it reads as a bug. What is frontmost is **not named**: this
    refusal fires precisely when the frontmost application is one nobody
    approved, so naming it would turn every refused activation into a reading
    of what the person is doing -- which is the thing the allowlist exists to
    prevent, and a strictly larger capability than the one being refused.

    Nothing here suggests retrying in a loop. The remedy is a person: either
    they come back to one of the approved windows, or they approve the one
    they are in.
    """

    return (
        f'"{target.name}" was not brought to the front: the application that '
        "is frontmost right now is not in this session's approved list.\n"
        "Somebody is using a window that is not part of this task, and taking "
        "the screen away from it is not this tool's decision to make. Wait "
        "until an approved application is in front, or ask the person to "
        "approve the one they are working in.\n"
        "Do not poll this call, and do not attempt to work around it -- never "
        "use AppleScript, System Events, PowerShell, SendKeys, shell commands, "
        "or any other method to activate an application."
    )


def application_is_not_running(*, target: ApplicationIdentity) -> str:
    """What the model is told when the approved application is not launched.

    This tool activates and never launches, and the message says so rather
    than leaving the model to infer it from a failure. Launching is a strictly
    larger act than reordering windows -- it starts a process on somebody's
    machine, with whatever that application does on startup -- and it is not
    what a person approving a list of names agreed to (ADR-091 §4).
    """

    return (
        f'"{target.name}" is approved for this session but is not running, '
        "and this tool brings an application forward rather than starting "
        "one.\n"
        "Ask the person to open it. Starting an application is not something "
        "approving its name authorized."
    )


def activation_did_not_take(
    *, target: ApplicationIdentity, now_frontmost: ApplicationIdentity
) -> str:
    """What the model is told when the activation was made and did not hold.

    Not a policy refusal -- nothing was denied -- but the model has to hear it
    as a failure anyway, because its next action is about to be refused by the
    frontmost check for a reason it would otherwise have no way to see.
    Activation is a request to a window server, not a function call: a modal
    sheet, a full-screen space, or an application declining to come forward all
    end here.
    """

    return (
        f'"{target.name}" was asked to come to the front and did not: '
        f'"{now_frontmost.name or "an unidentified application"}" is frontmost '
        "instead.\n"
        "Nothing was clicked or typed. Take a screenshot to see what is on "
        "screen before trying anything that depends on the window you asked "
        "for."
    )


__all__ = [
    "SCREENSHOT_QUALITY",
    "ApplicationIdentity",
    "ApplicationKind",
    "DisplayFrame",
    "ScreenTier",
    "ScreenshotBudget",
    "activation_did_not_take",
    "activation_needs_a_grant",
    "activation_would_take_the_screen",
    "application_is_not_running",
    "focus_lost",
    "frontmost_is_not_approved",
    "kind_of",
    "off_frame",
    "permits",
    "refusal",
    "tier_for",
]
