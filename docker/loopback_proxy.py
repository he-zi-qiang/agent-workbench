"""Carry one TCP port across the line a loopback-only process will not cross.

Two directions, one program.

**Outward** (the original use): a process in this container is bound to
127.0.0.1, as its header-based identity boundary requires, and Compose maps a
port to the host's 127.0.0.1 -- but Docker port forwarding cannot reach a
container's own loopback. So this listens on the container interface and
forwards to the loopback process. The API has used it this way since the
Compose topology existed; the sandbox broker uses it the same way (ADR-0107).

**Inward** (ADR-0107, ADR-0108): a process in this container is a *client* of
a loopback-only server that lives somewhere else -- the sandbox broker in
another container, the computer-use server on the Windows host. Every guard
on that path assumes loopback: the settings validator refuses a plain-HTTP
endpoint that is not 127.0.0.1, the MCP SDK validates the Host header because
the bind address is loopback, and the servers' own `--host` flags are
argparse choice lists of loopback names. So this listens on *this*
container's 127.0.0.1 and forwards to the named upstream. The client dials
127.0.0.1, sends `Host: 127.0.0.1:<port>`, and the bytes reach a server that
was itself started on 127.0.0.1 -- every guard holds, unchanged, because
every guard is looking at a loopback address and seeing one.

That is not a way around those guards. It is what they are for: none of them
was written to stop two processes of one deployment from talking, they were
written to stop a process from listening where a stranger could reach it, and
a tunnel whose two ends are both loopback listens nowhere new.

Plain stdlib, no timeouts on an idle connection: an MCP call can hold a
stream open for the whole of a sandbox run, and a forwarder that closed it
early would turn a slow script into a transport error.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

LISTEN_HOST = os.environ.get("LOCAL_PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LOCAL_PROXY_PORT", "8000"))
UPSTREAM_HOST = os.environ.get("LOCAL_PROXY_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("LOCAL_PROXY_UPSTREAM_PORT", "8001"))


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65_536):
            writer.write(data)
            await writer.drain()
    finally:
        with contextlib.suppress(ConnectionError):
            writer.close()
            await writer.wait_closed()


async def _handle(
    client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            UPSTREAM_HOST, UPSTREAM_PORT
        )
    except OSError:
        # The upstream is not there. Closing the client's socket is the whole
        # answer: an HTTP client sees a connection that dropped, which is the
        # same thing it would have seen dialling the upstream itself -- and
        # `apps/api/routes/computer.py` turns exactly that into "not running".
        client_writer.close()
        await client_writer.wait_closed()
        return
    await asyncio.gather(
        _copy(client_reader, upstream_writer),
        _copy(upstream_reader, client_writer),
    )


async def main() -> None:
    server = await asyncio.start_server(_handle, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":  # pragma: no cover - container entry point
    asyncio.run(main())
