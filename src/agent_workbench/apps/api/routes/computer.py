"""What the screen-control server says about itself, forwarded to the console.

**One route, read-only, and the narrowness is the decision** (ADR-095 §5).

The gate that decides what a model may do to this machine's screen lives in a
separate process -- `agent-workbench-computer`, MCP over Streamable HTTP on a
loopback port -- and until now nothing in this process could see it. The console
page said so out loud and showed no state at all rather than plausible rows.

Two ways to give a browser that state, and the one not taken is the reason this
file exists. Letting the browser read `127.0.0.1:8768` directly means giving that
process CORS, and CORS is about **which pages may read the response**: get the
origin list wrong and every website this person visits can read what they
approved and what they are looking at. Forwarding through here has no such
failure mode -- the console and this API are one origin, so the browser's own
same-origin policy does the work, and nothing has to be configured correctly for
that to hold.

The cost is stated rather than hidden: the control plane becomes a client of a
process that can move the cursor. What keeps that from being a widening:

* it forwards **one** route, and that route is a read. There is no path from
  here to any tool that touches the screen, and
  `tests/architecture/test_computer_forward_is_read_only.py` fails if one
  appears.
* this process still has no `ScreenPort` and no way to synthesize input. It
  receives a JSON document another process composed. ADR-070's "agent-api has no
  screen" is unchanged.
* the URL is validated as loopback at config load, so this cannot become a way
  to read somebody else's display.

**Unreachable is the normal answer.** That server is not started by any of the
ordinary `scripts/dev.sh` paths, so a console asking this on a typical machine
gets "not running" -- and that is a different fact from "running, and nobody has
approved anything". They are returned as different shapes for the same reason
the page refused to draw an invented allowlist: a reader who cannot tell those
apart will read one as the other.
"""

from __future__ import annotations

import sys
from typing import Any, Final, Literal

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel

from agent_workbench.apps.api.state import dependencies_of

COMPUTER_PREFIX: Final[str] = "/v1/computer"

#: Long enough for a loopback round trip that has to read the front of the
#: screen, short enough that a console polling this does not stack up requests
#: against a server that is wedged. The read itself is one `NSWorkspace` call.
FORWARD_TIMEOUT_SECONDS: Final[float] = 2.0

router = APIRouter(prefix=COMPUTER_PREFIX, tags=["computer"])

HostPlatform = Literal["darwin", "win32", "linux", "other"]


def host_platform() -> HostPlatform:
    """Which platform *this API process* runs on, coarsened to four names.

    Reported so the console can say how to start the screen server on *this*
    deployment instead of hard-coding one launcher. The two native launchers
    differ (`scripts/dev.sh computer-server` on macOS, `scripts\\computer.cmd`
    on Windows, ADR-0108), and in the Compose stack the API is a Linux
    container while the server has to run on the host outside it -- so the
    browser's own OS is the wrong thing to branch on: the person reading the
    console on a Mac may be looking at a Windows host's stack. The 2026-09-04
    review found the page telling Windows users to run a macOS-only command.

    Coarsened rather than raw ``sys.platform``: the console branches on four
    cases and a raw string would invite it to grow a fifth by accident.
    """

    if sys.platform.startswith("darwin"):
        return "darwin"
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


class ComputerSessionResponse(BaseModel):
    """The screen server's answer, or the reason there is none.

    ``reachable`` is the field the console branches on, and it is separate from
    an empty ``session`` on purpose: a server that is not running and a server
    with an empty allowlist are the two states this whole page has been careful
    about since it was written.
    """

    reachable: bool
    #: Exactly what that server answered, passed through unparsed.
    #:
    #: Not re-modelled here, and that is deliberate: this process does not own
    #: the shape of a screen session, and a model class in this file would be a
    #: second definition of it that drifts from the first. What this route owns
    #: is *whether the answer arrived*.
    session: dict[str, Any] | None = None
    #: Why not, in the console's own terms. Empty when it did arrive.
    detail: str = ""
    #: Where this API process runs, so the console can name the right launcher
    #: for the screen server -- see :func:`host_platform`. Present in every
    #: answer, because the hint is needed exactly when ``reachable`` is false.
    host_platform: HostPlatform


@router.get("/session", response_model=ComputerSessionResponse)
async def session(request: Request) -> ComputerSessionResponse:
    """Read the screen server's session, or say why it could not be read."""

    dependencies = dependencies_of(request)
    # Resolved and discarded, like `tasks.capabilities`. Nothing here belongs to
    # a principal -- it describes this machine -- but a route reachable without
    # the identity adapter would be the one such route in the process.
    dependencies.principals.resolve(request)
    url = dependencies.config.computer_session_url

    try:
        async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT_SECONDS) as client:
            answered = await client.get(url)
    except httpx.HTTPError:
        # Every transport failure is the same fact to a reader: that server is
        # not answering here. Distinguishing "connection refused" from "timed
        # out" would put the console in the business of diagnosing a process it
        # cannot start.
        return ComputerSessionResponse(
            reachable=False,
            detail="屏幕控制服务器没有在这台机器上应答。",
            host_platform=host_platform(),
        )

    if answered.status_code != 200:
        return ComputerSessionResponse(
            reachable=False,
            detail=f"屏幕控制服务器答了 {answered.status_code}。",
            host_platform=host_platform(),
        )
    return ComputerSessionResponse(
        reachable=True, session=answered.json(), host_platform=host_platform()
    )


__all__ = ["COMPUTER_PREFIX", "ComputerSessionResponse", "router"]
