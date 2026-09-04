"""Screen adapters: one per platform, plus the honest refusal for the rest.

`for_this_platform` is the only entry point a composition root should use. It
returns a working adapter or raises -- it never returns something that pretends
to work, because a screen adapter that silently does nothing is a model
convinced it has clicked a button that was never clicked.
"""

from __future__ import annotations

import sys

from agent_workbench.ports.screen import ScreenPort, ScreenUnavailableError


def for_this_platform() -> ScreenPort:
    """The adapter for the machine this process is on.

    The platform imports are inside the function on purpose. The macOS one
    pulls pyobjc, which is behind the `computer-use` extra and is not
    installable on Linux at all, so a module-level import would make this
    package unimportable in CI -- and `adapters/screen` has to stay importable
    everywhere for the refusal below to be reachable.
    """

    if sys.platform == "darwin":
        from agent_workbench.adapters.screen.darwin import DarwinScreen

        return DarwinScreen()
    if sys.platform == "win32":
        # Same shape, other platform (ADR-0108). The import is inside for a
        # weaker version of the macOS reason: `win32.py` imports nothing that
        # is missing elsewhere, but it resolves `user32` at construction, and
        # a module-level construction would make this package raise on
        # import everywhere but Windows.
        from agent_workbench.adapters.screen.win32 import Win32Screen

        return Win32Screen()
    raise ScreenUnavailableError(
        f"computer use is implemented for macOS and Windows only; this is "
        f"{sys.platform}. The tier gate, the screenshot budget and the focus "
        "check are platform-independent and still under test -- what is "
        "missing is the adapter that touches a screen."
    )


__all__ = ["for_this_platform"]
