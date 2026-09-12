"""What the guarded browser is called, from the side that has to name it.

ADR-0113 §3.4 fixed the surface at six tools and said why it is six rather than
twenty. This module is that list in the one place both callers can see it: a
Task profile declares the six in ``[[mcp.servers]]`` and a coding session's
process asks the same server for the same six at startup. Neither may import
the server package -- ``apps/browser_mcp`` is an outer-boundary package and
``tests/architecture/test_dependency_boundaries.py`` fails a core module that
reaches into one -- so the names live here, the way ``domain/sandbox.py`` holds
``SANDBOX_REMOTE_TOOL`` for the same reason.

**Named, not discovered.** The local arm asks the server what it has and admits
only what is listed here. That ordering is the point: a directory that grew a
seventh tool overnight would otherwise widen a coding session's catalogue
without anybody deciding to, and ADR-025's whole argument for freezing bindings
at startup is that the set a turn may reach is a deployment's decision rather
than a server's.

The local names a turn actually sees are ``mcp_browser_<remote>`` --
``adapters/mcp/naming.py`` builds them from the alias below, and the alias is
here rather than at each call site so the two arms cannot drift into naming one
server two things.
"""

from __future__ import annotations

from typing import Final

#: The alias every profile gives this server, and therefore the first half of
#: every local tool name derived from it. Changing it renames six tools, which
#: renames them inside every Task envelope already frozen -- so it is a value
#: with a decision behind it rather than a label.
BROWSER_ALIAS: Final[str] = "browser"

#: The six, in the order ADR-0113 §3.4 introduces them: open a page, read what
#: it says, evaluate in it, act on it, picture it, and ask what went wrong.
#:
#: ``browser_eval`` and ``browser_interact`` are why the server is declared
#: ``retryable_effects = false``: one mutates the page it runs in and the other
#: clicks things, and one non-replayable tool makes the server non-replayable
#: (ADR-0113 §3.4, ADR-025 §2.7). That declaration is a property of the server
#: and belongs to the profile; this tuple is only the list of names.
BROWSER_REMOTE_TOOLS: Final[tuple[str, ...]] = (
    "browser_open",
    "browser_snapshot",
    "browser_eval",
    "browser_interact",
    "browser_screenshot",
    "browser_diagnostics",
)

__all__ = [
    "BROWSER_ALIAS",
    "BROWSER_REMOTE_TOOLS",
]
