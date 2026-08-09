"""Console entry point for the loopback Word MCP service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from agent_workbench.apps.word_mcp.server import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-word-mcp",
        description="Run the project-owned Word MCP server on loopback.",
    )
    parser.add_argument("--host", choices=_LOOPBACK_HOSTS, default=DEFAULT_HOST)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    arguments = parser.parse_args(argv)
    uvicorn.run(
        create_app(host=arguments.host),
        host=arguments.host,
        port=arguments.port,
        access_log=False,
    )


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


if __name__ == "__main__":  # pragma: no cover - console script owns this branch
    main()


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "main"]
