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
from typing import Final

from agent_workbench.domain.computer import ApplicationIdentity, tier_for

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
    """

    if not applications:
        return False
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


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ConsentUnavailableError",
    "ask",
]
