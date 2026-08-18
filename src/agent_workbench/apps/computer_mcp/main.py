"""Console entry point for the loopback computer-use MCP service.

Loopback only, like every other project-owned MCP server here, and for a
sharper reason than the others: this one can move the cursor. A port that
listened on anything else would be a remote input device (ADR-044, ADR-070).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from agent_workbench.apps.computer_mcp.server import create_app
from agent_workbench.ports.screen import ScreenUnavailableError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8768
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-computer-mcp",
        description="Run the project-owned computer-use MCP server on loopback.",
    )
    parser.add_argument("--host", choices=_LOOPBACK_HOSTS, default=DEFAULT_HOST)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    arguments = parser.parse_args(argv)
    try:
        app = create_app(host=arguments.host)
    except ScreenUnavailableError as unavailable:
        # Exits rather than serving. A server that starts and refuses every
        # call is a server an operator has to read logs to diagnose; the
        # message this carries names the missing extra or the missing grant.
        parser.exit(2, f"{unavailable}\n")
        return
    uvicorn.run(
        app,
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
    main(sys.argv[1:])


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "main"]
