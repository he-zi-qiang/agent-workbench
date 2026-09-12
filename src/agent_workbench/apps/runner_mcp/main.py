"""Console entry point for the loopback command-runner MCP service (ADR-0115)."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from agent_workbench.apps.runner_mcp.server import create_app

DEFAULT_HOST = "127.0.0.1"
#: 8774: the next free slot in the loopback range this project hands out
#: (8765 word, 8766 sandbox, 8767 web, 8768 computer, 8769 encoder, 8773 browser). The
#: container publishes it on its own interface through `loopback_proxy.py`,
#: the way the sandbox broker publishes 8766, and the API reaches it through a
#: tunnel whose two ends are both loopback (ADR-0107 §3.4).
DEFAULT_PORT = 8774
#: Where Compose mounts the host folder the API can write (ADR-0109); the same
#: path on both sides, which is what lets `cwd` be a string rather than a set
#: of files to ship.
DEFAULT_PROJECTS_ROOT = "/projects"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-runner-mcp",
        description=(
            "Run the project-owned command runner MCP server on loopback: one "
            "shell command per call, in a directory under --projects-root."
        ),
    )
    parser.add_argument("--host", choices=_LOOPBACK_HOSTS, default=DEFAULT_HOST)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    parser.add_argument(
        "--projects-root",
        default=DEFAULT_PROJECTS_ROOT,
        help="The directory every `cwd` must lie under.",
    )
    arguments = parser.parse_args(argv)
    uvicorn.run(
        create_app(host=arguments.host, projects_root=Path(arguments.projects_root)),
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


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "DEFAULT_PROJECTS_ROOT", "main"]
