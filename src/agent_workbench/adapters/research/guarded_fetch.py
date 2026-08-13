"""One GET, with every hop of its redirect chain judged before it opens.

Extracted from the search adapter when the second caller arrived. The reason it
has to be shared rather than reimplemented is the reason it exists at all:
``follow_redirects=True`` hands the choice of destination to the HTTP client, so
a public URL answering ``302 Location: http://169.254.169.254/`` reaches the
metadata service with the address guard never having seen the second address. A
second copy of this loop is a second place for that line to be wrong.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Final, Protocol
from urllib.parse import urljoin

from agent_workbench.adapters.research.address_guard import (
    AddressResolver,
    DestinationRefusedError,
    ProxyRouting,
    assert_public_destination,
    resolve_addresses,
    routes_through_proxy,
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


class GuardedStreamClient(GuardedHttpClient, Protocol):
    """The same client, plus the call that hands a body over in pieces.

    An extension rather than a replacement of the Protocol above, because the
    two callers want different things and widening the base would make the
    search adapter's fake client grow a method it never calls. What that fake
    exists for -- driving a whole redirect chain with no network and no
    listening port -- is the property this must not cost, so the streaming
    caller gets a wider Protocol and the same fakes stay drivable.

    ``stream`` is shaped like ``httpx.AsyncClient.stream`` deliberately: a
    context manager rather than a coroutine, because a response whose size is
    still unknown is only readable while its connection is open, and *closing
    it early* is the entire mechanism by which an oversized body never lands
    in this process. A coroutine returning a fully-read response could not
    offer that, which is how the ceiling came to be enforced after the fact.
    """

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
        timeout: float,
    ) -> AbstractAsyncContextManager[Any]: ...


#: One hop of a chain, opened. A context manager rather than a coroutine so the
#: streaming caller can look at a redirect's headers and drop its connection
#: without ever reading the body that came with it.
_OpenHop = Callable[[str], AbstractAsyncContextManager[Any]]


@asynccontextmanager
async def _guarded_chain(
    url: str,
    *,
    resolve: AddressResolver,
    proxied: ProxyRouting,
    max_redirects: int,
    open_hop: _OpenHop,
) -> AsyncGenerator[Any]:
    """The loop this module exists in order to have exactly one of.

    Both entry points below drive it, differing only in how a hop is opened.
    The alternative -- a second loop for the streaming caller -- is precisely
    what the module docstring warns about: the line that judges an address
    before the connection opens would then exist in two places, and the day
    one of them changed, only one would be under test.
    """

    current = url
    for _ in range(max_redirects + 1):
        await assert_public_destination(current, resolve=resolve, proxied=proxied)
        async with open_hop(current) as response:
            status = int(getattr(response, "status_code", 200))
            location = redirect_target(response) if status in REDIRECT_STATUSES else ""
            if not location:
                # Not a redirect, or a redirect status with nowhere to go. In
                # the second case the response is what the caller gets;
                # inventing a destination would be worse.
                yield response
                return
        # Resolved against the URL that answered, so a relative Location names
        # the same destination a browser would compute. Computed after the hop
        # is closed, which is what keeps a redirect's own body unread.
        current = urljoin(current, location)
    raise DestinationRefusedError("the redirect chain is too long")


async def guarded_get(
    http: GuardedHttpClient,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    resolve: AddressResolver = resolve_addresses,
    proxied: ProxyRouting = routes_through_proxy,
    max_redirects: int = MAX_REDIRECTS,
) -> Any:
    """GET ``url``, following redirects here rather than in the client.

    The whole body is in memory before this returns, because the client read
    it. A caller that must bound what a server can push into this process
    wants :func:`guarded_stream` instead.

    Raises :class:`DestinationRefusedError` for any hop that is not publicly
    routable, and for a chain that will not end.
    """

    @asynccontextmanager
    async def open_hop(target: str) -> AsyncGenerator[Any]:
        # Nothing to close: this client already read the body, so leaving the
        # block is where a streaming hop would have dropped the connection.
        yield await http.get(
            target,
            headers=headers,
            follow_redirects=False,
            timeout=timeout,
        )

    async with _guarded_chain(
        url,
        resolve=resolve,
        proxied=proxied,
        max_redirects=max_redirects,
        open_hop=open_hop,
    ) as response:
        return response


def guarded_stream(
    http: GuardedStreamClient,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    resolve: AddressResolver = resolve_addresses,
    proxied: ProxyRouting = routes_through_proxy,
    max_redirects: int = MAX_REDIRECTS,
) -> AbstractAsyncContextManager[Any]:
    """The same judged chain, with the final response's body still on the wire.

    The caller reads it inside the ``async with`` and decides, while it is
    arriving, whether to keep going. Leaving the block closes the response
    whether it was read to the end or abandoned partway.

    Raises the same :class:`DestinationRefusedError` in the same places, from
    the same loop -- this differs from :func:`guarded_get` only in how each hop
    is opened, which is the one property an SSRF defence cannot afford to have
    two versions of.
    """

    def open_hop(target: str) -> AbstractAsyncContextManager[Any]:
        return http.stream(
            "GET",
            target,
            headers=headers,
            follow_redirects=False,
            timeout=timeout,
        )

    return _guarded_chain(
        url,
        resolve=resolve,
        proxied=proxied,
        max_redirects=max_redirects,
        open_hop=open_hop,
    )


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
    "GuardedStreamClient",
    "guarded_get",
    "guarded_stream",
    "redirect_target",
]
