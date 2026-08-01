"""What every HTTP-speaking CLI command shares.

The CLI has no database, no workflow and no store imports. It speaks the same
contract another client would, which is what keeps a convenient operator command
from becoming a second way around authorization or idempotency -- a `task
cancel` that reached the Registry directly would be exactly that.

Server error bodies are never rendered. They are written for operators reading
logs and can carry request detail, provider text or a DSN; a terminal transcript
is not where that belongs. What the caller gets instead is a stable local
category and the status code, which is enough to branch on and contains nothing
the server did not already disclose by answering.

Identity is three headers because that is what the development resolver reads.
Scopes are here rather than in each command's own flag handling because they are
part of who is calling, not part of what is being asked: a tool that declares a
permission scope is unreachable without one, and a caller that forgot the flag
should see the same refusal an unscoped principal would.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, TextIO, cast

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0

EXIT_REQUEST_FAILED = 1
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_TRANSPORT_ERROR = 5

HttpClientFactory = Callable[[str, float], httpx.Client]


class CliHttpError(RuntimeError):
    """A caller-safe HTTP failure, with no server response body."""

    def __init__(self, *, code: str, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)

    @property
    def exit_code(self) -> int:
        if self.status_code == 404:
            return EXIT_NOT_FOUND
        if self.status_code == 409:
            return EXIT_CONFLICT
        if self.status_code is None:
            return EXIT_TRANSPORT_ERROR
        return EXIT_REQUEST_FAILED


def default_http_client(base_url: str, timeout_seconds: float) -> httpx.Client:
    """Construct the short-lived client used for exactly one CLI command."""

    return httpx.Client(base_url=base_url, timeout=timeout_seconds)


def identity_headers(args: Any) -> dict[str, str]:
    """The development identity, as the API's resolver reads it.

    ``--scope`` is repeatable and joined here rather than at each call site, so
    a command that forgets to forward it cannot exist. Absent scopes send no
    header at all: an empty one and a missing one both mean "no scopes", and
    sending the empty string makes a request look like it decided something.
    """

    headers = {
        "x-tenant-id": args.tenant_id,
        "x-principal-id": args.principal_id,
    }
    scopes = tuple(getattr(args, "scope", None) or ())
    if scopes:
        headers["x-principal-scopes"] = ",".join(scopes)
    return headers


def response_json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        raise CliHttpError(
            code=error_code(response.status_code), status_code=response.status_code
        )
    try:
        payload: object = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CliHttpError(code="invalid_server_response") from error
    if not isinstance(payload, dict):
        raise CliHttpError(code="invalid_server_response")
    return cast(dict[str, Any], payload)


def error_code(status_code: int) -> str:
    if status_code == 401:
        return "unauthenticated"
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "request_conflict"
    if status_code == 422:
        return "invalid_request"
    return "request_failed"


def render_result(payload: Mapping[str, Any], *, as_json: bool, stream: TextIO) -> None:
    if as_json:
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        stream.write("\n")
        return
    for key in sorted(payload):
        value = payload[key]
        rendered = (
            json.dumps(value, sort_keys=True, separators=(",", ":"))
            if isinstance(value, (dict, list))
            else str(value)
        )
        stream.write(f"{key}: {rendered}\n")


def render_error(error: CliHttpError, stream: TextIO, *, as_json: bool) -> int:
    """Render only a stable local category, never a server-provided detail."""

    payload: dict[str, Any] = {"error": error.code}
    if error.status_code is not None:
        payload["status"] = error.status_code
    render_result(payload, as_json=as_json, stream=stream)
    return error.exit_code


__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "EXIT_CONFLICT",
    "EXIT_NOT_FOUND",
    "EXIT_REQUEST_FAILED",
    "EXIT_TRANSPORT_ERROR",
    "CliHttpError",
    "HttpClientFactory",
    "default_http_client",
    "error_code",
    "identity_headers",
    "render_error",
    "render_result",
    "response_json",
]
