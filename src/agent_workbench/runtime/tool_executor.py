"""Running one authorized handler, and always answering it.

This is the narrow place where a tool call becomes a tool result. Whatever the
handler does -- return, raise, hang, or come back with something the result
contract rejects -- exactly one ``ToolResult`` leaves this method, because the
model is already holding a ``tool_call_id`` that has to be closed.

The timeout lives here rather than in the loop above it. A serial loop has no
other way to survive a handler that never returns: budgets bound how much a run
may spend, not how long a single await may block.

What is deliberately missing is argument validation against the tool schema and
the hook that may rewrite those arguments. Both belong to the tool gateway, and
both change what "final arguments" means, so they arrive together rather than
half now.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import perf_counter

from agent_workbench.domain.errors import ErrorInfo, OperationCancelledError
from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.tools import ToolCall, ToolResult
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.tools import ToolBinding, ToolInvocation


class ToolExecutor:
    """Executes one already-authorized call under its declared timeout."""

    __slots__ = ("_monotonic",)

    def __init__(self, *, monotonic: Callable[[], float] | None = None) -> None:
        # Injected so a demo or a golden test can produce stable durations;
        # production reads the real monotonic clock.
        self._monotonic = monotonic if monotonic is not None else perf_counter

    async def execute(
        self,
        binding: ToolBinding,
        call: ToolCall,
        *,
        context: ExecutionContext,
        cancellation: CancellationToken,
    ) -> ToolResult:
        invocation = ToolInvocation(
            call=call,
            context=context,
            cancellation=cancellation,
            timeout_seconds=binding.spec.timeout_seconds,
        )
        started = self._monotonic()

        try:
            async with asyncio.timeout(binding.spec.timeout_seconds):
                result = await binding.handler(invocation)
        except TimeoutError:
            return self._failure(
                call,
                ErrorInfo(
                    code="tool_timeout",
                    message=(
                        f"{call.tool_name} exceeded its "
                        f"{binding.spec.timeout_seconds}s timeout"
                    ),
                    retryable=True,
                ),
                started,
            )
        except OperationCancelledError as exc:
            return self._failure(call, exc.to_error_info(), started)
        except Exception as exc:
            # A handler fault is this call's result, not the run's exception:
            # the loop still owes the model an answer for this id.
            return self._failure(
                call,
                ErrorInfo.from_exception(exc, default_code="tool_failed"),
                started,
            )

        if (
            result.tool_call_id != call.tool_call_id
            or result.tool_name != call.tool_name
        ):
            # One handler answering the wrong call would break the pairing for
            # two ids at once. It fails this call instead.
            return self._failure(
                call,
                ErrorInfo(
                    code="tool_failed",
                    message=f"{call.tool_name} answered a different tool call",
                ),
                started,
            )
        return self._attach_duration(result, started)

    def _failure(
        self,
        call: ToolCall,
        error: ErrorInfo,
        started: float,
    ) -> ToolResult:
        return ToolResult.failed(call, error, duration_ms=self._elapsed_ms(started))

    def _attach_duration(self, result: ToolResult, started: float) -> ToolResult:
        if result.duration_ms is not None:
            return result
        return result.model_copy(update={"duration_ms": self._elapsed_ms(started)})

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._monotonic() - started) * 1000))


__all__ = ["ToolExecutor"]
