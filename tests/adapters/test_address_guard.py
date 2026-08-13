"""Where this process will and will not send a request (ADR-027 §3.2).

Every refusal is paired with the accepted form of the same thing. That pairing
is the whole discipline here: a guard that refuses everything satisfies each
"this was blocked" assertion on its own, and would be indistinguishable from a
working one until the day it silently stopped every fetch.

The resolver is injected throughout. A test that had to reach a real DNS to
prove a refusal is a test that fails offline for reasons unrelated to what it
checks -- and one that would quietly stop testing anything the day the name it
used started resolving somewhere else.

**So is the routing branch, and for a sharper version of the same reason.**
Which rule judges a name depends on whether a proxy opens the connection, and
the default reads the ambient environment. Left to that default, every refusal
below would pass on CI -- where nothing exports ``HTTP_PROXY`` -- and fail on any
developer machine that does, because the name would take the proxied branch and
never reach the resolver at all. That is not a test suite with an environmental
quirk; it is a suite that tests a *different function* depending on the shell
that started it. Every case therefore states its branch, and the two branches
have their own sections below.
"""

from __future__ import annotations

import asyncio
import ipaddress

import pytest

from agent_workbench.adapters.research.address_guard import (
    DestinationRefusedError,
    assert_public_destination,
    is_public_address,
    routes_through_proxy,
)

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


def _resolver(*addresses: str):
    async def resolve(host: str) -> tuple[str, ...]:
        del host
        return addresses

    return resolve


def _direct(url: str) -> bool:
    """This process opens the connection, so it resolves the destination."""

    del url
    return False


def _through_a_proxy(url: str) -> bool:
    del url
    return True


def _check(url: str, *addresses: str) -> None:
    asyncio.run(
        assert_public_destination(url, resolve=_resolver(*addresses), proxied=_direct)
    )


def _check_proxied(url: str) -> None:
    """The proxied branch, with a resolver that fails the test if consulted.

    The refusals in this section have to come from the name rule. A resolver
    that quietly answered would make them indistinguishable from the address
    rule running anyway -- which is the exact confusion this branch exists to
    end.
    """

    async def refuse_to_resolve(host: str) -> tuple[str, ...]:
        raise AssertionError(f"the proxied branch resolved {host!r}")

    asyncio.run(
        assert_public_destination(
            url, resolve=refuse_to_resolve, proxied=_through_a_proxy
        )
    )


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


# --- The proxied branch: the proxy resolves, so the name is what gets judged ---


def test_a_fake_ip_answer_refuses_when_direct_and_is_ignored_when_proxied() -> None:
    """The regression this branch exists for, both halves in one place.

    A fake-IP/TUN proxy answers every name out of `198.18.0.0/15` -- RFC 2544's
    benchmarking range, correctly not globally routable -- and then translates
    it back on the way out. Judged as an address that is a refusal; judged as a
    name it is an ordinary public site, which is what `curl` on the same machine
    demonstrates by fetching it.

    The direct half is the control, and it is the important one: it pins that
    nothing here started trusting `198.18.0.0/15` itself. The range is still
    refused whenever this process is the one resolving.
    """

    with pytest.raises(DestinationRefusedError):
        _check("https://www.deepseek.com/", "198.18.0.47")

    _check_proxied("https://www.deepseek.com/")


def test_an_address_literal_is_refused_through_a_proxy_too() -> None:
    """The line that keeps the metadata service out with a proxy in front.

    A literal is already the destination, so no resolver stands between this
    check and the connection and there is nothing for a proxy to have resolved
    differently. Both branches judge it identically.
    """

    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8000/admin",
        "http://10.0.0.1/",
        "http://127.0.0.1:8000/",
    ):
        with pytest.raises(DestinationRefusedError):
            _check_proxied(url)

    # Control: a public literal is as acceptable here as it is when direct.
    _check_proxied(f"https://{PUBLIC_V4}/doc")


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost6",
        "router",
        "buildserver",
        "printer.local",
        "metadata.google.internal",
        "db.localdomain",
        "nas.home.arpa",
    ],
    ids=str,
)
def test_a_name_that_is_internal_by_construction_is_refused_when_proxied(
    host: str,
) -> None:
    """What the name rule can decide without a resolver.

    Single-label names are in here because a name with no dot is a search-domain
    lookup: it resolves inside whatever network answers it and never on the
    public internet, so asking a proxy to reach one is asking it to reach into
    its own LAN.
    """

    with pytest.raises(DestinationRefusedError):
        _check_proxied(f"http://{host}/")


@pytest.mark.parametrize(
    "host",
    ["research.example", "www.deepseek.com", "en.wikipedia.org", "weather.com.cn"],
    ids=str,
)
def test_an_ordinary_public_name_is_permitted_when_proxied(host: str) -> None:
    """The control for the refusals above.

    Without it, a name rule that refused everything would satisfy every
    assertion in this section and be indistinguishable from a working one --
    which is precisely the failure that made this branch necessary.
    """

    _check_proxied(f"https://{host}/some/page")


def test_the_proxied_branch_states_its_limit_rather_than_implying_coverage() -> None:
    """A site-specific internal name is *allowed* through, and that is documented.

    Pinned as a test because it is the honest boundary of the narrower
    guarantee, and an undocumented gap that nobody wrote down is how a security
    story drifts from what the code does. `wiki.corp.example` is internal to
    somebody's network and there is no resolver-free way to know it -- the proxy's
    own egress policy is what stops it, not this module.

    If this ever starts raising, the module docstring is what needs updating
    with it; do not simply delete the test.
    """

    _check_proxied("https://wiki.corp.example/runbook")


def test_a_trailing_dot_does_not_slip_a_name_past_the_rule() -> None:
    """`localhost.` and `localhost` are the same name to a resolver."""

    for host in ("localhost.", "printer.local.", "metadata.google.internal."):
        with pytest.raises(DestinationRefusedError):
            _check_proxied(f"http://{host}/")


# --- Reading the routing decision out of the same variables the client reads ---


@pytest.fixture
def clean_proxy_env(monkeypatch: pytest.MonkeyPatch):
    """No proxy variables, whatever the shell running the suite exported.

    The suite has to state this rather than inherit it: these tests are about
    what the variables mean, so a machine that already exports one would be
    answering the question under test.
    """

    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    # macOS's `getproxies` falls back to System Configuration when the
    # environment is empty, which would make these assertions depend on the
    # machine's network panel. Pinned to the environment reader alone.
    monkeypatch.setattr(
        "agent_workbench.adapters.research.address_guard.getproxies",
        __import__("urllib.request", fromlist=["x"]).getproxies_environment,
    )
    return monkeypatch


def test_no_proxy_variables_means_this_process_resolves(clean_proxy_env) -> None:
    assert routes_through_proxy("https://research.example/doc") is False


def test_the_scheme_decides_which_variable_applies(clean_proxy_env) -> None:
    clean_proxy_env.setenv("HTTP_PROXY", "http://127.0.0.1:1082")

    assert routes_through_proxy("http://research.example/doc") is True
    # No HTTPS_PROXY and no ALL_PROXY: an https request is not proxied, so it
    # must go back to resolve-then-judge rather than inherit the http setting.
    assert routes_through_proxy("https://research.example/doc") is False


def test_all_proxy_covers_both_schemes(clean_proxy_env) -> None:
    clean_proxy_env.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")

    assert routes_through_proxy("http://research.example/doc") is True
    assert routes_through_proxy("https://research.example/doc") is True


@pytest.mark.parametrize(
    ("no_proxy", "url", "proxied"),
    [
        ("research.example", "https://research.example/doc", False),
        ("research.example", "https://api.research.example/doc", False),
        (".research.example", "https://api.research.example/doc", False),
        ("research.example", "https://research.example./doc", False),
        ("localhost,127.0.0.1,::1,.local", "http://printer.local/", False),
        ("localhost,127.0.0.1", "http://127.0.0.1:8000/", False),
        ("192.168.0.0/16", "http://192.168.4.4/", False),
        ("192.168.0.0/16", "https://research.example/doc", True),
        ("*", "https://research.example/doc", False),
        ("other.example", "https://research.example/doc", True),
    ],
    ids=str,
)
def test_no_proxy_sends_a_url_back_to_resolve_then_judge(
    clean_proxy_env, no_proxy: str, url: str, proxied: bool
) -> None:
    """Every ambiguous form resolves toward "direct", on purpose.

    The two ways to be wrong are not symmetric. Reading "proxied" when the
    client connects directly skips resolve-then-judge and is a hole; reading
    "direct" when the client proxies merely refuses a page it could have read.
    The match is therefore a superset of httpx's -- suffix as well as
    subdomain -- so it errs into the branch that checks more.
    """

    clean_proxy_env.setenv("HTTPS_PROXY", "http://127.0.0.1:1082")
    clean_proxy_env.setenv("HTTP_PROXY", "http://127.0.0.1:1082")
    clean_proxy_env.setenv("NO_PROXY", no_proxy)

    assert routes_through_proxy(url) is proxied
