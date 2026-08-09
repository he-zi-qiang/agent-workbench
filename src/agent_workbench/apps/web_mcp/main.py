"""Console entry point for the loopback read-only web MCP service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import httpx
import uvicorn

from agent_workbench.apps.web_mcp.fetcher import WebFetcher
from agent_workbench.apps.web_mcp.server import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8767
DEFAULT_TIMEOUT_SECONDS = 20.0
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-web-mcp",
        description="Run the project-owned read-only web MCP server on loopback.",
    )
    parser.add_argument("--host", choices=_LOOPBACK_HOSTS, default=DEFAULT_HOST)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    parser.add_argument("--timeout-seconds", type=_timeout, default=None)
    arguments = parser.parse_args(argv)
    timeout = arguments.timeout_seconds or DEFAULT_TIMEOUT_SECONDS
    # Redirects are followed by the address guard, one judged hop at a time, so
    # the client must never follow one on its own. There is deliberately no flag
    # that could turn this back on.
    client = httpx.AsyncClient(follow_redirects=False, timeout=timeout)
    uvicorn.run(
        create_app(
            host=arguments.host,
            fetcher=WebFetcher(http=client, timeout_seconds=timeout),
        ),
        host=arguments.host,
        port=arguments.port,
        access_log=False,
    )


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


def _timeout(value: str) -> float:
    parsed = float(value)
    if not 1.0 <= parsed <= 120.0:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 120 seconds")
    return parsed


if __name__ == "__main__":  # pragma: no cover - console script owns this branch
    main()


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "DEFAULT_TIMEOUT_SECONDS", "main"]
