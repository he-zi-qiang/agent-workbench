"""HTTP control-plane commands for durable Tasks.

The CLI intentionally has no database or workflow imports.  It exercises the
same `/v1/tasks` contract as another client would, which keeps a convenient
operator command from becoming a second way around Task authorization or
idempotency.  Server error bodies are never rendered: they can contain details
that belong in server logs, not a terminal transcript.

The shared HTTP plumbing lives in :mod:`agent_workbench.apps.cli.http`, because
search, chat, approvals and artifacts all need the same client, the same
identity headers and the same refusal to echo a server body.  The names below
are re-exported so this module stays the one a Task caller imports.
"""

from __future__ import annotations

import uuid
from typing import Any, TextIO

import httpx

from agent_workbench.apps.cli.http import (
    DEFAULT_API_URL,
    DEFAULT_TIMEOUT_SECONDS,
    EXIT_CONFLICT,
    EXIT_NOT_FOUND,
    EXIT_REQUEST_FAILED,
    EXIT_TRANSPORT_ERROR,
    CliHttpError,
    HttpClientFactory,
    default_http_client,
    identity_headers,
    render_error,
    render_result,
    response_json,
)

#: Kept as the Task-facing name for the shared error. The Task commands were
#: the first HTTP client here and their exit codes are a documented contract.
TaskCliError = CliHttpError


def run_task(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory = default_http_client,
) -> int:
    """Execute one Task command and render a script-friendly result."""

    headers = identity_headers(args)
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
    render_result(result, as_json=args.json, stream=stream)
    return 0


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
                # Only when chosen. An omitted flag must submit the exact bytes
                # the CLI sent before the flag existed, so the deployment's
                # default -- not this client's opinion of it -- decides.
                **(
                    {"graph": args.graph}
                    if getattr(args, "graph", None) is not None
                    else {}
                ),
            },
        )
        return response_json(response), key
    if command == "list":
        params: dict[str, Any] = {"limit": args.limit}
        if args.status:
            params["status"] = list(args.status)
        if args.cursor is not None:
            params["cursor"] = args.cursor
        return (
            response_json(client.get("/v1/tasks", headers=headers, params=params)),
            None,
        )
    if command == "get":
        return (
            response_json(client.get(f"/v1/tasks/{args.task_id}", headers=headers)),
            None,
        )
    if command == "timeline":
        params = {"limit": args.limit}
        if args.cursor is not None:
            params["cursor"] = args.cursor
        return (
            response_json(
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
            response_json(
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
    "render_result",
    "response_json",
    "run_task",
]
