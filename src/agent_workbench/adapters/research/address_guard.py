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

The other half of that same sentence, and the reason this module has two
branches. Resolve-then-judge assumes the answer this process gets back is the
place the connection will land. **Behind a forward proxy it is not.** The client
sends ``CONNECT example.com:443`` to the proxy and the proxy does the resolving;
what our own resolver said never reaches the wire. On a developer machine
running a fake-IP/TUN proxy that assumption does not merely weaken, it inverts:
every hostname resolves into ``198.18.0.0/15``, a range that is correctly *not*
globally routable, and the guard refuses nineteen out of nineteen pages that
``curl`` on the same machine fetches with a 200. The guard was judging a
placeholder the proxy hands out and then translates back.

So the question asked first is *who resolves this connection*, and the answer
decides which rule applies:

* **This process does** (no proxy for this URL) -- resolve-then-judge, exactly
  as above, unchanged.
* **The proxy does** -- do not resolve, because our resolver is not the
  authority on the destination and treating it as one is what produced the
  refusals. Judge the *name* instead: the policy has to be expressed in the
  vocabulary the request actually travels in.

Said plainly, because this is the narrower guarantee: **in the proxied branch
the enforcement boundary is the proxy, not this process.** What survives here is
an IP literal check -- ``http://169.254.169.254/`` is refused in either branch,
since a literal needs no resolver to judge -- plus a name rule that refuses the
internal forms a model could be steered into naming. What is given up is the
general case: a name this deployment has never heard of, pointing at something
internal to the *proxy's* network, is allowed through by this module and stopped
only by whatever egress policy that proxy has. A deployment that needs the full
resolve-then-judge guarantee must therefore not put a proxy in front of this
process, or must exclude these destinations from it with ``NO_PROXY``.

Which branch runs is read from the environment the HTTP client itself reads
(``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY``, minus ``NO_PROXY``), and the
reading is deliberately biased: see :func:`routes_through_proxy` for why the
ambiguous cases all resolve toward "this process resolves".
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import urlsplit
from urllib.request import getproxies

#: Resolves a hostname to the addresses a client might connect to. Injectable
#: so the tests state the answer instead of depending on a real DNS -- a test
#: that needs the network to prove a refusal is a test that fails offline for
#: reasons unrelated to what it checks.
AddressResolver = Callable[[str], Awaitable[tuple[str, ...]]]

#: Whether the connection to this URL is opened through a forward proxy, in
#: which case this process does not resolve the destination and its resolver's
#: answer says nothing about where the bytes go. Injectable for the same reason
#: :data:`AddressResolver` is: a test that has to export an environment variable
#: to reach a branch is a test that changes meaning depending on the shell that
#: started it, and this is the branch where that would be least acceptable.
ProxyRouting = Callable[[str], bool]

PermittedSchemes: Final[frozenset[str]] = frozenset({"http", "https"})

#: Name suffixes that are internal by construction. `.local` is mDNS, `.internal`
#: is the private-use TLD every cloud metadata service publishes itself under
#: (`metadata.google.internal`), `.localdomain` is the historical resolver
#: default and `.home.arpa` is RFC 8375's homenet zone.
#:
#: Unlike the address rule, this *is* a blocklist, and it is one for a reason
#: worth stating rather than hiding: in the proxied branch there is no
#: `is_global` to take the complement of. `is_global` works because somebody
#: upstream maintains the list of what the internet does not route; names have no
#: such list, because whether `wiki.corp` is internal is a fact about one
#: network. So this enumerates the forms that are internal *everywhere* and is
#: honest that a site-specific name is not among them.
_INTERNAL_SUFFIXES: Final[tuple[str, ...]] = (
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
)

#: Refused outright rather than by suffix, since neither has a dot to suffix.
_INTERNAL_NAMES: Final[frozenset[str]] = frozenset({"localhost", "localhost6"})

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


def routes_through_proxy(url: str) -> bool:
    """Whether a request for ``url`` leaves through a forward proxy.

    Read from :func:`urllib.request.getproxies`, which is the same call httpx
    makes to build its own proxy mounts, so the two agree on *which* variables
    are consulted and on macOS's System Configuration fallback for free. What is
    reimplemented here is the ``NO_PROXY`` match, and it is reimplemented
    **deliberately loosely**.

    The looseness has a direction, and the direction is the whole design. The
    two ways to be wrong are not symmetric:

    * Claiming "proxied" when the client actually connects directly means the
      name check ran where resolve-then-judge should have. That is a hole.
    * Claiming "direct" when the client actually proxies means resolve-then-judge
      ran against a resolver that is not authoritative. That is at worst today's
      bug -- a reachable page refused -- and never an unsafe connection.

    So every ambiguity resolves toward "direct". A ``NO_PROXY`` entry is treated
    as bypassing whenever it *plausibly* matches -- exact host, suffix, or
    parent domain -- which is a superset of httpx's rule, and a superset here
    means erring into the branch that checks more.
    """

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    # Trailing dot is the same name; leaving it on would make `example.com.`
    # miss a `NO_PROXY=example.com` that the client honours.
    host = (parts.hostname or "").lower().rstrip(".")
    if host == "":
        return False

    # Annotated rather than inferred: `getproxies` carries no useful stub, so
    # the types here would otherwise be whatever the checker managed to resolve
    # that run -- which is a signal that reports differently cold than warm.
    settings: dict[str, str] = {
        str(key).lower(): str(value) for key, value in getproxies().items()
    }

    no_proxy: str = settings.get("no", "")
    entries = [entry.strip().lower() for entry in no_proxy.split(",")]
    if "*" in entries:
        # httpx reads a bare `*` as "ignore every proxy variable", not as one
        # more pattern, and so does curl.
        return False
    for entry in entries:
        if entry == "":
            continue
        if _bypasses(host, entry):
            return False

    proxy = settings.get(scheme) or settings.get("all")
    return bool(proxy)


def _bypasses(host: str, entry: str) -> bool:
    """Whether one ``NO_PROXY`` entry plausibly covers ``host``."""

    if "/" in entry:
        # A CIDR block only ever covers a literal address; a name cannot be
        # inside one without being resolved, and resolving here is the very
        # thing this branch exists to avoid.
        try:
            network = ipaddress.ip_network(entry, strict=False)
            return ipaddress.ip_address(host) in network
        except ValueError:
            return False
    bare = entry.lstrip(".")
    return host == bare or host.endswith(f".{bare}") or host.endswith(bare)


def assert_permitted_name(host: str) -> None:
    """Raise unless ``host`` is a name this process will ask a proxy to reach.

    The proxied branch's whole rule. It refuses what is internal by construction
    -- ``localhost``, the private-use suffixes, and any single-label name, since
    a name with no dot is a search-domain lookup that resolves inside whatever
    network answers it and never on the public internet.

    It does not, and cannot, refuse a site-specific internal name. That limit is
    in the module docstring rather than softened here.
    """

    name = host.lower().rstrip(".")
    if name == "":
        raise DestinationRefusedError("the URL names no host")
    if name in _INTERNAL_NAMES:
        raise DestinationRefusedError(f"{host!r} is a name that never leaves this host")
    if "." not in name:
        raise DestinationRefusedError(
            f"{host!r} has no domain and resolves only inside a private network"
        )
    if name.endswith(_INTERNAL_SUFFIXES):
        raise DestinationRefusedError(
            f"{host!r} is in a namespace reserved for private networks"
        )


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
    proxied: ProxyRouting = routes_through_proxy,
) -> None:
    """Raise :class:`DestinationRefusedError` unless ``url`` is safe to request.

    Called before every connection, including each hop of a redirect chain: a
    public URL that answers ``302 Location: http://169.254.169.254/`` is the
    same attack with one more step, and a check that only ran on the first URL
    would not see it.

    Which of the two rules a *name* is judged by depends on ``proxied``; an
    address literal is judged the same way in both, because a literal is already
    the destination and no resolver stands between this check and the connection.
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
        if proxied(url):
            # The proxy resolves this one, so our resolver has no standing to
            # judge it -- and on a fake-IP proxy it would refuse every name on
            # the strength of a placeholder. Judge the name and stop here; there
            # is deliberately no fallback to resolution, since a resolver whose
            # answer is not the destination gives a *wrong* answer, not a
            # second opinion.
            assert_permitted_name(host)
            return
        candidates = await resolve(host)
    else:
        # Already an address; there is nothing to resolve and nothing a
        # resolver could be asked to change its mind about. Judged identically
        # in both branches -- this is the line that keeps
        # `http://169.254.169.254/` refused with a proxy in front.
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
    "ProxyRouting",
    "assert_permitted_name",
    "assert_public_destination",
    "is_public_address",
    "resolve_addresses",
    "routes_through_proxy",
]
