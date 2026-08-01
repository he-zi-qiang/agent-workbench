"""The read-and-answer commands: search, chat, approvals and artifacts.

Every one of these existed only as an HTTP route. That gap mattered most for
approvals: the human step this project is built around could be performed with
``curl`` and nothing else, so the one part of the system that requires a person
was the one part with no interface for one.

``search`` is here for a reason worth stating. It answers "what would the model
have been shown", which is exactly the half of chat that needs no provider -- so
a deployment with no model key can still demonstrate retrieval, and a retrieval
problem can be told apart from a generation one without guessing.

``chat ask`` opens a session when it is not given one and prints the id it
opened. A command that silently created a session per invocation would make
every follow-up question a new conversation, and the caller would have no way to
notice.
"""

from __future__ import annotations

import uuid
from typing import Any, TextIO

import httpx

from agent_workbench.apps.cli.http import (
    CliHttpError,
    HttpClientFactory,
    default_http_client,
    identity_headers,
    render_result,
    response_json,
)


def run_search(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory = default_http_client,
) -> int:
    """Ask what the corpus holds, without asking a model to talk about it."""

    headers = identity_headers(args)
    with _client(args, http_client_factory) as client:
        payload = response_json(
            client.post(
                "/v1/search",
                headers=headers,
                json={
                    "query": args.query,
                    "knowledge_base_id": args.knowledge_base_id,
                    "top_k": args.top_k,
                },
            )
        )
    if args.json:
        render_result(payload, as_json=True, stream=stream)
        return 0
    _render_search(payload, stream)
    return 0


def run_chat(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory = default_http_client,
) -> int:
    headers = identity_headers(args)
    with _client(args, http_client_factory) as client:
        if args.chat_command == "history":
            payload = response_json(
                client.get(
                    f"/v1/chat/sessions/{args.session_id}/messages", headers=headers
                )
            )
            render_result(payload, as_json=args.json, stream=stream)
            return 0

        session_id = args.session_id
        if session_id is None:
            opened = response_json(
                client.post(
                    "/v1/chat/sessions",
                    headers=headers,
                    json={} if args.title is None else {"title": args.title},
                )
            )
            session_id = opened["session_id"]
        answered = response_json(
            client.post(
                f"/v1/chat/sessions/{session_id}/messages",
                # Always present. The route requires it, and a turn without one
                # is a turn a timed-out client cannot safely retry -- it would
                # ask the model a second time and pay for it twice.
                headers={**headers, "Idempotency-Key": _chat_key(args)},
                json={
                    "question": args.question,
                    "knowledge_base_id": args.knowledge_base_id,
                    "top_k": args.top_k,
                },
            )
        )

    # The session id is echoed whether it was supplied or opened here: it is
    # the only way a caller continues the conversation, and a command that
    # opened one without saying so would strand it.
    payload = {**answered, "session_id": session_id}
    if args.json:
        render_result(payload, as_json=True, stream=stream)
        return 0
    _render_answer(payload, stream)
    return 0


def run_approval(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory = default_http_client,
) -> int:
    headers = identity_headers(args)
    with _client(args, http_client_factory) as client:
        if args.approval_command == "list":
            params: dict[str, Any] = {"limit": args.limit}
            if args.status is not None:
                params["status"] = args.status
            if args.cursor is not None:
                params["cursor"] = args.cursor
            payload = response_json(
                client.get("/v1/approvals", headers=headers, params=params)
            )
            render_result(payload, as_json=args.json, stream=stream)
            return 0
        if args.approval_command == "get":
            payload = response_json(
                client.get(f"/v1/approvals/{args.approval_id}", headers=headers)
            )
            render_result(payload, as_json=args.json, stream=stream)
            return 0
        payload = response_json(
            client.post(
                f"/v1/approvals/{args.approval_id}/decisions",
                headers=headers,
                json={
                    "decision": args.approval_command,
                    "decision_version": args.decision_version,
                },
            )
        )
    render_result(payload, as_json=args.json, stream=stream)
    return 0


def run_artifact(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory = default_http_client,
) -> int:
    """Read one artifact back, to a file or to this stream."""

    headers = identity_headers(args)
    with _client(args, http_client_factory) as client:
        response = client.get(f"/v1/artifacts/{args.artifact_id}", headers=headers)
    if response.status_code >= 400:
        raise CliHttpError(
            code=_artifact_error(response.status_code),
            status_code=response.status_code,
        )

    if args.output is None:
        stream.write(response.text)
        if not response.text.endswith("\n"):
            stream.write("\n")
        return 0
    with open(args.output, "wb") as handle:
        handle.write(response.content)
    render_result(
        {
            "artifact_id": args.artifact_id,
            "path": args.output,
            "bytes": len(response.content),
            # Reported by the server alongside the body. Printing it lets a
            # caller verify the file it just wrote without a second request.
            "sha256": response.headers.get("x-artifact-sha256", ""),
        },
        as_json=args.json,
        stream=stream,
    )
    return 0


def _chat_key(args: Any) -> str:
    if args.idempotency_key is not None:
        return args.idempotency_key
    return f"cli_{uuid.uuid4().hex}"


def _artifact_error(status_code: int) -> str:
    return "not_found" if status_code == 404 else "request_failed"


def _client(args: Any, factory: HttpClientFactory) -> httpx.Client:
    return factory(args.api_url, args.timeout_seconds)


def _render_search(payload: dict[str, Any], stream: TextIO) -> None:
    hits = payload.get("hits", [])
    stream.write(f"retriever: {payload.get('retriever', 'unknown')}\n")
    stream.write(f"hits: {len(hits)}\n")
    for hit in hits:
        stream.write(f"\n  {hit.get('chunk_id')}  {hit.get('document_id')}")
        stream.write(f"  (rev {hit.get('document_version')})\n")
        stream.write(f"  {_one_line(str(hit.get('text', '')))}\n")


def _render_answer(payload: dict[str, Any], stream: TextIO) -> None:
    stream.write(f"session_id: {payload.get('session_id')}\n")
    stream.write(f"withheld: {payload.get('withheld')}\n")
    citations = payload.get("citations", [])
    stream.write(f"citations: {len(citations)}\n")
    for citation in citations:
        stream.write(f"  {citation.get('chunk_id')}  {citation.get('document_id')}\n")
    stream.write(f"\n{payload.get('answer', '')}\n")


def _one_line(text: str, *, width: int = 96) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= width:
        return collapsed
    return collapsed[: width - 1] + "…"


__all__ = ["run_approval", "run_artifact", "run_chat", "run_search"]
