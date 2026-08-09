"""Where this process will and will not send a request (ADR-027 §3.2).

Every refusal is paired with the accepted form of the same thing. That pairing
is the whole discipline here: a guard that refuses everything satisfies each
"this was blocked" assertion on its own, and would be indistinguishable from a
working one until the day it silently stopped every fetch.

The resolver is injected throughout. A test that had to reach a real DNS to
prove a refusal is a test that fails offline for reasons unrelated to what it
checks -- and one that would quietly stop testing anything the day the name it
used started resolving somewhere else.
"""

from __future__ import annotations

import asyncio
import ipaddress

import pytest

from agent_workbench.adapters.research.address_guard import (
    DestinationRefusedError,
    assert_public_destination,
    is_public_address,
)

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


def _resolver(*addresses: str):
    async def resolve(host: str) -> tuple[str, ...]:
        del host
        return addresses

    return resolve


def _check(url: str, *addresses: str) -> None:
    asyncio.run(assert_public_destination(url, resolve=_resolver(*addresses)))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "0.1.2.3",
        "100.64.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",
        "ff02::1",
        "::",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:10.0.0.1",
        "2002:7f00:1::1",
        "2002:0a00:0001::1",
    ],
    ids=str,
)
def test_a_name_resolving_to_a_non_public_address_is_refused(address: str) -> None:
    with pytest.raises(DestinationRefusedError):
        _check("https://research.example/doc", address)


@pytest.mark.parametrize(
    "address",
    [PUBLIC_V4, PUBLIC_V6, "8.8.8.8", "1.1.1.1", "::ffff:8.8.8.8"],
    ids=str,
)
def test_a_name_resolving_to_a_public_address_is_permitted(address: str) -> None:
    """The control group, and the reason the refusals above mean anything."""

    _check("https://research.example/doc", address)


def test_the_cloud_metadata_address_is_refused_in_both_families() -> None:
    """Named on its own because it is the one an injected instruction aims at.

    169.254.169.254 is the IMDS address on every major cloud; `fd00:ec2::254`
    is EC2's IPv6 form. Both are covered by the general rule, and both get a
    case here so a change that reopened either fails by name rather than as one
    line of a parametrised list.
    """

    for address in ("169.254.169.254", "fd00:ec2::254", "::ffff:169.254.169.254"):
        with pytest.raises(DestinationRefusedError):
            _check("http://metadata.internal/latest/meta-data/", address)


def test_a_host_is_judged_by_its_worst_address() -> None:
    """Nothing here decides which answer the client will connect to.

    A name that returns one public and one private address is a name that can
    land either way, so the public answer must not launder the private one.
    """

    with pytest.raises(DestinationRefusedError):
        _check("https://research.example/doc", PUBLIC_V4, "10.0.0.1")

    # Control: several answers, all public, is an ordinary CDN and is allowed.
    _check("https://research.example/doc", PUBLIC_V4, "1.1.1.1", PUBLIC_V6)


def test_a_literal_address_in_the_url_is_judged_without_resolving() -> None:
    """There is nothing to resolve and nothing a resolver could be asked.

    The resolver here answers "public" for anything; if a literal host went
    through it, the private literal below would be accepted.
    """

    with pytest.raises(DestinationRefusedError):
        _check("http://169.254.169.254/latest/meta-data/", PUBLIC_V4)
    with pytest.raises(DestinationRefusedError):
        _check("http://[::1]:8000/admin", PUBLIC_V4)

    _check(f"https://{PUBLIC_V4}/doc", "10.0.0.1")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://research.example/doc",
        "gopher://research.example/",
        "https://",
        "not a url at all",
    ],
    ids=str,
)
def test_a_scheme_or_host_this_process_will_not_request_is_refused(url: str) -> None:
    with pytest.raises(DestinationRefusedError):
        _check(url, PUBLIC_V4)


def test_both_http_and_https_are_permitted() -> None:
    """The control for the scheme refusals.

    Plain HTTP stays allowed: this is a network-destination check, not a
    transport-security policy, and refusing it here would silently drop a class
    of real sources under the wrong justification.
    """

    _check("https://research.example/doc", PUBLIC_V4)
    _check("http://research.example/doc", PUBLIC_V4)


def test_a_name_that_does_not_resolve_is_refused_rather_than_attempted() -> None:
    with pytest.raises(DestinationRefusedError):
        _check("https://research.example/doc")


def test_the_message_never_names_the_address_it_refused() -> None:
    """It reaches operator logs and, through a tool result, the model.

    Which internal range a guess landed in is the one detail worth withholding
    from something that may be relaying a prompt injection's questions.
    """

    with pytest.raises(DestinationRefusedError) as refused:
        _check("https://research.example/doc", "10.11.12.13")

    assert "10.11.12.13" not in str(refused.value)
    assert "research.example" in str(refused.value)


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("::ffff:127.0.0.1", False),
        ("224.0.0.1", False),
        ("ff02::1", False),
    ],
    ids=str,
)
def test_the_address_predicate_agrees_with_the_url_check(
    address: str, public: bool
) -> None:
    """Multicast is the case worth spelling out.

    `ipaddress` reports `224.0.0.1` and `ff02::1` as `is_global`, so a check
    written as "is_global" alone would admit both. Neither is a destination for
    a TCP request.
    """

    assert is_public_address(ipaddress.ip_address(address)) is public
