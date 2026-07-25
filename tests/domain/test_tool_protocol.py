"""Tool specification rules and the one-result-per-call invariant."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_workbench.domain.errors import ErrorInfo, ToolPairingError
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec, align_results


def _spec(**overrides: object) -> ToolSpec:
    defaults: dict[str, object] = {
        "name": "read_document",
        "description": "Read one indexed document.",
        "input_schema": {"type": "object"},
        "concurrency": "parallel",
        "risk": "read",
        "idempotency": "safe",
        "timeout_seconds": 30,
    }
    return ToolSpec.model_validate(defaults | overrides)


def _call(tool_call_id: str = "toolu_1", tool_name: str = "read_document") -> ToolCall:
    return ToolCall(tool_call_id=tool_call_id, tool_name=tool_name)


def test_side_effecting_tools_must_cross_an_exclusive_barrier() -> None:
    with pytest.raises(ValidationError, match="must be exclusive"):
        _spec(
            name="export_artifact",
            risk="write",
            concurrency="parallel",
            idempotency="keyed",
            permission_scopes=("artifact:write",),
        )


def test_side_effecting_tools_must_declare_a_permission_scope() -> None:
    with pytest.raises(ValidationError, match="permission scope"):
        _spec(
            name="export_artifact",
            risk="write",
            concurrency="exclusive",
            idempotency="keyed",
        )


def test_read_tools_must_be_idempotent() -> None:
    with pytest.raises(ValidationError, match="safe idempotency"):
        _spec(idempotency="unsafe")


def test_tool_input_schema_must_describe_an_object() -> None:
    with pytest.raises(ValidationError, match="JSON Schema object"):
        _spec(input_schema={"type": "array"})


def test_permission_scopes_are_normalized() -> None:
    spec = _spec(
        name="export_artifact",
        risk="external",
        concurrency="exclusive",
        idempotency="keyed",
        permission_scopes=("net:write", "artifact:write", "net:write"),
    )
    assert spec.permission_scopes == ("artifact:write", "net:write")


def test_a_call_may_name_a_tool_that_does_not_exist() -> None:
    """An unknown tool still has to become exactly one ToolResult."""

    call = _call(tool_name="Definitely Not Registered")
    result = ToolResult.failed(
        call,
        ErrorInfo(code="unknown_tool", message="no such tool"),
    )
    assert align_results([call], [result]) == (result,)


def test_a_call_rejects_a_control_character_tool_name() -> None:
    with pytest.raises(ValidationError):
        _call(tool_name="read\ndocument")


def test_a_failed_result_carries_an_error_and_a_successful_one_does_not() -> None:
    call = _call()
    with pytest.raises(ValidationError, match="ErrorInfo"):
        ToolResult(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            status="error",
        )
    with pytest.raises(ValidationError, match="ErrorInfo"):
        ToolResult(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            status="ok",
            error=ErrorInfo(code="tool_failed", message="but reported ok"),
        )


def test_results_are_submitted_in_call_order_not_completion_order() -> None:
    calls = [_call("toolu_1"), _call("toolu_2"), _call("toolu_3")]
    completed_out_of_order = [
        ToolResult.succeeded(calls[2], content="third"),
        ToolResult.succeeded(calls[0], content="first"),
        ToolResult.succeeded(calls[1], content="second"),
    ]

    aligned = align_results(calls, completed_out_of_order)

    assert [result.content for result in aligned] == ["first", "second", "third"]


def test_a_missing_result_is_a_protocol_error() -> None:
    calls = [_call("toolu_1"), _call("toolu_2")]

    with pytest.raises(ToolPairingError, match="missing ToolResult"):
        align_results(calls, [ToolResult.succeeded(calls[0])])


def test_a_duplicate_result_is_a_protocol_error() -> None:
    call = _call()

    with pytest.raises(ToolPairingError, match="duplicate ToolResult"):
        align_results([call], [ToolResult.succeeded(call), ToolResult.succeeded(call)])


def test_a_result_without_a_call_is_a_protocol_error() -> None:
    call = _call("toolu_1")
    orphan = ToolResult.succeeded(_call("toolu_orphan"))

    with pytest.raises(ToolPairingError, match="without a matching call"):
        align_results([call], [ToolResult.succeeded(call), orphan])


def test_a_result_must_report_the_tool_the_call_proposed() -> None:
    call = _call("toolu_1", "read_document")
    mismatched = ToolResult.succeeded(_call("toolu_1", "export_artifact"))

    with pytest.raises(ToolPairingError, match="but the call proposed"):
        align_results([call], [mismatched])


def test_a_handler_exception_becomes_the_mandatory_result() -> None:
    call = _call()

    result = ToolResult.from_exception(call, RuntimeError("boom"), duration_ms=12)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_failed"
    assert result.tool_call_id == call.tool_call_id
