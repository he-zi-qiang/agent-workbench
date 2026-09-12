"""The approval dialog, and every way of not saying yes.

The dialog itself cannot be driven from a test -- it is a modal window waiting
for a person. What can be driven is everything around it: what is handed to
``osascript``, and how each shape of answer is read. Those are where a consent
check goes wrong, because they are the paths nobody looks at: a timeout, an
Escape, a missing binary. Each of them has exactly one safe reading.

Nothing here opens a dialog, on any platform. Every test that lets ``ask``
dispatch pins ``sys.platform`` first, so the osascript tests exercise the
osascript branch on Windows too, rather than whichever dialog the host has --
which is how this file came to open ten real Windows dialogs and wait two
minutes on each (2026-09-12, ``docs/status.md`` 第七十九批 §3). Behind that,
``tests/apps/conftest.py`` makes the real ``MessageBoxTimeoutW`` raise, and the
last test in this file asserts that it does.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

import pytest

from agent_workbench.apps.computer_mcp import consent
from agent_workbench.domain.computer import ApplicationIdentity

NOTES = ApplicationIdentity(bundle_id="com.apple.Notes", name="Notes")
TERMINAL = ApplicationIdentity(bundle_id="com.apple.Terminal", name="Terminal")


def _on_a_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``ask`` to the osascript branch, whatever machine runs this.

    ``ask`` reads ``sys.platform`` on every call -- the dispatch is the fact
    the two routing tests at the bottom assert -- so this is the one seam the
    branch needs, and the fake ``osascript`` is only ever reached through it.
    Without this line a test *describes* the macOS dialog and *exercises*
    whichever one the host has. On Windows that was the real box: the fake
    ``subprocess.run`` beside it was never called, the box waited its full
    countdown, and a timeout reads as a refusal, so half of these tests
    passed for the wrong reason and the other half failed for one that
    looked like consent logic (2026-09-12, ``docs/status.md`` 第七十九批 §3).
    """

    monkeypatch.setattr(consent.sys, "platform", "darwin")


def _answers(
    monkeypatch: pytest.MonkeyPatch, stdout: str, *, code: int = 0
) -> list[tuple[str, ...]]:
    """Replace osascript with a recording that answers however we say."""

    calls: list[tuple[str, ...]] = []

    def fake_run(arguments: Any, **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(arguments))
        return subprocess.CompletedProcess(arguments, code, stdout, "")

    _on_a_mac(monkeypatch)
    monkeypatch.setattr(consent.shutil, "which", lambda _: "/usr/bin/osascript")
    monkeypatch.setattr(consent.subprocess, "run", fake_run)
    return calls


def test_the_allow_button_is_the_only_thing_that_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answers(monkeypatch, f"button returned:{consent._ALLOW}, gave up:false")

    assert asyncio.run(consent.ask((NOTES,), reason="做点什么")) is True


def test_the_deny_button_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _answers(monkeypatch, f"button returned:{consent._DENY}, gave up:false")

    assert asyncio.run(consent.ask((NOTES,))) is False


def test_a_dialog_nobody_answered_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """`giving up after` returns an empty button and says it gave up.

    Read explicitly rather than inferred from the empty button, so a future
    AppleScript that reports a timeout some other way cannot read as a yes.
    """

    _answers(monkeypatch, "button returned:, gave up:true")

    assert asyncio.run(consent.ask((NOTES,))) is False


def test_escape_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """AppleScript reports a user cancel as a non-zero exit."""

    _answers(monkeypatch, "", code=1)

    assert asyncio.run(consent.ask((NOTES,))) is False


def test_a_mangled_answer_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything that is not the allow button named back to us is a no."""

    _answers(monkeypatch, "something else entirely")

    assert asyncio.run(consent.ask((NOTES,))) is False


def test_an_osascript_that_hangs_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dialog's own countdown should fire first; this is the backstop."""

    def hangs(arguments: Any, **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(arguments, 1.0)

    _on_a_mac(monkeypatch)
    monkeypatch.setattr(consent.shutil, "which", lambda _: "/usr/bin/osascript")
    monkeypatch.setattr(consent.subprocess, "run", hangs)

    assert asyncio.run(consent.ask((NOTES,))) is False


def test_a_machine_with_no_way_to_ask_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct from a refusal, because it is a different thing to fix.

    "You said no" and "nobody could be asked" send an operator to two different
    places, and only one of them is worth retrying.
    """

    _on_a_mac(monkeypatch)
    monkeypatch.setattr(consent.shutil, "which", lambda _: None)

    with pytest.raises(consent.ConsentUnavailableError):
        asyncio.run(consent.ask((NOTES,)))


def test_an_empty_list_is_not_a_question_worth_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_a_mac(monkeypatch)
    called: list[object] = []
    monkeypatch.setattr(consent.shutil, "which", lambda _: called.append(1))

    assert asyncio.run(consent.ask(())) is False
    assert called == []


def test_the_model_never_writes_applescript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injection surface, closed by construction rather than by escaping.

    Application names come from the model. If one were interpolated into the
    script source, a name carrying `do shell script` would run. The script is a
    constant and every variable piece travels in argv, where AppleScript reads
    it as data. Verified against a live osascript on 2026-08-24: the payload
    below was displayed as text and did not execute.
    """

    hostile = ApplicationIdentity(
        bundle_id="com.example.app",
        name='"); do shell script "touch /tmp/pwned',
    )
    calls = _answers(monkeypatch, f"button returned:{consent._ALLOW}, gave up:false")

    asyncio.run(consent.ask((hostile,)))

    script = calls[0][2]
    assert "do shell script" not in script
    assert script == consent._SCRIPT
    # The name is present, but as an argument rather than as source.
    assert any("do shell script" in argument for argument in calls[0][3:])


def test_the_person_is_told_which_tier_each_application_gets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approving Terminal grants strictly less than approving Notes.

    A dialog that hid that would be collecting consent for something other
    than what happens.
    """

    calls = _answers(monkeypatch, f"button returned:{consent._ALLOW}, gave up:false")

    asyncio.run(consent.ask((NOTES, TERMINAL), reason="整理会议纪要"))

    body = calls[0][4]
    assert "Notes" in body and "full" in body
    assert "Terminal" in body and "click" in body
    assert "整理会议纪要" in body
    # And that the grant does not outlive the session.
    assert "一重启就清空" in body


def test_the_person_is_told_the_set_can_be_reordered_and_where_that_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Since ADR-091 approving a list also means "and it may choose which of
    these is in front", which a list of names does not say on its own.

    The bound travels in the same sentence on purpose: the reassurance and the
    permission are one fact, and a dialog that showed only the first half
    would be collecting consent for something other than what happens.
    """

    calls = _answers(monkeypatch, f"button returned:{consent._ALLOW}, gave up:false")

    asyncio.run(consent.ask((NOTES, TERMINAL)))

    body = calls[0][4]
    assert "可以被切到前台" in body
    assert "不在名单里" in body


def test_the_default_button_is_the_refusal() -> None:
    """A person who hits Return by reflex must not have approved anything."""

    assert "default button denyLabel" in consent._SCRIPT


# --- Windows -------------------------------------------------------------------
#
# The dialog itself is `MessageBoxTimeoutW`, which no test here can drive. What
# can be driven is the reading of its answer, which is where a consent check
# goes wrong: three return values and everything else, and only one of them
# is a person saying yes (ADR-0108 §3).


def _box(answer: int) -> tuple[list[tuple[str, str, int]], consent.MessageBox]:
    calls: list[tuple[str, str, int]] = []

    def fake(title: str, body: str, milliseconds: int) -> int:
        calls.append((title, body, milliseconds))
        return answer

    return calls, fake


def test_on_windows_the_yes_button_is_the_only_thing_that_grants() -> None:
    _, yes = _box(consent._IDYES)
    _, no = _box(consent._IDNO)

    assert asyncio.run(consent.ask_win32((NOTES,), message_box=yes)) is True
    assert asyncio.run(consent.ask_win32((NOTES,), message_box=no)) is False


def test_on_windows_a_dialog_nobody_answered_refuses() -> None:
    """`MB_TIMEDOUT` is its own value, checked by name for the reason the
    macOS path checks `gave up:true` explicitly."""

    _, timed_out = _box(consent._MB_TIMEDOUT)

    assert asyncio.run(consent.ask_win32((NOTES,), message_box=timed_out)) is False


@pytest.mark.parametrize("answer", [0, 1, 2, 5, 8, 11, -1])
def test_on_windows_any_other_answer_refuses(answer: int) -> None:
    """A closed dialog (0 -- out of memory, or no desktop), Cancel, OK, and
    anything a future Windows might invent: none is a yes."""

    _, other = _box(answer)

    assert asyncio.run(consent.ask_win32((NOTES,), message_box=other)) is False


def test_on_windows_the_person_reads_the_tier_and_which_button_is_which() -> None:
    """A Yes/No box cannot label its buttons, so the body has to say what
    'yes' grants -- and the tier line the macOS body carries is still there."""

    calls, yes = _box(consent._IDYES)

    asyncio.run(
        consent.ask_win32(
            (NOTES, TERMINAL), reason="整理笔记", message_box=yes, timeout_seconds=30
        )
    )

    ((title, body, milliseconds),) = calls
    assert title == "屏幕控制批准"
    assert "权限" in body and ": full" not in body
    assert "full" in body and "click" in body
    assert "整理笔记" in body
    assert "「是」" in body and "「否」" in body
    assert milliseconds == 30_000


def test_on_windows_the_text_is_data_and_reaches_the_box_verbatim() -> None:
    """No interpreter sits between this call and the screen, so a name that
    would be code in an AppleScript is only ever a string here."""

    hostile = ApplicationIdentity(
        bundle_id="evil.exe", name='x" & do shell script "touch /tmp/pwned'
    )
    calls, yes = _box(consent._IDYES)

    asyncio.run(consent.ask_win32((hostile,), message_box=yes))

    assert hostile.name in calls[0][1]


def test_on_windows_an_empty_list_is_not_a_question_worth_asking() -> None:
    calls, yes = _box(consent._IDYES)

    assert asyncio.run(consent.ask_win32((), message_box=yes)) is False
    assert calls == []


def test_a_platform_that_is_not_windows_takes_the_osascript_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux is not Windows, so it is asked the way it always was: through
    `osascript`, and refused by name when there is none. CI runs this file on
    Linux with a stand-in `osascript`, which is why the dispatch must not
    raise for a platform it has no box for."""

    monkeypatch.setattr(consent.sys, "platform", "linux")
    monkeypatch.setattr(consent.shutil, "which", lambda _: None)

    with pytest.raises(consent.ConsentUnavailableError, match="osascript"):
        asyncio.run(consent.ask((NOTES,)))


def test_ask_routes_to_the_windows_dialog_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate calls `ask`; which dialog it gets is decided here, by platform."""

    calls, yes = _box(consent._IDYES)
    monkeypatch.setattr(consent.sys, "platform", "win32")
    monkeypatch.setattr(consent, "_windows_message_box", yes)

    assert asyncio.run(consent.ask((NOTES,), reason="r")) is True
    assert len(calls) == 1


def test_a_test_that_lets_ask_reach_the_real_box_fails_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape every osascript test above had on 2026-09-12, on purpose:
    ``ask`` on win32 with nothing faked. What must happen is what the autouse
    fixture in ``tests/apps/conftest.py`` does -- the real ``MessageBoxTimeoutW``
    is never called, and the test fails at once with a message that says how
    to fix it. This is the assertion that the fixture is live.

    Two things are chosen so that *removing* the fixture fails this test on
    both platforms this project runs on, and cheaply. The exception it expects
    is the tripwire's own -- an ``AssertionError`` with its message, matched
    that way because a ``conftest.py`` class imported under a second module
    name is a second class -- and not ``ConsentUnavailableError``, so on Linux
    the "no Windows message box here" refusal cannot pass for it. And the
    timeout is one second, so on Windows a missing fixture costs one second
    of dialog rather than a hundred and twenty.
    """

    monkeypatch.setattr(consent.sys, "platform", "win32")

    with pytest.raises(
        AssertionError, match="would have opened a real 「屏幕控制批准」"
    ):
        asyncio.run(consent.ask((NOTES,), timeout_seconds=1.0))
