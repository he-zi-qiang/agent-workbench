"""``docker/decide_sandbox.py``: the launcher's one question about the broker.

Whether `sandbox_run` is on for a start is decided by probing the broker's
runtime through the loopback tunnel (ADR-0107), the way web search is decided
by probing for a key. Three answers the broker can give and one it cannot,
each pinned against a stand-in that answers on a real loopback port -- the
probe is standard-library `urllib`, and a fake `urlopen` would test the
fake.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "docker" / "decide_sandbox.py"


@contextmanager
def _broker(status: int, body: dict[str, object]) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/health"
    finally:
        server.shutdown()
        server.server_close()


def _run(url: str, *, wait: str = "1") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROBE)],
        env={
            **os.environ,
            "SANDBOX_HEALTH_URL": url,
            "SANDBOX_PROBE_WAIT_SECONDS": wait,
            "NO_PROXY": "127.0.0.1,localhost",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_a_broker_whose_runtime_answers_turns_the_sandbox_on() -> None:
    with _broker(200, {"status": "ok", "container_runtime_available": True}) as url:
        result = _run(url)

    assert result.returncode == 0
    assert "sandbox_run is on" in result.stderr


def test_a_broker_without_a_runtime_leaves_it_off_and_says_why() -> None:
    """503 is the broker saying it is up and the daemon is not (ADR-029 §3.6)."""

    with _broker(
        503,
        {
            "status": "degraded",
            "container_runtime": "docker",
            "container_runtime_available": False,
        },
    ) as url:
        result = _run(url)

    assert result.returncode == 1
    assert "docker" in result.stderr and "not available" in result.stderr
    assert "stays off" in result.stderr


def test_a_200_that_does_not_name_the_runtime_is_not_a_yes() -> None:
    """A health page that happens to answer 200 is not the broker's contract."""

    with _broker(200, {"status": "ok"}) as url:
        result = _run(url)

    assert result.returncode == 1


def test_nothing_at_the_address_leaves_it_off_after_the_wait() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    result = _run(f"http://127.0.0.1:{port}/health")

    assert result.returncode == 1
    assert "no sandbox broker answered" in result.stderr


def test_the_probe_needs_nothing_but_the_standard_library() -> None:
    """It runs before the package has a reason to be imported, and it is the
    file somebody reads from a container log alone."""

    source = PROBE.read_text(encoding="utf-8")
    imports = [
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert "agent_workbench" not in imports
    assert "httpx" not in imports
