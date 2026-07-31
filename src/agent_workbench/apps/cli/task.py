"""HTTP control-plane commands for durable Tasks.

The CLI intentionally has no database or workflow imports.  It exercises the
same `/v1/tasks` contract as another client would, which keeps a convenient
operator command from becoming a second way around Task authorization or
idempotency.  Server error bodies are never rendered: they can contain details
that belong in server logs, not a terminal transcript.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any, TextIO, cast

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 30.0

EXIT_REQUEST_FAILED = 1
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_TRANSPORT_ERROR = 5


class TaskCliError(RuntimeError):
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


HttpClientFactory = Callable[[str, float], httpx.Client]


def default_http_client(base_url: str, timeout_seconds: float) -> httpx.Client:
    """Construct the short-lived client used for exactly one CLI command."""

    return httpx.Client(base_url=base_url, timeout=timeout_seconds)


def run_task(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory = default_http_client,
) -> int:
    """Execute one Task command and render a script-friendly result."""

    headers = {
        "x-tenant-id": args.tenant_id,
        "x-principal-id": args.principal_id,
    }
    try:
        with http_client_factory(args.api_url, args.timeout_seconds) as client:
            payload, idempotency_key = _request(client, args, headers)
    except httpx.HTTPError as error:
        raise TaskCliError(code="transport_error") from error

    result: dict[str, Any] = payload
    if idempotency_key is not None:
        # A submitted Task must always display the effective key. It is the
        # only value a caller needs to retry a request safely after a timeout.
        result = {**payload, "idempotency_key": idempotency_key}
    _render(result, as_json=args.json, stream=stream)
    return 0


def render_error(error: TaskCliError, stream: TextIO, *, as_json: bool) -> int:
    """Render only a stable local category, never a server-provided detail."""

    payload: dict[str, Any] = {"error": error.code}
    if error.status_code is not None:
        payload["status"] = error.status_code
    _render(payload, as_json=as_json, stream=stream)
    return error.exit_code


def _request(
    client: httpx.Client,
    args: Any,
    headers: dict[str, str],
) -> tuple[dict[str, Any], str | None]:
    command = args.task_command
    if command == "submit":
        key = _idempotency_key(args)
        response = client.post(
            "/v1/tasks",
            headers={**headers, "Idempotency-Key": key},
            json={
                "objective": args.objective,
                "max_revisions": args.max_revisions,
                **(
                    {"knowledge_base_id": args.knowledge_base_id}
                    if args.knowledge_base_id is not None
                    else {}
                ),
            },
        )
        return _response_json(response), key
    if command == "get":
        return (
            _response_json(client.get(f"/v1/tasks/{args.task_id}", headers=headers)),
            None,
        )
    if command == "timeline":
        params: dict[str, Any] = {"limit": args.limit}
        if args.cursor is not None:
            params["cursor"] = args.cursor
        return (
            _response_json(
                client.get(
                    f"/v1/tasks/{args.task_id}/timeline",
                    headers=headers,
                    params=params,
                )
            ),
            None,
        )
    if command == "cancel":
        return (
            _response_json(
                client.post(
                    f"/v1/tasks/{args.task_id}/cancel",
                    headers=headers,
                    json={"reason": args.reason},
                )
            ),
            None,
        )
    raise RuntimeError(f"unsupported Task command: {command}")  # pragma: no cover


def _idempotency_key(args: Any) -> str:
    if args.idempotency_key is not None:
        return args.idempotency_key
    # A command with no caller-provided key is still a retry-safe command. The
    # generated key is rendered alongside the created Task so it can be saved.
    return f"cli_{uuid.uuid4().hex}"


def _response_json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        raise TaskCliError(
            code=_error_code(response.status_code), status_code=response.status_code
        )
    try:
        payload: object = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise TaskCliError(code="invalid_server_response") from error
    if not isinstance(payload, dict):
        raise TaskCliError(code="invalid_server_response")
    return cast(dict[str, Any], payload)


def _error_code(status_code: int) -> str:
    if status_code == 404:
        return "task_not_found"
    if status_code == 409:
        return "request_conflict"
    return "request_failed"


def _render(payload: dict[str, Any], *, as_json: bool, stream: TextIO) -> None:
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


__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "EXIT_CONFLICT",
    "EXIT_NOT_FOUND",
    "EXIT_REQUEST_FAILED",
    "EXIT_TRANSPORT_ERROR",
    "HttpClientFactory",
    "TaskCliError",
    "default_http_client",
    "render_error",
    "run_task",
]
