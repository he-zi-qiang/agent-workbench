"""What the browser proxy refuses (ADR-0112 §3.1, §3.2).

These are the boundary's tests, not the browser's. Every case here is a
destination judged without a Chromium anywhere near it, because the guarantee
has to hold for whatever the *page* asks for and a page can ask for anything.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.apps.browser_mcp.proxy import (
    AllowedHost,
    GuardedProxy,
    ProxyRefusal,
    _to_origin_form,
    parse_allowed,
)


def _resolver(**answers: tuple[str, ...]):
    """A resolver that answers from a table and refuses to be surprised."""

    async def resolve(host: str) -> tuple[str, ...]:
        try:
            return answers[host]
        except KeyError as error:  # pragma: no cover - unseeded host
            raise AssertionError(
                f"test resolved an unexpected host: {host!r}"
            ) from error

    return resolve


def _decide(proxy: GuardedProxy, host: str, port: int = 443) -> str:
    return asyncio.run(proxy.destination_for(host, port))


def _refusal(proxy: GuardedProxy, host: str, port: int = 443) -> str:
    with pytest.raises(ProxyRefusal) as caught:
        _decide(proxy, host, port)
    return caught.value.reason


# -- the destinations that made this module exist --------------------------


def test_the_metadata_endpoint_is_refused_as_a_literal() -> None:
    """The one a page reaches for, and it never gets resolved at all."""

    proxy = GuardedProxy(resolve=_resolver())
    assert "not publicly routable" in _refusal(proxy, "169.254.169.254", 80)


def test_a_public_name_that_resolves_inward_is_refused() -> None:
    """Rebinding's first half: the name is fine, the answer is not.

    This is the case a hostname allowlist cannot catch and the reason the
    judgement is on addresses.
    """

    proxy = GuardedProxy(resolve=_resolver(**{"evil.example": ("169.254.169.254",)}))
    assert "169.254.169.254" in _refusal(proxy, "evil.example")


def test_one_private_answer_among_public_ones_refuses_the_whole_name() -> None:
    """Judged by its worst answer, because nothing here picks which is used."""

    proxy = GuardedProxy(
        resolve=_resolver(**{"mixed.example": ("93.184.216.34", "10.0.0.5")})
    )
    assert "10.0.0.5" in _refusal(proxy, "mixed.example")


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "100.64.0.1",  # carrier-grade NAT
        "::ffff:127.0.0.1",  # v4-mapped loopback
    ],
)
def test_internal_literals_are_refused(host: str) -> None:
    proxy = GuardedProxy(resolve=_resolver())
    assert _refusal(proxy, host, 80)


def test_a_public_address_is_dialled_by_the_address_that_was_judged() -> None:
    """The return value is an address, never the name.

    Handing the name back would give the resolution to somebody else, which is
    precisely the rebinding window this proxy exists to close.
    """

    proxy = GuardedProxy(resolve=_resolver(**{"example.com": ("93.184.216.34",)}))
    assert _decide(proxy, "example.com") == "93.184.216.34"


# -- the operator's list ----------------------------------------------------


def test_an_operator_named_internal_host_is_allowed() -> None:
    proxy = GuardedProxy(
        allowed=parse_allowed(["api:8000"]),
        resolve=_resolver(api=("172.18.0.4",)),
    )
    assert _decide(proxy, "api", 8000) == "172.18.0.4"


def test_the_list_is_host_and_port_together() -> None:
    """Naming `api:8000` must not also open whatever answers on 5432."""

    proxy = GuardedProxy(
        allowed=parse_allowed(["api:8000"]),
        resolve=_resolver(api=("172.18.0.4",)),
    )
    assert "not publicly routable" in _refusal(proxy, "api", 5432)


def test_a_malformed_allowlist_entry_is_refused_at_parse_time() -> None:
    """Before any request arrives, so a typo is a start-up failure."""

    with pytest.raises(ValueError, match="host:port"):
        parse_allowed(["api"])


def test_nothing_in_a_request_can_extend_the_list() -> None:
    """The list is a frozenset built at construction; this pins that shape."""

    proxy = GuardedProxy(allowed=parse_allowed(["api:8000"]), resolve=_resolver())
    assert isinstance(proxy.allowed, frozenset)
    assert AllowedHost(host="api", port=8000) in proxy.allowed


# -- protocol handling ------------------------------------------------------


def test_an_absolute_uri_is_rewritten_to_origin_form_keeping_the_host_header() -> None:
    """The other half of "connect to what was judged": the Host header stands."""

    head = (
        b"GET http://example.com/a/b?c=d HTTP/1.1\r\n"
        b"Host: example.com\r\n"
        b"Accept: */*\r\n\r\n"
    )
    rewritten = _to_origin_form(head)
    assert rewritten.startswith(b"GET /a/b?c=d HTTP/1.1\r\n")
    assert b"Host: example.com\r\n" in rewritten


def test_a_path_less_uri_becomes_a_root_request() -> None:
    assert _to_origin_form(
        b"GET http://example.com HTTP/1.1\r\nHost: x\r\n\r\n"
    ).startswith(b"GET / HTTP/1.1")


def test_a_scheme_the_proxy_does_not_forward_is_refused() -> None:
    proxy = GuardedProxy(resolve=_resolver())
    with pytest.raises(ProxyRefusal, match="not a scheme"):
        asyncio.run(_open(proxy, "GET ftp://example.com/x HTTP/1.1"))


async def _open(proxy: GuardedProxy, request_line: str) -> None:
    from agent_workbench.apps.browser_mcp.proxy import _destination_of

    method, target, _ = request_line.split(" ", 2)
    host, port = _destination_of(method, target)
    await proxy.destination_for(host, port)


def test_a_host_that_does_not_resolve_is_a_bad_gateway_not_a_refusal() -> None:
    """A broken name and a forbidden one are different answers to the model."""

    async def resolve(host: str) -> tuple[str, ...]:
        del host
        return ()

    proxy = GuardedProxy(resolve=resolve)
    with pytest.raises(ProxyRefusal) as caught:
        _decide(proxy, "nowhere.example")
    assert caught.value.status == 502


# -- what the diagnostics tool reads ---------------------------------------


def test_every_decision_is_recorded_with_its_verdict() -> None:
    proxy = GuardedProxy(
        resolve=_resolver(**{"example.com": ("93.184.216.34",)}),
    )
    _decide(proxy, "example.com")
    proxy._record("GET", "example.com", 443, True, "connect 93.184.216.34:443")
    with pytest.raises(ProxyRefusal):
        _decide(proxy, "169.254.169.254", 80)
    proxy._record("GET", "169.254.169.254", 80, False, "refused")

    decisions, dropped = proxy.drain_decisions()
    assert [d.allowed for d in decisions] == [True, False]
    assert dropped == 0
    assert proxy.drain_decisions() == ((), 0)


def test_the_decision_window_reports_what_fell_out_of_it() -> None:
    """A page issues hundreds of requests; a silent truncation would lie."""

    proxy = GuardedProxy(resolve=_resolver())
    for index in range(600):
        proxy._record("GET", f"h{index}", 443, True, "x")
    decisions, dropped = proxy.drain_decisions()
    assert len(decisions) == 512
    assert dropped == 88


# -- the transport, over real sockets ---------------------------------------
#
# The cases above judge destinations; these drive `handle` itself, because a
# guard that decides correctly and then forwards anyway is not a guard.


async def _serve(proxy: GuardedProxy) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _echo_upstream() -> tuple[asyncio.Server, int]:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        data = await reader.read(1024)
        writer.write(b"UPSTREAM-SAW:" + data.split(b"\r\n")[0])
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_a_refused_connect_never_reaches_the_upstream() -> None:
    async def scenario() -> bytes:
        proxy = GuardedProxy(resolve=_resolver())
        server, port = await _serve(proxy)
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n")
            await writer.drain()
            answer = await reader.read(4096)
            writer.close()
            return answer

    answer = asyncio.run(scenario())
    assert answer.startswith(b"HTTP/1.1 403")
    # The body says why, so a refused subresource is not just a network error
    # in the page's console.
    assert b"not publicly routable" in answer


def test_an_allowed_plain_request_is_forwarded_in_origin_form() -> None:
    async def scenario() -> bytes:
        upstream, upstream_port = await _echo_upstream()
        proxy = GuardedProxy(
            allowed=parse_allowed([f"127.0.0.1:{upstream_port}"]),
            resolve=_resolver(**{"127.0.0.1": ("127.0.0.1",)}),
        )
        server, port = await _serve(proxy)
        async with upstream, server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                f"GET http://127.0.0.1:{upstream_port}/probe HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{upstream_port}\r\n\r\n".encode()
            )
            await writer.drain()
            answer = await reader.read(4096)
            writer.close()
            return answer

    assert asyncio.run(scenario()) == b"UPSTREAM-SAW:GET /probe HTTP/1.1"


def test_an_allowed_connect_opens_a_tunnel_that_carries_bytes() -> None:
    async def scenario() -> tuple[bytes, bytes]:
        upstream, upstream_port = await _echo_upstream()
        proxy = GuardedProxy(
            allowed=parse_allowed([f"127.0.0.1:{upstream_port}"]),
            resolve=_resolver(**{"127.0.0.1": ("127.0.0.1",)}),
        )
        server, port = await _serve(proxy)
        async with upstream, server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            established = await reader.readuntil(b"\r\n\r\n")
            # Opaque from here on: whatever goes in comes out the far end
            # unread by this process. That is the CONNECT boundary ADR-0112
            # §3.2 states rather than implies.
            writer.write(b"TUNNELLED PAYLOAD\r\n")
            await writer.drain()
            body = await reader.read(4096)
            writer.close()
            return established, body

    established, body = asyncio.run(scenario())
    assert established.startswith(b"HTTP/1.1 200")
    assert body == b"UPSTREAM-SAW:TUNNELLED PAYLOAD"


# -- the branch a developer machine takes (ADR-0112 §3.2) -------------------
#
# Measured on 2026-09-12: with a fake-IP/TUN proxy in front, `example.com`
# resolved to 198.18.0.70 and resolve-then-judge refused every site on the
# strength of a placeholder. These pin the branch that fixes it *and* the parts
# of the rule it must not relax.


def _proxied() -> GuardedProxy:
    from agent_workbench.apps.browser_mcp.proxy import Upstream

    return GuardedProxy(
        upstream=Upstream(host="127.0.0.1", port=1082),
        # Seeded to fail loudly: nothing on this branch may resolve a name.
        resolve=_resolver(),
    )


def test_a_public_name_is_judged_without_being_resolved() -> None:
    """The whole point: our resolver is not the authority on the destination.

    The resolver here refuses every host, so a test that passes has not called
    it -- which is the assertion, since calling it is what produced the
    nineteen-out-of-nineteen refusals.
    """

    assert _decide(_proxied(), "example.com") == "example.com"


def test_a_literal_is_still_judged_as_an_address_behind_a_proxy() -> None:
    """The line that keeps the metadata endpoint refused with a proxy in front.

    A literal is already the destination; no resolver stands between the check
    and the connection, so the proxy branch buys it nothing and must not
    exempt it.
    """

    assert "not publicly routable" in _refusal(_proxied(), "169.254.169.254", 80)


@pytest.mark.parametrize(
    ("host", "because"),
    [
        ("localhost", "never leaves this host"),
        ("db", "no domain"),  # single-label: a search-domain lookup
        ("service.internal", "reserved for private networks"),
    ],
)
def test_the_name_rule_still_refuses_what_is_internal_by_construction(
    host: str, because: str
) -> None:
    assert because in _refusal(_proxied(), host)


def test_an_operator_named_host_is_still_dialled_directly_behind_a_proxy() -> None:
    """`--allow-host` names something on *this* side of the proxy.

    Handing `api:8000` to an upstream proxy would send it somewhere that has
    never heard of it, so the allowlist keeps the direct branch.
    """

    from agent_workbench.apps.browser_mcp.proxy import Upstream

    proxy = GuardedProxy(
        upstream=Upstream(host="127.0.0.1", port=1082),
        allowed=parse_allowed(["api:8000"]),
        resolve=_resolver(api=("172.18.0.4",)),
    )
    assert _decide(proxy, "api", 8000) == "172.18.0.4"


def test_the_upstream_url_is_parsed_or_refused() -> None:
    from agent_workbench.apps.browser_mcp.proxy import Upstream

    assert Upstream.from_url("http://127.0.0.1:1082") == Upstream("127.0.0.1", 1082)
    # Bare host:port, as proxy variables are often written.
    assert Upstream.from_url("127.0.0.1:1082") == Upstream("127.0.0.1", 1082)
    with pytest.raises(ValueError, match="does not name a proxy host"):
        Upstream.from_url("http://")


def test_a_proxied_request_is_handed_over_with_its_head_unrewritten() -> None:
    """A proxy speaking to a proxy sends the same two forms it received.

    Rewriting the absolute URI into origin form here is how a double-proxy
    chain usually breaks: the upstream would get a path with no host and have
    nowhere to send it.
    """

    async def scenario() -> bytes:
        from agent_workbench.apps.browser_mcp.proxy import Upstream

        upstream, upstream_port = await _echo_upstream()
        proxy = GuardedProxy(
            upstream=Upstream(host="127.0.0.1", port=upstream_port),
            resolve=_resolver(),
        )
        server, port = await _serve(proxy)
        async with upstream, server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET http://example.com/probe HTTP/1.1\r\nHost: example.com\r\n\r\n"
            )
            await writer.drain()
            answer = await reader.read(4096)
            writer.close()
            return answer

    assert (
        asyncio.run(scenario()) == b"UPSTREAM-SAW:GET http://example.com/probe HTTP/1.1"
    )
