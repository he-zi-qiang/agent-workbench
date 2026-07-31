"""Domain error taxonomy.

The runtime must answer one question deterministically for every failure: what
``ToolResult`` does it become? Runtime invariant 1 says each exposed
``tool_call_id`` ends with exactly one result -- including unknown tools,
invalid arguments, denials, timeouts, handler exceptions and cancellation --
so failures need a closed set of machine codes rather than free-form strings.

Translating a foreign exception deliberately drops its message. Provider SDKs
put credentials, request bodies and prompt fragments into exception text, and
that text would otherwise travel into events, traces and the model's own
context, where it becomes both a leak and an injection vector.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Final, Literal

from pydantic import StringConstraints

from agent_workbench.domain.schema import DomainModel

ErrorCode = Literal[
    "unknown_tool",
    "invalid_tool_input",
    "policy_denied",
    "approval_required",
    "tool_timeout",
    "tool_failed",
    "cancelled",
    "budget_exceeded",
    "output_too_large",
    "not_found",
    "stale_execution",
    "incompatible_schema",
    "provider_error",
    "provider_unavailable",
    "internal_error",
]

ERROR_MESSAGE_MAX_LENGTH: Final[int] = 1024

ErrorMessage = Annotated[
    str,
    StringConstraints(min_length=1, max_length=ERROR_MESSAGE_MAX_LENGTH),
]


def _clip(message: str) -> str:
    normalized = " ".join(message.split())
    if len(normalized) <= ERROR_MESSAGE_MAX_LENGTH:
        return normalized
    return normalized[: ERROR_MESSAGE_MAX_LENGTH - 1] + "…"


class ErrorInfo(DomainModel):
    """Serializable failure description.

    ``message`` is operator-facing text that also reaches the model as a tool
    result. It must stay free of secrets, raw provider payloads and untrusted
    document content.
    """

    code: ErrorCode
    message: ErrorMessage
    retryable: bool = False

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        default_code: ErrorCode = "internal_error",
    ) -> ErrorInfo:
        """Normalize any exception into a leak-free ``ErrorInfo``."""

        if isinstance(exc, AgentWorkbenchError):
            return exc.to_error_info()
        # Only the exception type crosses the boundary: a third-party message
        # is untrusted content of unknown provenance.
        return cls(
            code=default_code,
            message=f"unhandled {type(exc).__name__}",
            retryable=False,
        )


class AgentWorkbenchError(Exception):
    """Base class for failures with a stable machine code."""

    code: ClassVar[ErrorCode] = "internal_error"
    retryable: ClassVar[bool] = False

    def to_error_info(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code,
            message=_clip(str(self)) or self.code,
            retryable=self.retryable,
        )


class UnknownToolError(AgentWorkbenchError):
    """The model proposed a tool that is not in the registry."""

    code: ClassVar[ErrorCode] = "unknown_tool"


class ToolInputInvalidError(AgentWorkbenchError):
    """Tool arguments failed schema validation, before or after a hook."""

    code: ClassVar[ErrorCode] = "invalid_tool_input"


class PolicyDeniedError(AgentWorkbenchError):
    """The policy engine denied the call; no handler ran."""

    code: ClassVar[ErrorCode] = "policy_denied"


class ApprovalRequiredError(AgentWorkbenchError):
    """The call needs a human decision at a workflow boundary."""

    code: ClassVar[ErrorCode] = "approval_required"


class ToolTimeoutError(AgentWorkbenchError):
    """A tool exceeded its per-call deadline."""

    code: ClassVar[ErrorCode] = "tool_timeout"
    retryable: ClassVar[bool] = True


class ToolFailedError(AgentWorkbenchError):
    """A tool handler raised or reported a failure."""

    code: ClassVar[ErrorCode] = "tool_failed"


class OperationCancelledError(AgentWorkbenchError):
    """Cancellation propagated into the model call, tool or run."""

    code: ClassVar[ErrorCode] = "cancelled"


class BudgetExceededError(AgentWorkbenchError):
    """A step, tool, token, cost or deadline budget stopped the run."""

    code: ClassVar[ErrorCode] = "budget_exceeded"


class OutputTooLargeError(AgentWorkbenchError):
    """A result exceeded its configured size ceiling."""

    code: ClassVar[ErrorCode] = "output_too_large"


class NotFoundError(AgentWorkbenchError):
    """The object does not exist, or the caller may not learn that it does.

    Both cases raise the same error on purpose. If a cross-tenant lookup failed
    differently from a missing id, the difference would itself confirm that the
    object exists.
    """

    code: ClassVar[ErrorCode] = "not_found"


class StaleExecutionError(AgentWorkbenchError):
    """A fenced write was rejected: the lease or epoch is no longer current.

    This never enters an ordinary retry policy. The worker lost its claim, so
    the correct response is to cancel and let another worker reclaim the task.
    """

    code: ClassVar[ErrorCode] = "stale_execution"


class IncompatibleSchemaError(AgentWorkbenchError):
    """A persisted payload declares a schema version this process cannot run."""

    code: ClassVar[ErrorCode] = "incompatible_schema"


class ProviderError(AgentWorkbenchError):
    """An adapter failure, described without leaking SDK types or payloads."""

    code: ClassVar[ErrorCode] = "provider_error"
    retryable: ClassVar[bool] = True


class ToolPairingError(AgentWorkbenchError):
    """Tool calls and tool results do not form a one-to-one mapping."""

    code: ClassVar[ErrorCode] = "internal_error"


__all__ = [
    "ERROR_MESSAGE_MAX_LENGTH",
    "AgentWorkbenchError",
    "ApprovalRequiredError",
    "BudgetExceededError",
    "ErrorCode",
    "ErrorInfo",
    "ErrorMessage",
    "IncompatibleSchemaError",
    "NotFoundError",
    "OperationCancelledError",
    "OutputTooLargeError",
    "PolicyDeniedError",
    "ProviderError",
    "StaleExecutionError",
    "ToolFailedError",
    "ToolInputInvalidError",
    "ToolPairingError",
    "ToolTimeoutError",
    "UnknownToolError",
]
