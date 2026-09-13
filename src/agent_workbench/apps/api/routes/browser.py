"""The browser's latest frame, forwarded to the console (ADR-0113 §3.6).

**One route, read-only, and the narrowness is the decision** -- the same
sentence `routes/computer.py` opens with, and deliberately the same shape. That
file's reasoning transfers without amendment:

* letting the console read the browser server directly would mean giving that
  process CORS, and CORS is about *which pages may read the response*. Forwarding
  through here has no such failure mode, because the console and this API are one
  origin and the browser's own same-origin policy does the work.
* this process gains no way to *drive* the browser. It receives a JPEG another
  process composed. There is no path from here to `browser_interact`, and
  `tests/architecture/` fails if one appears.
* the URL is validated as loopback at config load, so this cannot become a way to
  watch a browser on another host.

**Read-only in the strong sense, not merely the HTTP one.** ADR-0113 §4 refuses
to let a person drive this browser from the panel, because two operators on one
page need an arbitration story and there isn't one. That refusal is enforced
here by there being nothing to enforce: this router has one GET and it returns
an image.

**Not running is the normal answer**, exactly as it is for the screen server: the
browser is started by `scripts/dev.sh browser-server` or the Compose `browser`
service, and a console asking this on a plain checkout gets 503. That is a
different fact from "running, and nothing has been opened yet", which is 204 --
and they are different shapes for the reason the computer page was careful
about: a reader who cannot tell them apart will read one as the other.
"""

from __future__ import annotations

from typing import Final

import httpx
from fastapi import APIRouter, Request, Response

from agent_workbench.apps.api.state import dependencies_of

BROWSER_PREFIX: Final[str] = "/v1/browser"

#: The header the browser server puts the frame's own address on, forwarded
#: here under the same name (ADR-0119). Spelled here rather than imported from
#: `apps/browser_mcp/server.py`: the API process does not import the browser
#: server -- it talks to it over loopback -- and `tests/architecture` holds
#: that separation. A test asserts the two spellings agree.
URL_HEADER: Final[str] = "X-Browser-Url"

#: A frame is tens of kilobytes over loopback. Short, because a console polling
#: this must not stack requests against a wedged server -- the same reasoning
#: and very nearly the same number as the computer forward.
FORWARD_TIMEOUT_SECONDS: Final[float] = 3.0

router = APIRouter(prefix=BROWSER_PREFIX, tags=["browser"])


@router.get(
    "/frame",
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "The latest frame."},
        204: {"description": "The browser is running but has opened nothing yet."},
        503: {"description": "The browser server is not answering here."},
    },
)
async def frame(request: Request) -> Response:
    """Forward the latest screencast frame, or say why there is none."""

    dependencies = dependencies_of(request)
    # Resolved and discarded, as `computer.session` does: nothing here belongs
    # to a principal -- it describes this deployment -- but a route reachable
    # without the identity adapter would be the one such route in the process.
    dependencies.principals.resolve(request)
    url = dependencies.config.browser_frame_url

    try:
        async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT_SECONDS) as client:
            answered = await client.get(url)
    except httpx.HTTPError:
        # Every transport failure is one fact to a reader: that server is not
        # answering here. The console says so and offers the command; it is not
        # in the business of diagnosing a process it cannot start.
        return Response(
            status_code=503,
            content=b"",
            headers={"X-Browser-Detail": "browser server not reachable"},
        )

    if answered.status_code == 204:
        return Response(status_code=204)
    if answered.status_code != 200:
        return Response(
            status_code=503,
            content=b"",
            headers={"X-Browser-Detail": f"browser server said {answered.status_code}"},
        )
    headers = {
        # The frame changes several times a second and a cached one is worse
        # than none: a panel showing a stale screenshot of a page that has
        # since navigated is actively misleading.
        "Cache-Control": "no-store"
    }
    # Forwarded, not derived (ADR-0119). This process does not know what page
    # the browser is on -- it holds a URL to a frame endpoint, not a browser --
    # and the one component that does know puts the answer on the frame it
    # composed, so the address and the picture cannot disagree.
    where = answered.headers.get(URL_HEADER)
    if where:
        headers[URL_HEADER] = where
    return Response(answered.content, media_type="image/jpeg", headers=headers)


__all__ = ["BROWSER_PREFIX", "router"]
