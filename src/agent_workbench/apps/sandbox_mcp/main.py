"""Console entry point for the loopback sandbox MCP service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn

from agent_workbench.apps.sandbox_mcp.executor import (
    DEFAULT_CONTAINER_RUNTIME,
    DEFAULT_SANDBOX_IMAGE,
    SandboxExecutor,
)
from agent_workbench.apps.sandbox_mcp.server import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-sandbox-mcp",
        description="Run the project-owned Python sandbox MCP server on loopback.",
    )
    parser.add_argument("--host", choices=_LOOPBACK_HOSTS, default=DEFAULT_HOST)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    # Flags rather than configuration fields: these two say which runtime and
    # which interpreter image a deployment has, which is the same class of fact
    # as --host and --port. The isolation itself is not reachable from here --
    # see executor.ISOLATION_FLAGS for why it must not be.
    parser.add_argument("--container-runtime", default=DEFAULT_CONTAINER_RUNTIME)
    parser.add_argument("--image", default=DEFAULT_SANDBOX_IMAGE)
    arguments = parser.parse_args(argv)
    uvicorn.run(
        create_app(
            host=arguments.host,
            executor=SandboxExecutor(
                runtime=arguments.container_runtime,
                image=arguments.image,
            ),
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


if __name__ == "__main__":  # pragma: no cover - console script owns this branch
    main()


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "main"]
