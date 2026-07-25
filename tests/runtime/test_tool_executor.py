"""One handler in, exactly one result out -- whatever the handler does."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from itertools import count

from agent_workbench.domain.errors import ErrorInfo, OperationCancelledError
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolBinding, ToolHandler, ToolInvocation
from agent_workbench.runtime.tool_executor import ToolExecutor

CONTEXT = ExecutionContext(
    principal=PrincipalContext(principal_id="user_1", tenant_id="tenant_a"),
    envelope=AuthorizationEnvelope(allowed_tools=("slow_tool",)),
    agent_run_id="run_1",
    policy_identity="policy-test:0000000000000000",
)
CALL = ToolCall(tool_call_id="toolu_1", tool_name="slow_tool")


def _spec(timeout_seconds: int = 1) -> ToolSpec:
    return ToolSpec(
        name="slow_tool",
        description="A tool used to exercise the executor.",
        input_schema={"type": "object"},
        concurrency="parallel",
        risk="read",
        idempotency="safe",
        timeout_seconds=timeout_seconds,
    )


def _ticking(step: float = 0.004) -> Callable[[], float]:
    counter = count()

    def reading() -> float:
        return next(counter) * step

    return reading


def _execute(handler: ToolHandler, *, timeout_seconds: int = 1) -> ToolResult:
    executor = ToolExecutor(monotonic=_ticking())
    binding = ToolBinding(spec=_spec(timeout_seconds), handler=handler)

    return asyncio.run(
        executor.execute(
            binding,
            CALL,
            context=CONTEXT,
            cancellation=NullCancellationToken(),
        )
    )


def test_a_successful_result_passes_through_with_its_duration() -> None:
    async def handler(invocation: ToolInvocation) -> ToolResult:
        return ToolResult.succeeded(invocation.call, content="done")

    result = _execute(handler)

    assert result.status == "ok"
    assert result.content == "done"
    assert result.duration_ms == 4


def test_a_duration_the_handler_measured_itself_is_kept() -> None:
    async def handler(invocation: ToolInvocation) -> ToolResult:
        return ToolResult.succeeded(invocation.call, content="done", duration_ms=17)

    assert _execute(handler).duration_ms == 17


def test_a_raising_handler_becomes_a_failure_without_leaking_its_message() -> None:
    async def handler(invocation: ToolInvocation) -> ToolResult:
        raise RuntimeError("db url postgres://user:sk-ant-canary@host/db")

    result = _execute(handler)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_failed"
    assert "sk-ant-canary" not in result.error.message
    assert result.tool_call_id == CALL.tool_call_id


def test_a_cancelled_handler_reports_cancellation() -> None:
    async def handler(invocation: ToolInvocation) -> ToolResult:
        raise OperationCancelledError("operator stopped the task")

    result = _execute(handler)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "cancelled"


def test_a_handler_that_answers_a_different_call_fails_its_own() -> None:
    """One handler must not be able to break the pairing for two ids."""

    async def handler(invocation: ToolInvocation) -> ToolResult:
        other = ToolCall(tool_call_id="toolu_other", tool_name="slow_tool")
        return ToolResult.succeeded(other, content="wrong call")

    result = _execute(handler)

    assert result.tool_call_id == CALL.tool_call_id
    assert result.status == "error"
    assert result.error is not None
    assert "different tool call" in result.error.message


def test_a_handler_that_never_returns_is_stopped_by_its_own_timeout() -> None:
    """Bounded, deterministic and one second long: the domain floor is 1s.

    A serial loop has no other way to survive a handler that hangs, so this is
    the one place the suite waits on real time.
    """

    async def handler(invocation: ToolInvocation) -> ToolResult:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    result = _execute(handler, timeout_seconds=1)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_timeout"
    assert result.error.retryable is True
    assert "1s timeout" in result.error.message


def test_a_handler_returning_an_invalid_result_fails_the_call() -> None:
    async def handler(invocation: ToolInvocation) -> ToolResult:
        # Longer than the domain's inline ceiling: artifacting a large result
        # belongs to context management, and until then it is a failure.
        return ToolResult.succeeded(invocation.call, content="x" * 9000)

    result = _execute(handler)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_failed"


def test_an_error_result_from_the_handler_is_left_alone() -> None:
    async def handler(invocation: ToolInvocation) -> ToolResult:
        return ToolResult.failed(
            invocation.call,
            ErrorInfo(code="not_found", message="no document doc_9"),
        )

    result = _execute(handler)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "not_found"
    assert result.duration_ms == 4
