"""Where this process is willing to send a request (ADR-027 §3.2).

The check this replaces compared literal hostnames against a small set, and its
own comment said what was missing: a *name* that resolves to a private address
was not caught. That was defensible while every fetched URL came from a search
engine's results -- reaching the metadata service meant poisoning an index
first. It stops being defensible the moment the model can name a URL itself,
because the model's input contains retrieved web text, and retrieved web text is
untrusted: one sentence on a page is then enough to aim a request at
``169.254.169.254``.

So the rule here is resolve-then-judge, and it is a deny-by-default rule rather
than a blocklist. An address is acceptable only if it is *globally routable*;
everything else -- loopback, link-local, private, unique-local, carrier-grade
NAT, benchmarking, documentation, unspecified, multicast -- is refused without
being enumerated. A blocklist of ranges is a list somebody has to remember to
extend; ``is_global`` is the complement of one already maintained upstream.

**A hostname with several addresses is judged by its worst one.** Nothing here
chooses which address the client will connect to, so one private answer among
public ones refuses the whole request.

What this does *not* close, stated plainly because a security boundary that
overstates itself is worse than one that does not exist: the name is resolved
here and resolved again by the HTTP client when it connects, so a resolver that
answers differently between those two moments (DNS rebinding) is not defeated by
this module. Closing that requires connecting to the address that was checked
and carrying the hostname in the ``Host`` header, which is a different change to
the transport. The exposure that motivated ADR-027 §3.2 -- a model steered into
naming an internal URL -- is fully covered; a resolver actively cooperating with
an attacker is not.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import urlsplit

#: Resolves a hostname to the addresses a client might connect to. Injectable
#: so the tests state the answer instead of depending on a real DNS -- a test
#: that needs the network to prove a refusal is a test that fails offline for
#: reasons unrelated to what it checks.
AddressResolver = Callable[[str], Awaitable[tuple[str, ...]]]

PermittedSchemes: Final[frozenset[str]] = frozenset({"http", "https"})

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class DestinationRefusedError(ValueError):
    """This process will not open a connection to that address.

    Carries the reason and the host, never the resolved address: the message
    reaches operator logs and, through a tool result, the model's own context,
    and the resolved address is the one detail that would confirm to a prompt
    injection which internal range it guessed right.
    """


async def resolve_addresses(host: str) -> tuple[str, ...]:
    """Every address ``host`` currently resolves to, IPv4 and IPv6."""

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as error:
        raise DestinationRefusedError(f"{host!r} does not resolve") from error
    return tuple(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public_address(address: IPAddress) -> bool:
    """Whether this address is one the public internet routes.

    ``is_global`` is the whole rule, with two corrections. Multicast addresses
    report ``is_global`` true and are not a destination this process has any
    business opening a TCP connection to; the unspecified address is refused for
    the same reason a bind-address check refuses ``0.0.0.0``.
    """

    if address.is_multicast or address.is_unspecified:
        return False
    if not address.is_global:
        return False
    # A second lock on the embedded forms. Today `is_global` already delegates
    # into each of these -- `::ffff:127.0.0.1` is not global, and a 6to4 address
    # wrapping 10.0.0.1 is not either -- so these branches agree with the check
    # above rather than adding to it. They are here because that delegation is
    # upstream behaviour this module would silently inherit a change to, and
    # this is the one place in the codebase where inheriting one quietly is a
    # network boundary rather than a bug.
    return all(is_public_address(embedded) for embedded in _embedded_v4(address))


def _embedded_v4(address: IPAddress) -> tuple[ipaddress.IPv4Address, ...]:
    if not isinstance(address, ipaddress.IPv6Address):
        return ()
    found: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        # (server, client). The client address is the interesting half; the
        # server half is checked too because neither should be internal.
        found.extend(address.teredo)
    return tuple(found)


async def assert_public_destination(
    url: str,
    *,
    resolve: AddressResolver = resolve_addresses,
) -> None:
    """Raise :class:`DestinationRefusedError` unless ``url`` is safe to request.

    Called before every connection, including each hop of a redirect chain: a
    public URL that answers ``302 Location: http://169.254.169.254/`` is the
    same attack with one more step, and a check that only ran on the first URL
    would not see it.
    """

    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise DestinationRefusedError("the URL could not be parsed") from error

    if parts.scheme.lower() not in PermittedSchemes:
        raise DestinationRefusedError(
            f"{parts.scheme!r} is not a scheme this process will request"
        )
    host = (parts.hostname or "").strip()
    if not host:
        raise DestinationRefusedError("the URL names no host")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        candidates = await resolve(host)
    else:
        # Already an address; there is nothing to resolve and nothing a
        # resolver could be asked to change its mind about.
        candidates = (str(literal),)

    if not candidates:
        raise DestinationRefusedError(f"{host!r} does not resolve")

    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as error:
            raise DestinationRefusedError(
                f"{host!r} resolved to something that is not an address"
            ) from error
        if not is_public_address(address):
            raise DestinationRefusedError(
                f"{host!r} resolves to an address that is not publicly routable"
            )


__all__ = [
    "AddressResolver",
    "DestinationRefusedError",
    "PermittedSchemes",
    "assert_public_destination",
    "is_public_address",
    "resolve_addresses",
]
