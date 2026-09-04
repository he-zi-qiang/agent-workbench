"""``docker/loopback_proxy.py`` forwards bytes, in both directions it is used.

The outward use (API port 8000) predates this file; the inward one (ADR-0107,
ADR-0108) is what made the listen host and the upstream host parameters. Both
are asserted over real sockets: a client dials the proxy's loopback listener,
the proxy dials an upstream on another loopback port, and what arrives is
byte-for-byte what was sent -- including a Host header naming the proxy's own
address, which is the whole point of the inward use.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROXY = ROOT / "docker" / "loopback_proxy.py"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _echo_upstream(port: int, seen: list[bytes]) -> socket.socket:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)

    def serve() -> None:
        # Every connection, not the first: `_wait_for` below dials the proxy
        # to learn it is listening, and the proxy dials this for each dial.
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                data = connection.recv(65_536)
                if data:
                    seen.append(data)
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
                    )

    threading.Thread(target=serve, daemon=True).start()
    return listener


def _wait_for(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("the proxy never listened")


def test_the_inward_tunnel_delivers_the_clients_loopback_host_header() -> None:
    listen, upstream = _free_port(), _free_port()
    seen: list[bytes] = []
    listener = _echo_upstream(upstream, seen)
    process = subprocess.Popen(
        [sys.executable, str(PROXY)],
        env={
            **os.environ,
            "LOCAL_PROXY_LISTEN_HOST": "127.0.0.1",
            "LOCAL_PROXY_PORT": str(listen),
            "LOCAL_PROXY_UPSTREAM_HOST": "127.0.0.1",
            "LOCAL_PROXY_UPSTREAM_PORT": str(upstream),
        },
    )
    try:
        _wait_for(listen)
        request = (
            f"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1:{listen}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        with socket.create_connection(("127.0.0.1", listen), timeout=5) as client:
            client.sendall(request)
            answer = client.recv(65_536)
    finally:
        process.terminate()
        process.wait(timeout=5)
        listener.close()

    assert seen == [request], "the request must arrive byte-for-byte"
    assert answer.endswith(b"ok")


def test_an_upstream_that_is_not_there_drops_the_connection_rather_than_hanging() -> (
    None
):
    """What `routes/computer.py` turns into "not running": a dropped
    connection, the same thing a direct dial would have seen."""

    listen, upstream = _free_port(), _free_port()
    process = subprocess.Popen(
        [sys.executable, str(PROXY)],
        env={
            **os.environ,
            "LOCAL_PROXY_LISTEN_HOST": "127.0.0.1",
            "LOCAL_PROXY_PORT": str(listen),
            "LOCAL_PROXY_UPSTREAM_HOST": "127.0.0.1",
            "LOCAL_PROXY_UPSTREAM_PORT": str(upstream),
        },
    )
    try:
        _wait_for(listen)
        with socket.create_connection(("127.0.0.1", listen), timeout=5) as client:
            client.sendall(b"GET / HTTP/1.1\r\n\r\n")
            client.settimeout(5)
            assert client.recv(1) == b""
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_the_defaults_are_the_outward_ones_the_api_has_always_used() -> None:
    """The parameters were added for the inward use; the API's container sets
    only the two ports and must keep getting a wildcard listener over loopback."""

    source = PROXY.read_text(encoding="utf-8")
    assert (
        'LISTEN_HOST = os.environ.get("LOCAL_PROXY_LISTEN_HOST", "0.0.0.0")' in source
    )
    assert (
        'UPSTREAM_HOST = os.environ.get("LOCAL_PROXY_UPSTREAM_HOST", "127.0.0.1")'
        in source
    )
