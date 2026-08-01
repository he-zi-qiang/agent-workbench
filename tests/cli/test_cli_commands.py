"""Search, chat, approval and artifact commands, against a mocked transport.

What is under test is the request each command sends and the exit code it
returns, not the server. A real API would make these tests about assembly and
would hide the thing that actually matters here: that the CLI is a client, with
no second route to a store and no way to name an identity the headers did not
carry.

One property gets its own attention. ``--scope`` is what makes the export path
reachable, and a command that dropped it would fail in the Worker with a policy
denial nobody would connect back to a missing flag -- so every HTTP command is
checked for it by name.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_workbench.apps.cli.http import (
    EXIT_NOT_FOUND,
    EXIT_TRANSPORT_ERROR,
)
from agent_workbench.apps.cli.main import main

IDENTITY = ("--tenant-id", "tenant_a", "--principal-id", "user_1")


def _client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[str, float], httpx.Client]:
    def factory(base_url: str, timeout_seconds: float) -> httpx.Client:
        return httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=httpx.MockTransport(handler),
        )

    return factory


def _run(
    *argv: str,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[int, str]:
    output = io.StringIO()
    code = main(
        (*argv,),
        stream=output,
        http_client_factory=_client_factory(handler),
    )
    return code, output.getvalue()


def _endpoint(*argv: str, scopes: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Identity flags sit on the command, not the subcommand.

    ``agent-cli approval --tenant-id t --principal-id p list`` -- the same shape
    ``task`` already had, so every HTTP command reads the same way.
    """

    scope_flags = tuple(flag for scope in scopes for flag in ("--scope", scope))
    return (argv[0], "--api-url", "http://api.test", *IDENTITY, *scope_flags, *argv[1:])


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def test_search_posts_the_query_and_renders_the_packet() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "chunk_id": "chk_1",
                        "document_id": "doc_1",
                        "document_version": "3",
                        "text": "Qdrant performs one fusion per query.",
                    }
                ],
                "citations": [],
                "retriever": "hybrid+rerank",
            },
        )

    code, output = _run(
        *_endpoint(
            "search", "--query", "how does fusion run", "--knowledge-base-id", "kb_1"
        ),
        handler=handler,
    )

    assert code == 0
    assert captured["url"] == "http://api.test/v1/search"
    assert captured["body"] == {
        "query": "how does fusion run",
        "knowledge_base_id": "kb_1",
        "top_k": 8,
    }
    # No identity in the body. It is a header, and a body field would let a
    # caller name whose documents to search.
    assert "principal_id" not in captured["body"]
    assert captured["headers"]["x-tenant-id"] == "tenant_a"
    # Which retriever answered is part of the answer: a result set means
    # something different under each.
    assert "hybrid+rerank" in output
    assert "chk_1" in output


def test_search_without_a_retrieval_stack_reports_the_refusal() -> None:
    """The API returns 409 when it assembled no retrieval. Not a crash here."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "no retrieval stack"})

    code, output = _run(
        *_endpoint("search", "--query", "x", "--knowledge-base-id", "kb_1", "--json"),
        handler=handler,
    )

    assert code == 4
    assert json.loads(output) == {"error": "request_conflict", "status": 409}
    assert "no retrieval stack" not in output


# --------------------------------------------------------------------------
# chat
# --------------------------------------------------------------------------


def test_chat_ask_opens_a_session_and_reports_which_one() -> None:
    """A command that opened a session without saying so would strand it."""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/chat/sessions":
            return httpx.Response(201, json={"session_id": "sess_opened"})
        return httpx.Response(
            200,
            json={
                "answer": "Inside the database.",
                "citations": [{"chunk_id": "chk_1", "document_id": "doc_1"}],
                "withheld": False,
                "run_id": "run_1",
                "turn_id": "turn_1",
            },
        )

    code, output = _run(
        *_endpoint(
            "chat",
            "ask",
            "--question",
            "where does fusion run",
            "--knowledge-base-id",
            "kb_1",
            "--json",
        ),
        handler=handler,
    )

    assert code == 0
    assert seen == [
        "POST /v1/chat/sessions",
        "POST /v1/chat/sessions/sess_opened/messages",
    ]
    assert json.loads(output)["session_id"] == "sess_opened"


def test_chat_ask_with_a_session_does_not_open_another() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "answer": "a",
                "citations": [],
                "withheld": False,
                "run_id": "run_1",
                "turn_id": "turn_1",
            },
        )

    code, output = _run(
        *_endpoint(
            "chat",
            "ask",
            "--question",
            "q",
            "--knowledge-base-id",
            "kb_1",
            "--session-id",
            "sess_mine",
            "--json",
        ),
        handler=handler,
    )

    assert code == 0
    assert seen == ["/v1/chat/sessions/sess_mine/messages"]
    assert json.loads(output)["session_id"] == "sess_mine"


def test_chat_sends_an_idempotency_key_it_was_given() -> None:
    """A repeated key returns the first answer instead of asking again."""

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "answer": "a",
                "citations": [],
                "withheld": False,
                "run_id": "run_1",
                "turn_id": "turn_1",
            },
        )

    _run(
        *_endpoint(
            "chat",
            "ask",
            "--question",
            "q",
            "--knowledge-base-id",
            "kb_1",
            "--session-id",
            "sess_1",
            "--idempotency-key",
            "key_1",
        ),
        handler=handler,
    )

    assert captured["headers"]["idempotency-key"] == "key_1"


def test_chat_against_a_deployment_without_a_model_is_not_found() -> None:
    """The route is not registered at all there, which is a 404 by design."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    code, output = _run(
        *_endpoint("chat", "history", "--session-id", "sess_1", "--json"),
        handler=handler,
    )

    assert code == EXIT_NOT_FOUND
    assert json.loads(output) == {"error": "not_found", "status": 404}


# --------------------------------------------------------------------------
# approval
# --------------------------------------------------------------------------


def test_approval_list_asks_for_the_pending_queue() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"approvals": [], "cursor": None})

    code, _ = _run(
        *_endpoint("approval", "list", "--status", "pending", "--limit", "10"),
        handler=handler,
    )

    assert code == 0
    assert "status=pending" in captured["url"]
    assert "limit=10" in captured["url"]


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_a_decision_posts_the_answer_and_its_version(decision: str) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "approval_id": "apr_1",
                "task_id": "task_1",
                "status": decision,
                "decision_version": 1,
                "decided_at": "2026-07-31T09:00:00Z",
                "created_at": "2026-07-31T08:00:00Z",
            },
        )

    code, output = _run(
        *_endpoint("approval", decision, "apr_1", "--json"),
        handler=handler,
    )

    assert code == 0
    assert captured["url"] == "http://api.test/v1/approvals/apr_1/decisions"
    assert captured["body"] == {"decision": decision, "decision_version": 1}
    assert json.loads(output)["status"] == decision


def test_deciding_an_approval_that_moved_is_a_conflict_not_a_crash() -> None:
    """The Task may have been cancelled while a person was thinking."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "task is cancelled"})

    code, output = _run(
        *_endpoint("approval", "approved", "apr_1", "--json"), handler=handler
    )

    assert code == 4
    assert json.loads(output) == {"error": "request_conflict", "status": 409}


# --------------------------------------------------------------------------
# artifact
# --------------------------------------------------------------------------


def test_artifact_get_writes_the_bytes_and_reports_the_digest(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "report.md"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/artifacts/art_1"
        return httpx.Response(
            200,
            content=b"# Task report\n",
            headers={"x-artifact-sha256": "a" * 64},
        )

    code, output = _run(
        *_endpoint("artifact", "get", "art_1", "--output", str(destination), "--json"),
        handler=handler,
    )

    assert code == 0
    assert destination.read_bytes() == b"# Task report\n"
    rendered = json.loads(output)
    assert rendered["bytes"] == 14
    # Reported by the server with the body, so the file can be checked without
    # a second request.
    assert rendered["sha256"] == "a" * 64


def test_artifact_get_without_an_output_writes_to_the_stream() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"body text")

    code, output = _run(*_endpoint("artifact", "get", "art_1"), handler=handler)

    assert code == 0
    assert output == "body text\n"


def test_an_artifact_that_is_not_yours_is_the_same_answer_as_absent() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    code, output = _run(
        *_endpoint("artifact", "get", "art_1", "--json"), handler=handler
    )

    assert code == EXIT_NOT_FOUND
    assert json.loads(output) == {"error": "not_found", "status": 404}


# --------------------------------------------------------------------------
# Identity, shared by every command above
# --------------------------------------------------------------------------


SCOPED_COMMANDS = [
    ("search", "--query", "q", "--knowledge-base-id", "kb_1"),
    (
        "chat",
        "ask",
        "--question",
        "q",
        "--knowledge-base-id",
        "kb_1",
        "--session-id",
        "s",
    ),
    ("approval", "list"),
    ("artifact", "get", "art_1"),
    ("task", "list"),
]


@pytest.mark.parametrize("argv", SCOPED_COMMANDS, ids=lambda a: a[0] + ":" + a[1])
def test_every_http_command_forwards_the_declared_scopes(
    argv: tuple[str, ...],
) -> None:
    """The flag that makes the export path reachable.

    A command that dropped it would fail inside the Worker with a policy denial
    nobody would trace back to a missing argument, so this is asserted per
    command rather than once.
    """

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("headers", dict(request.headers))
        return httpx.Response(200, json={"ok": True, "session_id": "s"})

    code, _ = _run(
        *_endpoint(*argv, scopes=("artifact:export", "knowledge:read")),
        handler=handler,
    )

    assert code == 0
    assert captured["headers"]["x-principal-scopes"] == (
        "artifact:export,knowledge:read"
    )


def test_no_scope_flag_sends_no_scope_header() -> None:
    """An empty header and a missing one both mean "no scopes".

    Sending the empty string makes a request look like it decided something.
    """

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"approvals": [], "cursor": None})

    _run(*_endpoint("approval", "list"), handler=handler)

    assert "x-principal-scopes" not in captured["headers"]


def test_a_transport_failure_is_its_own_category() -> None:
    """Not a server answer, so not reported as a request that failed."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    code, output = _run(*_endpoint("approval", "list", "--json"), handler=handler)

    assert code == EXIT_TRANSPORT_ERROR
    assert json.loads(output) == {"error": "transport_error"}
