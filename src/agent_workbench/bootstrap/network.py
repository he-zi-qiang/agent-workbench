"""Deciding whether a bind address can be reached from another machine.

This is a separate module because two layers need the same answer and neither
may import the other: settings validation rejects a bad address before a
process starts, and process assembly rejects one that reached it another way.
A rule enforced in one place is a rule that holds until somebody builds the
config object differently.

The question asked here is deliberately narrow. It is not "is this host name
ours" but "will binding to this refuse connections from off-box", which is why
a name that is not ``localhost`` is rejected rather than resolved: resolution
can succeed at validation time and mean something else at bind time, and the
safe answer to "I am not sure" is no.
"""

from __future__ import annotations

from ipaddress import ip_address

# RFC 6761 reserves this name for the loopback interface, so it is the one
# hostname whose meaning does not depend on the resolver.
LOOPBACK_HOSTNAME = "localhost"


def is_loopback_bind_address(host: str) -> bool:
    """Report whether binding to ``host`` is unreachable from other machines.

    Wildcards (``0.0.0.0``, ``::``) are not loopback: they bind every
    interface, which is the case this exists to catch.
    """

    candidate = host.strip()
    if candidate == LOOPBACK_HOSTNAME:
        return True
    try:
        # Bracketed IPv6 is how a host:port string carries it; accept both.
        return ip_address(candidate.removeprefix("[").removesuffix("]")).is_loopback
    except ValueError:
        return False


__all__ = ["LOOPBACK_HOSTNAME", "is_loopback_bind_address"]
