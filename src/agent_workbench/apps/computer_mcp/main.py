"""Console entry point for the loopback computer-use MCP service.

Loopback only, like every other project-owned MCP server here, and for a
sharper reason than the others: this one can move the cursor. A port that
listened on anything else would be a remote input device (ADR-044, ADR-070).
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Sequence
from typing import Any

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
    _serve(app, host=arguments.host, port=arguments.port)


def _serve(app: Any, *, host: str, port: int) -> None:
    """Run the HTTP server, and on macOS give the main thread to AppKit.

    **The main thread is the feature, not an implementation detail**
    (ADR-092). macOS lets a process change which application is frontmost only
    when four things are true at once, and the last of them is a live
    main-thread run loop. Measured 2026-08-29 on this machine, holding the
    other three fixed (bundled `.app`, ad-hoc signature, Accessibility
    granted): without `NSApplication.run()` activation succeeded **0 of 15**
    times, every attempt timing out with the frontmost application unchanged;
    with it, **15 of 15**, including taking focus from the application the
    person was typing into.

    So uvicorn moves to a background thread. That inverts ADR-076 §2, which
    gave the main thread to uvicorn and rejected `NSAlert` because a modal
    dialog there would stop the server answering anything -- including the
    health probe an operator uses to find out why. The objection was correct
    and is answered by the arrangement rather than by avoidance: the run loop a
    dialog would block is no longer the one serving HTTP.

    No AppKit symbol appears in this module. The run loop is one operating
    system's requirement, so it lives with the rest of that operating system's
    requirements in `adapters/screen/darwin.py` -- the one file in this
    repository that carries pyright suppressions, and the reason ADR-070 §3
    can still say "one file".
    """

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, access_log=False))
    if sys.platform != "darwin":
        # Windows (ADR-0108) has no run-loop requirement: `SetForegroundWindow`
        # is a call, not a request to a window server that needs this thread
        # to be listening, so uvicorn keeps the main thread as it always did.
        server.run()
        return

    from agent_workbench.adapters.screen.darwin import give_main_thread_to_appkit

    # Daemon, because the main thread now belongs to AppKit and that is what
    # decides when this process ends. `install_signal_handlers` no-ops off the
    # main thread -- uvicorn's own behaviour, and the reason this needs no
    # patching of it.
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    try:
        give_main_thread_to_appkit(serving=thread.is_alive)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


if __name__ == "__main__":  # pragma: no cover - console script owns this branch
    main(sys.argv[1:])


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "main"]
