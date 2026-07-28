"""A failed agent run is an HTTP failure, not an empty successful answer."""

from __future__ import annotations

import json
from typing import Any

from agent_workbench.application.chat import ChatExecutionError
from agent_workbench.apps.api.main import _render_chat_execution_error
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.runs import AgentOutcome


def _response(outcome: AgentOutcome) -> tuple[int, dict[str, Any]]:
    response = _render_chat_execution_error(
        None,  # pyright: ignore[reportArgumentType]
        ChatExecutionError(outcome),
    )
    return response.status_code, json.loads(bytes(response.body))


def test_a_provider_failure_is_not_reported_as_http_200() -> None:
    status_code, payload = _response(
        AgentOutcome(
            agent_run_id="run_failed",
            status="failed",
            stop_reason="error",
            error=ErrorInfo(code="provider_error", message="provider unavailable"),
        )
    )

    assert status_code == 502
    assert payload["run_id"] == "run_failed"
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "provider_error"


def test_a_cancelled_run_is_not_reported_as_http_200() -> None:
    status_code, payload = _response(
        AgentOutcome(
            agent_run_id="run_cancelled",
            status="cancelled",
            stop_reason="cancelled",
        )
    )

    assert status_code == 409
    assert payload["run_id"] == "run_cancelled"
    assert payload["status"] == "cancelled"


def test_a_run_deadline_is_reported_as_a_gateway_timeout() -> None:
    status_code, payload = _response(
        AgentOutcome(
            agent_run_id="run_deadline",
            status="failed",
            stop_reason="deadline",
            error=ErrorInfo(code="budget_exceeded", message="deadline reached"),
        )
    )

    assert status_code == 504
    assert payload["stop_reason"] == "deadline"
