"""One GET, with every hop of its redirect chain judged before it opens.

Extracted from the search adapter when the second caller arrived. The reason it
has to be shared rather than reimplemented is the reason it exists at all:
``follow_redirects=True`` hands the choice of destination to the HTTP client, so
a public URL answering ``302 Location: http://169.254.169.254/`` reaches the
metadata service with the address guard never having seen the second address. A
second copy of this loop is a second place for that line to be wrong.
"""

from __future__ import annotations

from typing import Any, Final, Protocol
from urllib.parse import urljoin

from agent_workbench.adapters.research.address_guard import (
    AddressResolver,
    DestinationRefusedError,
    assert_public_destination,
    resolve_addresses,
)

#: How many hops a chain may take. Each hop is judged separately, so this bounds
#: looping rather than exposure.
MAX_REDIRECTS: Final[int] = 5

REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})


class GuardedHttpClient(Protocol):
    """The one method this needs from ``httpx.AsyncClient``.

    A Protocol rather than the client itself so a test can drive the whole
    redirect path with no network and no listening port.
    """

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
        timeout: float,
    ) -> Any: ...


async def guarded_get(
    http: GuardedHttpClient,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    resolve: AddressResolver = resolve_addresses,
    max_redirects: int = MAX_REDIRECTS,
) -> Any:
    """GET ``url``, following redirects here rather than in the client.

    Raises :class:`DestinationRefusedError` for any hop that is not publicly
    routable, and for a chain that will not end.
    """

    current = url
    for _ in range(max_redirects + 1):
        await assert_public_destination(current, resolve=resolve)
        response = await http.get(
            current,
            headers=headers,
            follow_redirects=False,
            timeout=timeout,
        )
        status = int(getattr(response, "status_code", 200))
        if status not in REDIRECT_STATUSES:
            return response
        location = redirect_target(response)
        if not location:
            # A redirect status with nowhere to go. The response is what the
            # caller gets; inventing a destination would be worse.
            return response
        # Resolved against the URL that answered, so a relative Location names
        # the same destination a browser would compute.
        current = urljoin(current, location)
    raise DestinationRefusedError("the redirect chain is too long")


def redirect_target(response: Any) -> str:
    headers: Any = getattr(response, "headers", None)
    if headers is None:
        return ""
    getter: Any = getattr(headers, "get", None)
    if getter is None:
        return ""
    return str(getter("location") or "")


__all__ = [
    "MAX_REDIRECTS",
    "REDIRECT_STATUSES",
    "GuardedHttpClient",
    "guarded_get",
    "redirect_target",
]
