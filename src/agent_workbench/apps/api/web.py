"""Serving the console, from the same origin as the API it calls.

Same origin is the whole design, not a convenience. The browser identity here is
three request headers, and two things follow from that which are easy to get
wrong in the other order.

CORS never enters the picture. A console on its own port would need the API to
answer preflights and to allow a header list; every one of those is a decision
about who may call this API from where, and the deployment that gets it slightly
wrong is the one that allowed ``*``. Mounted here, there is no cross-origin
request to permit.

And ``EventSource`` cannot carry the identity headers -- its constructor takes
``withCredentials`` and nothing else -- so the console reads the event stream
with ``fetch`` and parses the frames itself. That is not a workaround for the
mount; it is a fact about the browser API that would hold on any origin, and it
is why ``Last-Event-ID`` is sent explicitly rather than by the browser.

Mounted under a prefix rather than at ``/``. A static mount at the root answers
every path no route claimed, which turns a mistyped API path into a page of HTML
with a 200 on it -- and the client that asked for JSON has to parse a document
to discover it was wrong.

Absent when no directory is configured. The same answer this codebase gives
elsewhere: a surface that cannot work is not registered, so a client meets one
404 rather than a page that loads and then fails at every request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

#: Where the console lives. Not ``/``; see the module docstring.
WEB_PREFIX: Final[str] = "/ui"

#: The one file a directory must contain to be a console rather than a
#: directory somebody pointed at by accident.
ENTRY_FILE: Final[str] = "index.html"

#: Vite copies this public file to the root of ``dist``. Requiring it keeps the
#: API from accepting ``web/`` itself, whose entry document points at TSX
#: source that neither StaticFiles nor a browser can compile.
BUILD_MARKER: Final[str] = "agent-workbench-console.json"


class WebDirectoryError(ValueError):
    """The configured console directory is not one.

    Raised at assembly rather than tolerated: a process told to serve a console
    and silently serving nothing is a deployment that looks healthy and has no
    interface, and the person who mistyped the path finds out from a browser.
    """


def resolve_web_directory(raw: str) -> Path:
    """Check the directory now, while a startup failure is still cheap."""

    directory = Path(raw).expanduser().resolve()
    if not directory.is_dir():
        raise WebDirectoryError(f"the web directory does not exist: {directory}")
    if not (directory / ENTRY_FILE).is_file():
        raise WebDirectoryError(f"the web directory has no {ENTRY_FILE}: {directory}")
    if not (directory / BUILD_MARKER).is_file():
        raise WebDirectoryError(
            f"the web directory is not a production build; missing {BUILD_MARKER}: "
            f"{directory}"
        )
    return directory


def mount_console(app: FastAPI, directory: Path) -> None:
    """Serve the console under ``/ui``, and point ``/`` at it.

    ``html=True`` makes the mount serve ``index.html`` for the prefix itself,
    and that is enough -- but **not for the reason this docstring gave until
    2026-08-31**, which was "the console is one page with its own tab switching,
    so there are no client routes to fall back on".

    There are client routes: the console is a ``HashRouter`` with fourteen of
    them, three of which are deep links. What makes them irrelevant here is the
    *hash*: everything after ``#`` never reaches this server, so every request
    it sees is for ``/ui`` or a built asset, and there is nothing to fall back.

    The conclusion is the same and the premise is not, which matters to exactly
    one reader: whoever switches this console to ``BrowserRouter``. Under the
    old sentence they would read "no client routes" and leave this mount alone;
    under this one they know they need an ``index.html`` fallback for unknown
    paths under ``/ui``.
    """

    app.mount(
        WEB_PREFIX,
        StaticFiles(directory=directory, html=True),
        name="console",
    )

    app.add_api_route(
        "/",
        _console_redirect,
        methods=["GET"],
        include_in_schema=False,
    )


async def _console_redirect() -> RedirectResponse:
    """A convenience, and deliberately a redirect rather than the page.

    Serving the console at ``/`` as well would give it two addresses, and the
    relative asset paths inside it only resolve under one of them.
    """

    return RedirectResponse(url=f"{WEB_PREFIX}/", status_code=307)


__all__ = [
    "BUILD_MARKER",
    "ENTRY_FILE",
    "WEB_PREFIX",
    "WebDirectoryError",
    "mount_console",
    "resolve_web_directory",
]
