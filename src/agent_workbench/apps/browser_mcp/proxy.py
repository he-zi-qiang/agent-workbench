"""The only way out of the browser container (ADR-0113 §3.1, §3.2).

**Why the guard is here and not in the tool.** The first design checked the URL
inside ``browser_open`` and handed it to Chromium. That check is worth nothing.
The model approves one URL; everything after the page loads is issued by the
*page* -- subresources, ``fetch``/XHR, WebSocket upgrades, ``<img src>``, meta
refresh, 301 hops, ``window.location`` -- and the page was never asked. One line
of JavaScript on an approved origin reaches ``169.254.169.254`` without the tool
layer seeing a thing.

So the judgement moved to the one layer every request must cross. Chromium runs
with ``--proxy-server`` pointed here and no bypass list, in a container that has
no default route (§3.3): this is not a check in front of the network, it is the
network.

**Resolve once, judge that, connect to that.** ``address_guard`` states plainly
that it cannot close DNS rebinding -- the name is resolved to judge it and
resolved again by the client to connect, and an answer that changes between
those two moments wins. Closing it needs "connect to the address that was
checked, and carry the hostname in the Host header", which is what a proxy does
by construction. So this module resolves, judges the addresses it got, and dials
*that address*. The browser never resolves anything; it only talks to us. On
this path rebinding is closed, and that is a stronger guarantee than the one
`web_mcp` can make, not a weaker one.

**What it deliberately does not do.** A ``CONNECT`` tunnel is opaque: the
decision is made about the destination, never about the bytes. Reading them
would mean terminating TLS for every site the model visits, which is a different
program with a different threat model. The boundary here is *where a connection
may land*, and it says so rather than implying more.

**The allowlist is for the deployment, never for the model.** Public addresses
are judged by ``is_public_address`` and need no list. Anything internal -- a dev
server in the same Compose project, say -- is refused unless an operator named
it on the command line. Nothing in an MCP request can add to that list, because
the list arrives before any request does.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Protocol
from urllib.parse import urlsplit

from agent_workbench.adapters.research.address_guard import (
    AddressResolver,
    DestinationRefusedError,
    assert_permitted_name,
    is_public_address,
    resolve_addresses,
)

#: One request line plus headers. Chromium's are far below this; a peer that
#: sends more before the blank line is not speaking HTTP to a proxy.
MAX_HEAD_BYTES: Final[int] = 64 * 1024
#: Read granularity for the tunnel. Same value `loopback_proxy.py` uses.
CHUNK_BYTES: Final[int] = 65_536
#: How long a peer may take to send its request line and headers.
HEAD_TIMEOUT_SECONDS: Final[float] = 20.0
#: How long the upstream connect may take before the request is failed.
CONNECT_TIMEOUT_SECONDS: Final[float] = 20.0
#: Decisions kept for `browser_diagnostics`. A page can issue hundreds of
#: requests; this is a window, and the drop count is reported rather than
#: hidden -- a diagnostic that silently truncates is one that lies.
DECISION_LOG_SIZE: Final[int] = 512

_SCHEME_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}


class ProxyRefusal(Exception):
    """A request this proxy will not forward, with the reason the model sees."""

    def __init__(self, reason: str, *, status: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True, slots=True)
class Decision:
    """One judged destination, for the diagnostics tool."""

    at: float
    method: str
    host: str
    port: int
    allowed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class AllowedHost:
    """One internal destination an operator named explicitly.

    Host *and* port: naming ``api`` should not also open its database port if
    the same name happens to answer on one.
    """

    host: str
    port: int

    @classmethod
    def parse(cls, entry: str) -> AllowedHost:
        host, separator, port = entry.rpartition(":")
        if not separator or not host:
            raise ValueError(f"{entry!r} must be given as host:port")
        try:
            number = int(port)
        except ValueError as error:
            raise ValueError(f"{entry!r} has a port that is not a number") from error
        if not 1 <= number <= 65_535:
            raise ValueError(f"{entry!r} has a port outside 1-65535")
        return cls(host=host.lower().rstrip("."), port=number)


@dataclass(frozen=True, slots=True)
class Upstream:
    """A forward proxy this one hands judged requests to.

    **Why this exists at all**, because it weakens the guarantee and should not
    be reached for casually. `address_guard` states the problem in full: on a
    machine running a fake-IP/TUN proxy every hostname resolves into
    `198.18.0.0/15`, a range that is correctly *not* globally routable, and
    resolve-then-judge then refuses every site on the strength of a placeholder
    the proxy hands out and later translates back. Measured here on 2026-09-12:
    `example.com` resolved to `198.18.0.70` and the browser could open nothing.

    So the question asked first is the one that module asks -- *who resolves
    this connection* -- and the answer decides the rule. When an upstream proxy
    does the resolving, the name is judged instead, and this process forwards
    to the upstream rather than dialling an address it has no standing to
    choose.

    **What that costs, stated rather than implied: on this branch the
    enforcement boundary is the upstream proxy, not this process.** Judging the
    name catches `localhost`, single-label names and the private-use suffixes;
    it cannot catch a public name that the upstream resolves inward, and DNS
    rebinding is not closed here the way it is on the direct branch.

    **The container path never takes this branch.** Nothing in the Compose
    topology sets a proxy variable, and `browser-egress` reaches the network
    itself -- so ADR-0113 §3.2's stronger claim holds exactly where it was
    claimed, and this is the developer machine's weaker sibling.
    """

    host: str
    port: int

    @classmethod
    def from_url(cls, url: str) -> Upstream:
        parts = urlsplit(url if "://" in url else f"http://{url}")
        if parts.hostname is None:
            raise ValueError(f"{url!r} does not name a proxy host")
        return cls(host=parts.hostname, port=parts.port or 8080)


@dataclass
class GuardedProxy:
    """An HTTP/HTTPS forward proxy that judges every destination."""

    allowed: frozenset[AllowedHost] = frozenset()
    resolve: AddressResolver = resolve_addresses
    #: Set when this machine reaches the internet through another proxy.
    upstream: Upstream | None = None
    decisions: deque[Decision] = field(
        default_factory=lambda: deque(maxlen=DECISION_LOG_SIZE)
    )
    dropped: int = 0

    # -- judgement ---------------------------------------------------------

    async def destination_for(self, host: str, port: int) -> str:
        """The address a connection to ``host:port`` may use, or raise.

        Returns the *address*, not the name, because the caller must dial what
        was judged. Returning the name would hand the resolution back to
        somebody else and reopen exactly what the module docstring closes.
        """

        name = host.lower().rstrip(".")
        if not name:
            raise ProxyRefusal("the request names no host")

        if (
            self.upstream is not None
            and AllowedHost(host=name, port=port) not in self.allowed
        ):
            # The upstream resolves this, so our resolver is not the authority
            # on where it lands; judging its answer is what refused nineteen
            # reachable pages out of nineteen. Judge the name and hand it over.
            # An address literal still goes through the rule below, which is
            # what keeps `http://169.254.169.254/` refused with a proxy in
            # front -- a literal is already the destination.
            try:
                ipaddress.ip_address(name)
            except ValueError:
                try:
                    assert_permitted_name(name)
                except DestinationRefusedError as error:
                    raise ProxyRefusal(str(error)) from error
                return name

        if AllowedHost(host=name, port=port) in self.allowed:
            # Named by an operator, so it is judged by that naming and not by
            # routability -- the whole point of the list is destinations that
            # are *not* publicly routable. Still resolved here, so the dial
            # below uses an address like every other path.
            candidates = await self._resolve_or_refuse(name)
            return candidates[0]

        try:
            literal = ipaddress.ip_address(name)
        except ValueError:
            candidates = await self._resolve_or_refuse(name)
        else:
            candidates = (str(literal),)

        # Judged by its worst answer, for the reason `address_guard` gives:
        # nothing here chooses which address a later connection would use, so
        # one private answer among public ones refuses the whole request.
        for candidate in candidates:
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError as error:
                raise ProxyRefusal(
                    f"{host!r} resolved to something that is not an address"
                ) from error
            if not is_public_address(address):
                raise ProxyRefusal(
                    f"{host!r} resolves to {candidate}, which is not publicly "
                    "routable and was not named as a permitted internal host"
                )
        return candidates[0]

    async def _resolve_or_refuse(self, name: str) -> tuple[str, ...]:
        try:
            candidates = await self.resolve(name)
        except DestinationRefusedError as error:
            raise ProxyRefusal(str(error)) from error
        except OSError as error:
            raise ProxyRefusal(f"{name!r} does not resolve", status=502) from error
        if not candidates:
            raise ProxyRefusal(f"{name!r} does not resolve", status=502)
        return tuple(candidates)

    def _record(
        self, method: str, host: str, port: int, allowed: bool, detail: str
    ) -> None:
        if len(self.decisions) == self.decisions.maxlen:
            self.dropped += 1
        self.decisions.append(
            Decision(
                at=time.time(),
                method=method,
                host=host,
                port=port,
                allowed=allowed,
                detail=detail,
            )
        )

    def drain_decisions(self) -> tuple[tuple[Decision, ...], int]:
        """Take the window and the count of what fell out of it."""

        taken = tuple(self.decisions)
        dropped = self.dropped
        self.decisions.clear()
        self.dropped = 0
        return taken, dropped

    # -- transport ---------------------------------------------------------

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one client connection."""

        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS
            )
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            await _close(writer)
            return
        except asyncio.LimitOverrunError:
            await _refuse(writer, ProxyRefusal("request head too large", status=431))
            return
        if len(head) > MAX_HEAD_BYTES:
            await _refuse(writer, ProxyRefusal("request head too large", status=431))
            return

        try:
            method, target, _ = (
                head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
            )
        except ValueError:
            await _refuse(writer, ProxyRefusal("malformed request line", status=400))
            return

        try:
            host, port = _destination_of(method, target)
            address = await self.destination_for(host, port)
        except ProxyRefusal as refusal:
            self._record(
                method, host_of(target), port_of(target), False, refusal.reason
            )
            await _refuse(writer, refusal)
            return

        via = self.upstream
        dial = (via.host, via.port) if via is not None else (address, port)
        self._record(
            method,
            host,
            port,
            True,
            f"connect {address}:{port}"
            if via is None
            else f"via proxy {via.host}:{via.port}",
        )
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(*dial), CONNECT_TIMEOUT_SECONDS
            )
        except (TimeoutError, OSError) as error:
            await _refuse(
                writer, ProxyRefusal(f"upstream connect failed: {error}", status=502)
            )
            return

        try:
            if via is not None:
                # Hand the request over exactly as it arrived. A proxy speaking
                # to a proxy sends the same two forms -- `CONNECT host:port` or
                # an absolute URI -- so the head needs no rewriting at all, and
                # rewriting it is how a double-proxy chain usually breaks.
                upstream_writer.write(head)
                await upstream_writer.drain()
            elif method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                # A plain request arrives with an absolute URI; origin servers
                # want the path. The Host header is left exactly as the browser
                # wrote it, which is the half of "connect to what was judged"
                # that makes a virtual host still answer correctly.
                upstream_writer.write(_to_origin_form(head))
                await upstream_writer.drain()
            await _pump(reader, writer, upstream_reader, upstream_writer)
        finally:
            await _close(upstream_writer)
            await _close(writer)


def host_of(target: str) -> str:
    """Best-effort host for a *refused* request, so the log line is readable."""

    with contextlib.suppress(Exception):
        return _destination_of("GET", target)[0]
    return target[:80]


def port_of(target: str) -> int:
    with contextlib.suppress(Exception):
        return _destination_of("GET", target)[1]
    return 0


def _destination_of(method: str, target: str) -> tuple[str, int]:
    """Host and port a proxy request names, by its two legal forms."""

    if method.upper() == "CONNECT":
        host, separator, port = target.rpartition(":")
        if not separator:
            raise ProxyRefusal("CONNECT must name host:port", status=400)
        try:
            number = int(port)
        except ValueError as error:
            raise ProxyRefusal("CONNECT port is not a number", status=400) from error
        return host.strip("[]"), number

    parts = urlsplit(target)
    scheme = parts.scheme.lower()
    if scheme not in _SCHEME_PORTS:
        raise ProxyRefusal(
            f"{scheme or target[:40]!r} is not a scheme this proxy forwards"
        )
    if not parts.hostname:
        raise ProxyRefusal("the request names no host", status=400)
    return parts.hostname, parts.port or _SCHEME_PORTS[scheme]


def _to_origin_form(head: bytes) -> bytes:
    """Rewrite the absolute-URI request line into origin form."""

    line, _, rest = head.partition(b"\r\n")
    method, target, version = line.split(b" ", 2)
    parts = urlsplit(target.decode("latin-1"))
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return b" ".join([method, path.encode("latin-1"), version]) + b"\r\n" + rest


async def _pump(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    async def copy(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        try:
            while data := await source.read(CHUNK_BYTES):
                sink.write(data)
                await sink.drain()
        except (ConnectionError, OSError):
            return
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                sink.write_eof()

    await asyncio.gather(
        copy(client_reader, upstream_writer),
        copy(upstream_reader, client_writer),
        return_exceptions=True,
    )


async def _refuse(writer: asyncio.StreamWriter, refusal: ProxyRefusal) -> None:
    """Answer with the reason, in a form the browser will show and a log keeps.

    The body matters: a refused subresource is otherwise indistinguishable from
    a network error in the console, and the model would be debugging the wrong
    thing.
    """

    body = (
        f"agent-workbench browser proxy refused this request.\n\n{refusal.reason}\n"
    ).encode()
    writer.write(
        b"HTTP/1.1 %d Forbidden\r\nContent-Type: text/plain; charset=utf-8\r\n"
        b"Content-Length: %d\r\nConnection: close\r\n\r\n%s"
        % (refusal.status, len(body), body)
    )
    with contextlib.suppress(ConnectionError, OSError):
        await writer.drain()
    await _close(writer)


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(ConnectionError, OSError):
        writer.close()
        await writer.wait_closed()


def parse_allowed(entries: Iterable[str]) -> frozenset[AllowedHost]:
    """Build the operator's allowlist, refusing anything malformed loudly."""

    return frozenset(AllowedHost.parse(entry) for entry in entries if entry.strip())


# -- where `browser_diagnostics` gets its decisions from --------------------
#
# Two topologies, one question. Natively the proxy is in this process and the
# answer is a method call; under Compose it is in the egress container and the
# answer is an HTTP read that can fail. The failure is modelled rather than
# flattened: `None` means "could not find out", which is not the same answer as
# an empty tuple, and the tool says so in those words.


class DecisionSource(Protocol):
    """Where the refused destinations come from."""

    async def take(self) -> tuple[tuple[Decision, ...], int] | None: ...


@dataclass(frozen=True, slots=True)
class LocalDecisions:
    """The proxy is in this process (the native path)."""

    proxy: GuardedProxy

    async def take(self) -> tuple[tuple[Decision, ...], int] | None:
        return self.proxy.drain_decisions()


@dataclass(frozen=True, slots=True)
class RemoteDecisions:
    """The proxy is the egress container (ADR-0113 §3.3)."""

    url: str
    timeout_seconds: float = 5.0

    async def take(self) -> tuple[tuple[Decision, ...], int] | None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return None
        entries = tuple(
            Decision(
                at=float(entry.get("at", 0.0)),
                method=str(entry.get("method", "")),
                host=str(entry.get("host", "")),
                port=int(entry.get("port", 0)),
                allowed=bool(entry.get("allowed", False)),
                detail=str(entry.get("detail", "")),
            )
            for entry in payload.get("decisions", ())
        )
        return entries, int(payload.get("dropped", 0))


__all__ = [
    "AllowedHost",
    "Decision",
    "DecisionSource",
    "GuardedProxy",
    "LocalDecisions",
    "ProxyRefusal",
    "RemoteDecisions",
    "parse_allowed",
]
