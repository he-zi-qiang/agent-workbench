"""Task CLI commands speak only the public HTTP contract."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from typing import Any

import httpx

from agent_workbench.apps.cli.main import main
from agent_workbench.apps.cli.task import EXIT_CONFLICT, EXIT_NOT_FOUND

IDENTITY = ("--tenant-id", "tenant_a", "--principal-id", "user_1")


def _client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[str, float], httpx.Client]:
    def factory(base_url: str, timeout_seconds: float) -> httpx.Client:
        assert base_url == "http://api.test"
        assert timeout_seconds == 30.0
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
        ("task", "--api-url", "http://api.test", *IDENTITY, *argv),
        stream=output,
        http_client_factory=_client_factory(handler),
    )
    return code, output.getvalue()


def test_submit_sends_identity_and_idempotency_key_and_renders_json() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={"task_id": "task_1", "status": "queued", "status_detail": None},
        )

    code, output = _run(
        "submit",
        "--objective",
        "summarize the handbook",
        "--max-revisions",
        "3",
        "--idempotency-key",
        "retry_1",
        "--json",
        handler=handler,
    )

    assert code == 0
    assert captured["method"] == "POST"
    assert captured["url"] == "http://api.test/v1/tasks"
    assert captured["headers"]["x-tenant-id"] == "tenant_a"
    assert captured["headers"]["x-principal-id"] == "user_1"
    assert captured["headers"]["idempotency-key"] == "retry_1"
    assert captured["body"] == {
        "objective": "summarize the handbook",
        "max_revisions": 3,
    }
    assert json.loads(output) == {
        "idempotency_key": "retry_1",
        "status": "queued",
        "status_detail": None,
        "task_id": "task_1",
    }


def test_submit_forwards_the_chosen_graph_and_omits_an_unchosen_one() -> None:
    """The flag is a shape, and absence is the deployment's default.

    The control is the exact-body assertion in the identity test above: a
    submit without ``--graph`` sends the same bytes it sent before the flag
    existed, so the server -- not this client's opinion of its default --
    decides which graph an unchoosing caller gets (ADR-031 §2.3).
    """

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"task_id": "task_1", "status": "queued"})

    code, _ = _run(
        "submit",
        "--objective",
        "clean the ledger",
        "--graph",
        "general",
        "--json",
        handler=handler,
    )

    assert code == 0
    assert captured["body"]["graph"] == "general"


def test_submit_generates_and_displays_an_idempotency_key() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers["idempotency-key"]
        return httpx.Response(201, json={"task_id": "task_1", "status": "queued"})

    code, output = _run("submit", "--objective", "do work", "--json", handler=handler)

    assert code == 0
    assert captured["key"].startswith("cli_")
    assert json.loads(output)["idempotency_key"] == captured["key"]


def test_conflict_is_stable_and_does_not_print_the_server_detail() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "internal conflict secret"})

    code, output = _run(
        "submit",
        "--objective",
        "do work",
        "--idempotency-key",
        "retry_1",
        "--json",
        handler=handler,
    )

    assert code == EXIT_CONFLICT
    assert json.loads(output) == {"error": "request_conflict", "status": 409}
    assert "internal conflict secret" not in output


def test_not_found_is_stable_and_does_not_print_the_server_detail() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "task_1 belongs to another user"})

    code, output = _run("get", "task_1", "--json", handler=handler)

    assert code == EXIT_NOT_FOUND
    # ``not_found`` rather than ``task_not_found``: five commands now share one
    # error table, and a per-command prefix would mean the table had to know
    # which command called it -- which is the coupling sharing it removed.
    assert json.loads(output) == {"error": "not_found", "status": 404}
    assert "another user" not in output


def test_get_uses_the_task_control_endpoint() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"task_id": "task_1", "status": "running"})

    code, output = _run("get", "task_1", "--json", handler=handler)

    assert code == 0
    assert captured == {"method": "GET", "path": "/v1/tasks/task_1"}
    assert json.loads(output)["status"] == "running"


def test_timeline_forwards_cursor_and_prints_the_next_cursor() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["cursor"] = request.url.params["cursor"]
        captured["limit"] = request.url.params["limit"]
        return httpx.Response(
            200,
            json={
                "task_id": "task_1",
                "events": [{"event_type": "TaskSubmitted", "sequence": 1}],
                "cursor": "thr_1:1",
            },
        )

    code, output = _run(
        "timeline",
        "task_1",
        "--cursor",
        "thr_1:0",
        "--limit",
        "25",
        "--json",
        handler=handler,
    )

    assert code == 0
    assert captured == {
        "path": "/v1/tasks/task_1/timeline",
        "cursor": "thr_1:0",
        "limit": "25",
    }
    assert json.loads(output)["cursor"] == "thr_1:1"


def test_cancel_uses_the_task_control_endpoint() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"task_id": "task_1", "status": "cancelled"})

    code, output = _run(
        "cancel", "task_1", "--reason", "operator request", "--json", handler=handler
    )

    assert code == 0
    assert captured == {
        "method": "POST",
        "path": "/v1/tasks/task_1/cancel",
        "body": {"reason": "operator request"},
    }
    assert json.loads(output)["status"] == "cancelled"
