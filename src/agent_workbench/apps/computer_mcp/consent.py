"""The place a person actually says yes.

ADR-070 §2 says a person approves the whole list once, and until now nothing
did: ``ScreenGate.grant`` wrote the model's own list into the allowlist and
answered "approved". The tier table, the frontmost re-check and the empty
starting allowlist were all real; the consent they were guarding was not. A
gate whose first check is "did a person agree" cannot be the one check nobody
implemented.

This module is that check, and it is deliberately small and deliberately
outside the server's own process.

**Why a subprocess and not NSAlert.** An ``NSAlert`` needs an ``NSApplication``
and a run loop on the main thread, and uvicorn owns that thread. Driving Cocoa
from a worker thread is undefined at best; wedging the event loop behind a
modal dialog would stop the server answering anything, including the health
probe an operator uses to find out why. ``osascript`` is a separate process: it
can hang, crash, or be killed without taking the server with it.

**Why the model's text never enters the script.** The application names arrive
from the model, and a name is a string a model can choose. Interpolating one
into an AppleScript source string is a code-injection surface -- a name
containing a quote and ``do shell script`` would run. So the script is a
constant and every piece of text is passed through ``argv``, where AppleScript
treats it as data and nothing else. Verified 2026-08-24 with a name carrying a
``do shell script`` payload: the payload was displayed as text and did not run.

**Why every unclear answer is a refusal.** A timeout, an Escape, a missing
``osascript``, a mangled line -- none of them is a person saying yes, and the
only safe reading of "we do not know" is no. The one thing that grants is the
allow button being named back to us.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Final

from agent_workbench.domain.computer import ApplicationIdentity, tier_for
from agent_workbench.ports.screen import ScreenUnavailableError

#: What the buttons say. The allow label is compared against the answer, so it
#: is a constant rather than a literal spelled twice.
_ALLOW: Final[str] = "允许这次会话"
_DENY: Final[str] = "拒绝"

#: The dialog is a constant. Everything variable arrives in ``argv``, which
#: AppleScript reads as data -- see the module docstring.
_SCRIPT: Final[str] = """on run argv
  set theTitle to item 1 of argv
  set theBody to item 2 of argv
  set allowLabel to item 3 of argv
  set denyLabel to item 4 of argv
  set timeoutSeconds to (item 5 of argv) as integer
  display dialog theBody with title theTitle ¬
    buttons {denyLabel, allowLabel} default button denyLabel ¬
    with icon caution giving up after timeoutSeconds
end run"""

#: Long enough to read a list of applications and think about it, short enough
#: that a request nobody is at the machine for does not hold a tool call open
#: until the client's own timeout kills it with no explanation.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 120.0

#: How much longer the subprocess is allowed than the dialog's own countdown.
#: `giving up after` ends the dialog from the inside; this is the backstop for
#: an osascript that never gets that far.
_GRACE_SECONDS: Final[float] = 15.0


class ConsentUnavailableError(RuntimeError):
    """There is no way to ask a person on this machine.

    Raised rather than returning False so the caller can say *why* nothing was
    approved. "You denied this" and "nobody could be asked" are different
    sentences to put in front of a model, and only one of them is worth
    retrying after the operator fixes something.
    """


def _body(applications: tuple[ApplicationIdentity, ...], reason: str) -> str:
    """What the person reads.

    Names, not bundle ids: a bundle id is the identity the allowlist stores
    because an application cannot rename its way out of it, and it is not what
    somebody deciding recognises. Both are shown -- the name to recognise, the
    id to disambiguate two things claiming the same name.

    The tier is shown per application because it is the part a person cannot
    infer: approving Terminal grants strictly less than approving Notes, and
    an approval dialog that hid that would be asking for consent to something
    other than what happens.

    Since ADR-091 the same sentence has to be said about the *set*, and for
    the same reason. Approving three applications now also means "and it may
    choose which of these three is in front", which is not something a list of
    names says on its own. The line about it carries the bound with it -- that
    an unapproved window in front stops everything, switching included --
    because the reassurance and the permission are one fact and a dialog that
    gave only the first half would be asking for consent to something other
    than what happens.
    """

    lines = [
        "一个 Agent 请求在**这次会话**里控制下列应用：",
        "",
    ]
    for held in applications:
        tier = tier_for(held)
        label = held.name or "(未命名)"
        named = held.bundle_id or "无 bundle id"
        lines.append(f"  · {label}   [{named}]  权限：{tier}")
    lines.extend(
        [
            "",
            f"理由：{reason}" if reason else "（没有给出理由）",
            "",
            "权限含义：read=只能看，click=能点不能打字，full=不受限。",
            "名单里的应用之间可以被切到前台；最前面的窗口不在名单里时，"
            "包括切换在内的一切动作都会被拒绝。",
            "批准只在这次会话有效，服务器一重启就清空。",
        ]
    )
    return "\n".join(lines)


async def ask(
    applications: tuple[ApplicationIdentity, ...],
    *,
    reason: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Put the whole list in front of a person, once, and wait.

    Whole list rather than one dialog per application, because a person asked
    six times says yes six times without reading the sixth. It is one decision
    about one set, which is also what ADR-070 §2 describes.

    One dialog per platform, and the platform is decided here rather than by
    the caller: the gate knows how to ask, not what a dialog is made of. Any
    platform without one raises rather than answering, because the only thing
    worse than a dialog nobody saw is a grant nobody gave (ADR-0108 §3).
    """

    if not applications:
        return False
    if sys.platform == "win32":
        return await ask_win32(
            applications, reason=reason, timeout_seconds=timeout_seconds
        )
    if sys.platform != "darwin":
        raise ConsentUnavailableError(
            f"no approval dialog is implemented for {sys.platform}, so nothing "
            "can be approved. The screen tools stay unavailable rather than "
            "granting themselves."
        )
    binary = shutil.which("osascript")
    if binary is None:
        raise ConsentUnavailableError(
            "no `osascript` on this machine, so nothing can be approved. The "
            "screen tools stay unavailable rather than granting themselves."
        )

    arguments = (
        binary,
        "-e",
        _SCRIPT,
        "屏幕控制批准",
        _body(applications, reason),
        _ALLOW,
        _DENY,
        str(int(timeout_seconds)),
    )
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            arguments,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + _GRACE_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # The dialog's own `giving up after` should have fired first. Reaching
        # here means osascript itself is stuck, which is a refusal like any
        # other unclear answer.
        return False
    except OSError as unavailable:  # pragma: no cover - defensive
        raise ConsentUnavailableError(
            f"could not ask for approval: {unavailable}"
        ) from unavailable

    if completed.returncode != 0:
        # Escape, or the dialog being dismissed. AppleScript reports a user
        # cancel as a non-zero exit, and it means the same thing as Deny.
        return False
    answer = completed.stdout.strip()
    # `giving up after` returns the button as empty and says so. Checked
    # explicitly rather than relying on the empty button, so a future AppleScript
    # that reports a timeout differently cannot read as an approval.
    if "gave up:true" in answer:
        return False
    return f"button returned:{_ALLOW}" in answer


# --- Windows ---------------------------------------------------------------

#: What `MessageBoxTimeoutW` answers with. `IDYES` and `IDNO` are the two
#: buttons; `MB_TIMEDOUT` is its own value for a dialog nobody touched, and it
#: is checked by name for the reason the macOS path checks `gave up:true`
#: explicitly -- a timeout must never read as a yes.
_IDYES: Final[int] = 6
_IDNO: Final[int] = 7
_MB_TIMEDOUT: Final[int] = 32_000

#: Yes/No, a warning icon, **No as the default button**, and the dialog on top
#: of everything and in front. The default matters the way it does on macOS:
#: a person who hits Enter without reading has refused.
_MB_YESNO: Final[int] = 0x0000_0004
_MB_ICONWARNING: Final[int] = 0x0000_0030
_MB_DEFBUTTON2: Final[int] = 0x0000_0100
_MB_SETFOREGROUND: Final[int] = 0x0001_0000
_MB_TOPMOST: Final[int] = 0x0004_0000
_MESSAGE_BOX_STYLE: Final[int] = (
    _MB_YESNO | _MB_ICONWARNING | _MB_DEFBUTTON2 | _MB_SETFOREGROUND | _MB_TOPMOST
)

#: How a Windows dialog is put up: ``(title, body, milliseconds) -> answer``.
#: Injectable so the reading of every answer can be asserted on a machine that
#: has no `user32`, and so the suite never opens a dialog on whoever runs it.
MessageBox = Callable[[str, str, int], int]


def _windows_message_box(title: str, body: str, milliseconds: int) -> int:
    """`MessageBoxTimeoutW`, reached through the one module that speaks Win32.

    Imported here rather than at the top so this file imports on every
    platform; the call itself lives in `adapters/screen/win32.py`, beside the
    rest of the platform, for the reason `darwin.py` holds every pyobjc call:
    one module carries the FFI and its suppressions, and this one carries the
    decision about what an answer means.
    """

    from agent_workbench.adapters.screen.win32 import message_box_with_timeout

    return message_box_with_timeout(
        title, body, style=_MESSAGE_BOX_STYLE, milliseconds=milliseconds
    )


def _body_win32(applications: tuple[ApplicationIdentity, ...], reason: str) -> str:
    """The macOS body plus the one line a Yes/No box needs.

    `MessageBoxW` cannot label its buttons, so the text has to say which button
    is which; without that line "Yes" is a word, not a decision.
    """

    return _body(applications, reason) + "\n\n" + "「是」= 允许这次会话    「否」= 拒绝"


async def ask_win32(
    applications: tuple[ApplicationIdentity, ...],
    *,
    reason: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    message_box: MessageBox | None = None,
) -> bool:
    """The Windows dialog, and every way of not saying yes (ADR-0108 §3).

    The same three rules as the macOS asker. The text is data all the way to
    the screen. Every unclear answer is a refusal: a timeout, a closed dialog,
    a return value this code does not recognise, a `user32` that will not put
    one up. The one thing that grants is the Yes button.
    """

    if not applications:
        return False
    # Resolved here rather than as the parameter's default, so a test (or a
    # deployment) that replaces the module's box replaces the one that is
    # used; a default binds at definition time.
    box = _windows_message_box if message_box is None else message_box
    try:
        answer = await asyncio.to_thread(
            box,
            "屏幕控制批准",
            _body_win32(applications, reason),
            int(timeout_seconds * 1000),
        )
    except ConsentUnavailableError:
        raise
    except (ScreenUnavailableError, OSError) as unavailable:
        # The platform module says why it could not put a box up; to the
        # gate that is one fact, "nobody could be asked", and it is a
        # different fact from "the person said no".
        raise ConsentUnavailableError(
            f"could not ask for approval: {unavailable}. The screen tools stay "
            "unavailable rather than granting themselves."
        ) from unavailable
    if answer == _MB_TIMEDOUT:
        return False
    return answer == _IDYES


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ConsentUnavailableError",
    "MessageBox",
    "ask",
    "ask_win32",
]
