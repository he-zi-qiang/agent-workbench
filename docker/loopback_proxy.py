"""Expose an in-container loopback API only through Compose's host loopback.

The application process itself remains bound to 127.0.0.1, as required by its
header-based local identity boundary. Compose maps this proxy's port to the
host's 127.0.0.1 only; it exists because Docker port forwarding cannot reach a
different container's loopback listener directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("LOCAL_PROXY_PORT", "8000"))
UPSTREAM_HOST = "127.0.0.1"
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
