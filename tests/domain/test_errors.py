"""Error normalization, including what must never cross the boundary."""

from __future__ import annotations

import pytest

from agent_workbench.domain.errors import (
    ERROR_MESSAGE_MAX_LENGTH,
    AgentWorkbenchError,
    ApprovalRequiredError,
    BudgetExceededError,
    ErrorInfo,
    IncompatibleSchemaError,
    OperationCancelledError,
    OutputTooLargeError,
    PolicyDeniedError,
    ProviderError,
    StaleExecutionError,
    ToolFailedError,
    ToolInputInvalidError,
    ToolTimeoutError,
    UnknownToolError,
)

EXPECTED_CODES = [
    (UnknownToolError, "unknown_tool"),
    (ToolInputInvalidError, "invalid_tool_input"),
    (PolicyDeniedError, "policy_denied"),
    (ApprovalRequiredError, "approval_required"),
    (ToolTimeoutError, "tool_timeout"),
    (ToolFailedError, "tool_failed"),
    (OperationCancelledError, "cancelled"),
    (BudgetExceededError, "budget_exceeded"),
    (OutputTooLargeError, "output_too_large"),
    (StaleExecutionError, "stale_execution"),
    (IncompatibleSchemaError, "incompatible_schema"),
    (ProviderError, "provider_error"),
]


@pytest.mark.parametrize(("error_type", "code"), EXPECTED_CODES)
def test_domain_errors_carry_a_stable_machine_code(
    error_type: type[AgentWorkbenchError],
    code: str,
) -> None:
    info = error_type("something went wrong").to_error_info()

    assert info.code == code
    assert info.message == "something went wrong"


def test_a_foreign_exception_message_never_reaches_error_info() -> None:
    """Provider exceptions embed credentials, prompts and request bodies."""

    canary = "sk-ant-canary-must-not-leak"

    info = ErrorInfo.from_exception(RuntimeError(f"401 unauthorized: {canary}"))

    assert canary not in info.message
    assert info.message == "unhandled RuntimeError"
    assert info.code == "internal_error"


def test_a_foreign_exception_can_be_classified_by_the_caller() -> None:
    info = ErrorInfo.from_exception(TimeoutError("slow"), default_code="tool_failed")

    assert info.code == "tool_failed"


def test_timeouts_and_provider_failures_are_retryable() -> None:
    assert ToolTimeoutError("deadline").to_error_info().retryable is True
    assert ProviderError("502").to_error_info().retryable is True
    assert PolicyDeniedError("denied").to_error_info().retryable is False


def test_stale_execution_is_not_retryable() -> None:
    """A lost lease means reclaim, never retry in place."""

    assert StaleExecutionError("epoch 3 is stale").to_error_info().retryable is False


def test_long_messages_are_clipped_and_normalized() -> None:
    info = AgentWorkbenchError("x" * (ERROR_MESSAGE_MAX_LENGTH * 2)).to_error_info()

    assert len(info.message) == ERROR_MESSAGE_MAX_LENGTH

    collapsed = AgentWorkbenchError("two\n\n  words").to_error_info()
    assert collapsed.message == "two words"


def test_an_empty_message_falls_back_to_the_code() -> None:
    assert UnknownToolError().to_error_info().message == "unknown_tool"
