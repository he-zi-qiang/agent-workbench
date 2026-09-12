"""The only container that can both be seen by the browser and reach the net.

ADR-0113 §3.3. The browser container has no default route; this one has two
networks and one job. Chromium's `--proxy-server` points here, so "the browser
only leaves through the guard" is a property of the topology rather than of a
flag somebody remembered to pass: switch this service off and the browser does
not bypass it, it reaches nothing at all.

Two listeners, deliberately different in kind:

* the **proxy** itself, which speaks HTTP/CONNECT and is what Chromium dials;
* a **read-only `/decisions`** route, which is how `browser_diagnostics` learns
  what was refused now that the judging happens in a different process.

`/decisions` is not a new trust boundary -- the containers that can read it are
exactly the containers that can already hand this proxy a request -- but it is a
cross-process read, so it has a failure mode the in-process version did not: the
browser may be unable to reach it. The tool says so in those words rather than
reporting an empty list, because "nothing was refused" and "I could not find out
what was refused" mean opposite things to a model deciding whether its page is
broken.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_workbench.apps.browser_mcp.proxy import (
    GuardedProxy,
    Upstream,
    parse_allowed,
)

#: The proxy port Chromium dials. Published to the `internal` network only.
DEFAULT_PROXY_PORT = 8771
#: The control port `/decisions` is served on.
DEFAULT_CONTROL_PORT = 8772
DECISIONS_PATH = "/decisions"
HEALTH_PATH = "/health"


def create_app(proxy: GuardedProxy) -> Starlette:
    async def decisions(request: Request) -> JSONResponse:
        """Hand over the window of judged destinations, and clear it.

        Draining rather than peeking, for the same reason the in-process
        version drained: two readers of the same window would each see the
        other's entries and neither would know it.
        """

        del request
        judged, dropped = proxy.drain_decisions()
        return JSONResponse(
            {
                "decisions": [
                    {
                        "at": decision.at,
                        "method": decision.method,
                        "host": decision.host,
                        "port": decision.port,
                        "allowed": decision.allowed,
                        "detail": decision.detail,
                    }
                    for decision in judged
                ],
                "dropped": dropped,
            }
        )

    async def health(request: Request) -> JSONResponse:
        del request
        return JSONResponse(
            {
                "status": "ok",
                "service": "agent-workbench-browser-egress",
                "allowed_internal_hosts": sorted(
                    f"{entry.host}:{entry.port}" for entry in proxy.allowed
                ),
            }
        )

    return Starlette(
        routes=[
            Route(DECISIONS_PATH, endpoint=decisions, methods=["GET"]),
            Route(HEALTH_PATH, endpoint=health, methods=["GET"]),
        ]
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-browser-egress",
        description="Run the destination guard that the browser must leave through.",
    )
    # Not a loopback choice list, unlike every other server here, and the reason
    # is the point of the service: this one exists to be dialled from another
    # container. What keeps it from being a hole is that it forwards nothing it
    # has not judged -- the boundary is the guard, not the bind address.
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--proxy-port", type=_port, default=DEFAULT_PROXY_PORT)
    parser.add_argument("--control-port", type=_port, default=DEFAULT_CONTROL_PORT)
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help=(
            "An internal destination the guard may reach, as host:port. "
            "Repeatable. Everything else must be publicly routable."
        ),
    )
    parser.add_argument(
        "--upstream-proxy",
        default=None,
        metavar="URL",
        help=(
            "Hand judged requests to this forward proxy. See `proxy.Upstream` "
            "for what it weakens; a Compose deployment normally has none."
        ),
    )
    arguments = parser.parse_args(argv)
    try:
        allowed = parse_allowed(arguments.allow_host)
    except ValueError as error:
        parser.error(str(error))
    upstream = None
    if arguments.upstream_proxy is not None:
        try:
            upstream = Upstream.from_url(arguments.upstream_proxy)
        except ValueError as error:
            parser.error(str(error))

    asyncio.run(
        _serve(
            host=arguments.host,
            proxy_port=arguments.proxy_port,
            control_port=arguments.control_port,
            proxy=GuardedProxy(allowed=allowed, upstream=upstream),
        )
    )


async def _serve(
    *, host: str, proxy_port: int, control_port: int, proxy: GuardedProxy
) -> None:
    server = await asyncio.start_server(proxy.handle, host, proxy_port)
    config = uvicorn.Config(
        create_app(proxy), host=host, port=control_port, access_log=False
    )
    async with server:
        await uvicorn.Server(config).serve()


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


if __name__ == "__main__":  # pragma: no cover - console script owns this branch
    main()


__all__ = [
    "DECISIONS_PATH",
    "DEFAULT_CONTROL_PORT",
    "DEFAULT_PROXY_PORT",
    "HEALTH_PATH",
    "create_app",
    "main",
]
