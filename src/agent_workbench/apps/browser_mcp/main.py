"""Console entry point for the guarded browser MCP service (ADR-0112).

Three things share one event loop here, and the order they start in is not
cosmetic: the proxy must be listening before Chromium launches, because
Chromium is launched pointing at it and a browser whose proxy is not yet up
fails its first navigation rather than retrying. The MCP server comes last,
since answering a tool call before there is a page to act on would report a
readiness this process does not have.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from agent_workbench.apps.browser_mcp.proxy import (
    AllowedHost,
    DecisionSource,
    GuardedProxy,
    LocalDecisions,
    RemoteDecisions,
    Upstream,
    parse_allowed,
)
from agent_workbench.apps.browser_mcp.server import create_app
from agent_workbench.apps.browser_mcp.session import PlaywrightSession

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8773
#: The proxy listens on loopback inside this container only. It is never
#: published and never reachable from another container -- the browser is the
#: only client it will ever have.
DEFAULT_PROXY_PORT = 8771
DEFAULT_WORKSPACE_ROOT = Path("/workspace")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-browser-mcp",
        description="Run the project-owned browser MCP server on loopback.",
    )
    parser.add_argument("--host", choices=_LOOPBACK_HOSTS, default=DEFAULT_HOST)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    parser.add_argument("--proxy-port", type=_port, default=DEFAULT_PROXY_PORT)
    parser.add_argument(
        "--proxy-endpoint",
        default=None,
        metavar="URL",
        help=(
            "Use an external destination guard instead of starting one here -- "
            "under Compose this is the `browser-egress` service, which is the "
            "only container that can both be seen by this one and reach the "
            "network (ADR-0112 3.3). Omit it and the guard runs in this "
            "process, which is what the native path does."
        ),
    )
    parser.add_argument(
        "--decisions-url",
        default=None,
        metavar="URL",
        help=(
            "Where browser_diagnostics reads refused destinations from when "
            "the guard is external. Required with --proxy-endpoint."
        ),
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help=(
            "An internal destination the guard may reach, given as host:port. "
            "Repeatable. Everything else must be publicly routable. Nothing in "
            "an MCP request can add to this list."
        ),
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=DEFAULT_WORKSPACE_ROOT,
        help="Directory a `workspace_path` is resolved against (read-only).",
    )
    parser.add_argument(
        "--upstream-proxy",
        default=None,
        metavar="URL",
        help=(
            "Hand judged requests to this forward proxy instead of dialling "
            "destinations directly. Needed on a machine whose own traffic "
            "leaves through a fake-IP/TUN proxy, where every hostname resolves "
            "into 198.18.0.0/15 and address judgement refuses everything. On "
            "this branch names are judged rather than addresses -- see "
            "`proxy.Upstream`. The container path never uses it."
        ),
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Chromium headed; for local debugging on a machine with a display.",
    )
    arguments = parser.parse_args(argv)

    try:
        allowed = parse_allowed(arguments.allow_host)
    except ValueError as error:
        # Before anything starts, so a typo in a Compose file is a start-up
        # failure and not a destination that quietly never works.
        parser.error(str(error))

    if arguments.proxy_endpoint is not None and arguments.decisions_url is None:
        # Refused here rather than degraded at read time: a deployment that
        # points the browser at an external guard but forgets where to read its
        # verdicts would report "nothing refused" forever, which is the one
        # wrong answer this design set out to avoid.
        parser.error("--proxy-endpoint requires --decisions-url")
    upstream: Upstream | None = None
    if arguments.upstream_proxy is not None:
        try:
            upstream = Upstream.from_url(arguments.upstream_proxy)
        except ValueError as error:
            parser.error(str(error))

    if arguments.proxy_endpoint is not None and arguments.allow_host:
        parser.error(
            "--allow-host belongs on the external guard, not here; "
            "pass it to agent-browser-egress"
        )

    asyncio.run(
        _serve(
            host=arguments.host,
            port=arguments.port,
            proxy_port=arguments.proxy_port,
            proxy_endpoint=arguments.proxy_endpoint,
            decisions_url=arguments.decisions_url,
            upstream=upstream,
            allowed=allowed,
            workspace_root=arguments.workspace_root,
            headless=not arguments.headed,
        )
    )


async def _serve(
    *,
    host: str,
    port: int,
    proxy_port: int,
    proxy_endpoint: str | None,
    decisions_url: str | None,
    upstream: Upstream | None,
    allowed: frozenset[AllowedHost],
    workspace_root: Path,
    headless: bool,
) -> None:
    proxy_server: asyncio.Server | None = None
    decisions: DecisionSource
    if proxy_endpoint is None:
        proxy = GuardedProxy(allowed=allowed, upstream=upstream)
        proxy_server = await asyncio.start_server(proxy.handle, host, proxy_port)
        endpoint = f"http://{host}:{proxy_port}"
        decisions = LocalDecisions(proxy=proxy)
    else:
        endpoint = proxy_endpoint
        assert decisions_url is not None  # checked in main()
        decisions = RemoteDecisions(url=decisions_url)

    session = PlaywrightSession(
        proxy_endpoint=endpoint,
        workspace_root=workspace_root,
        headless=headless,
    )
    try:
        await session.start()
    except RuntimeError as error:
        # The `browser` extra is not installed. Say so and stop, rather than
        # serving a tool surface whose every call would fail the same way.
        print(f"agent-browser-mcp: {error}", file=sys.stderr)
        if proxy_server is not None:
            proxy_server.close()
        raise SystemExit(2) from error

    application = create_app(session, decisions, host=host)
    config = uvicorn.Config(application, host=host, port=port, access_log=False)
    try:
        await uvicorn.Server(config).serve()
    finally:
        if proxy_server is not None:
            proxy_server.close()
            with contextlib.suppress(Exception):
                await proxy_server.wait_closed()
        await session.aclose()


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


if __name__ == "__main__":  # pragma: no cover - console script owns this branch
    main()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_PROXY_PORT",
    "DEFAULT_WORKSPACE_ROOT",
    "main",
]
