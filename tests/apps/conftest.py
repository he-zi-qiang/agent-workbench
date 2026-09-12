"""One fixture, for every test under ``tests/apps/``: the real Windows approval
dialog fails the test instead of opening.

Measured 2026-09-12 on Windows 11 (``docs/status.md`` 第七十九批 §3): the
osascript tests in ``test_computer_consent.py`` replaced ``subprocess.run`` and
nothing else, so on win32 ``consent.ask`` dispatched past the fake to the real
``MessageBoxTimeoutW``, which sat on the desktop for its full 120-second
countdown and then answered "nobody said yes". Ten tests reach that dispatch --
counted by making the box raise, not by waiting for it -- and a timed-out box
reads as a refusal, so five of them passed for the wrong reason and five
failed for one that looked like broken consent logic. The record said six
dialogs; the code allows ten.

Pinning the platform in those tests is the fix, and it lives beside them.
This is what makes the *next* slip cost a second and a stack trace rather
than two minutes and a modal window on whoever is running the suite -- and
the slip can be made from three files, not one: ``ScreenGate``'s default
``consent`` is the real asker, so a gate or MCP-server test that omits
``consent=`` reaches the same box. Hence a directory-wide fixture rather than
one inside the consent file.

Patched at the last Python function before ``user32`` rather than at
``consent._windows_message_box``, so a route that bypassed the consent
module's indirection would trip it too. The exception is deliberately not one
``ask_win32`` converts into ``ConsentUnavailableError``: that conversion is the
product's answer to "no desktop", and a test that reached the real box by
accident must not pass by being told the box did not exist.
``test_computer_consent.py`` asserts this fixture is live.
"""

from __future__ import annotations

import pytest

from agent_workbench.adapters.screen import win32


class RealDialogReached(AssertionError):
    """A test let the consent path reach ``MessageBoxTimeoutW``.

    An ``AssertionError`` because it is a failed assertion about the test,
    and because that is the one base class neither ``ask_win32`` nor the
    gate above it catches. Tests match it by base class and message rather
    than by importing this name: pytest may load a ``conftest.py`` under a
    module name of its own, and a class imported under a second name is a
    second class that ``pytest.raises`` would not recognise.
    """


def _refuse_to_open(title: str, body: str, *, style: int, milliseconds: int) -> int:
    del body, style
    raise RealDialogReached(
        f"this test would have opened a real 「{title}」 dialog and waited "
        f"{milliseconds / 1000:.0f} s for a person to answer it. Pin "
        "`consent.sys.platform` to the branch under test, or pass a fake "
        "`message_box`; the module docstring of tests/apps/conftest.py says why."
    )


@pytest.fixture(autouse=True)
def no_real_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win32, "message_box_with_timeout", _refuse_to_open)
