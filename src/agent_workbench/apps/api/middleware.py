"""The control plane's size limit, and the one route it must not apply to.

Two planes share one server. Control requests are JSON describing work -- a few
hundred bytes -- and are capped so a client cannot make the process hold an
arbitrary body in memory before anything has looked at it. The data plane
carries document bytes and is exempt, because capping it at the control limit
is the same as not supporting uploads.

The limit is enforced by reading the body here, up to one byte past the
ceiling. Buffering that much is exactly what the ceiling already permits, and
it is the only way to be right about a request that declares no length or
declares one it does not honour: a Content-Length check alone trusts the sender
about the thing being checked.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_LENGTH_HEADER = b"content-length"


def _nothing_exempt(path: str) -> bool:
    return False


class ControlPlaneLimit:
    """Rejects oversized control requests with 413 before a route sees them."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        is_exempt: Callable[[str], bool] | None = None,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._app = app
        self._max_bytes = max_bytes
        # A predicate rather than a prefix. A prefix that happens to be a
        # router's mount point exempts every route under it, which is how a
        # control endpoint quietly loses its limit.
        self._is_exempt = is_exempt if is_exempt is not None else _nothing_exempt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_exempt(str(scope.get("path", ""))):
            await self._app(scope, receive, send)
            return

        declared = self._declared_length(scope)
        if declared is not None and declared > self._max_bytes:
            await self._refuse(send)
            return

        body, oversized = await self._read_body(receive)
        if oversized:
            await self._refuse(send)
            return

        await self._app(scope, _replay(body, receive), send)

    def _declared_length(self, scope: Scope) -> int | None:
        headers: Sequence[tuple[bytes, bytes]] = scope.get("headers", ())
        for name, value in headers:
            if name.lower() == CONTENT_LENGTH_HEADER:
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    async def _read_body(self, receive: Receive) -> tuple[list[Message], bool]:
        """Buffer the request, stopping one byte past the ceiling."""

        collected: list[Message] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                collected.append(message)
                return collected, False
            size += len(message.get("body", b""))
            collected.append(message)
            if size > self._max_bytes:
                return collected, True
            if not message.get("more_body", False):
                return collected, False

    async def _refuse(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": (
                    b'{"detail":"the control request exceeds the permitted size;'
                    b' document bytes belong on the upload data plane"}'
                ),
            }
        )


def _replay(messages: list[Message], original: Receive) -> Receive:
    """Hand the buffered request back, then step out of the way.

    Falling through to the original channel matters: a response that streams
    watches the same channel for ``http.disconnect``, and a replay that only
    ever returned empty bodies would leave it waiting for a message that can
    never arrive.
    """

    pending = list(messages)

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        return await original()

    return receive


__all__ = ["ControlPlaneLimit"]
