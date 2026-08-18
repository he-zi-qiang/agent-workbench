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

    The macOS import is inside the function on purpose. It pulls pyobjc, which
    is behind the `computer-use` extra and is not installable on Linux at all,
    so a module-level import would make this package unimportable in CI -- and
    `adapters/screen` has to stay importable everywhere for the refusal below
    to be reachable.
    """

    if sys.platform != "darwin":
        raise ScreenUnavailableError(
            f"computer use is implemented for macOS only; this is {sys.platform}. "
            "The tier gate, the screenshot budget and the focus check are "
            "platform-independent and still under test -- what is missing is "
            "the adapter that touches a screen."
        )
    from agent_workbench.adapters.screen.darwin import DarwinScreen

    return DarwinScreen()


__all__ = ["for_this_platform"]
