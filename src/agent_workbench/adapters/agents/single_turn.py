"""One model turn, wired end to end.

This is the walking skeleton's executor. It takes a run request, streams one
model call, emits the unified events and returns a terminal outcome -- and it
owns no tool loop, deliberately. Exactly one component in this system may own
the model-tool loop, and that component is the custom runtime arriving in WP02.
Shipping a second loop here, however small, is precisely the mistake the
architecture baseline forbids.

That makes a tool proposal the interesting case. Runtime invariant 1 says every
exposed ``tool_call_id`` ends with exactly one ``ToolResult``; an executor with
no loop cannot honour that. So a proposal is recorded as a durable event and
then fails the run, rather than being quietly dropped -- a dropped call would
leave the model waiting for an answer that is never coming, which is the exact
failure the invariant exists to prevent.

Everything else it does is the contract the runtime will have to reproduce:
budgets are checked before work starts rather than after it overran, adapter
faults become structured outcomes instead of exceptions, and a truncated answer
is reported as a failure rather than as a quiet success.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from agent_workbench.domain.errors import ErrorInfo, OperationCancelledError
from agent_workbench.domain.events import (
    ContextBuilt,
    ModelCompleted,
    ModelDelta,
    ModelStarted,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolProposed,
)
from agent_workbench.domain.identifiers import new_model_call_id
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    StopReason,
    TokenUsage,
)
from agent_workbench.domain.tools import (
    ToolCall,
    ToolSpec,
    argument_digest,
    canonical_arguments,
)
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.ports.model import (
    ModelFinishReason,
    ModelPort,
    ModelRequest,
    ModelTextDelta,
    ModelToolCallProposed,
    ModelUsageReported,
)
from agent_workbench.ports.tools import ToolRegistry

DEFAULT_MODEL_LABEL = "scripted-fake"

# Matches the domain's BoundedText ceiling. Artifacting a long answer instead of
# clipping it belongs to WP12, together with the context budget it protects.
MAX_OUTPUT_TEXT = 4096
TRUNCATION_MARKER = "… [truncated]"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_TEXT:
        return text
    return text[: MAX_OUTPUT_TEXT - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


class SingleTurnAgentExecutor:
    """Executes exactly one model turn and stops."""

    def __init__(
        self,
        *,
        model: ModelPort,
        registry: ToolRegistry | None = None,
        model_label: str = DEFAULT_MODEL_LABEL,
        clock: Callable[[], datetime] | None = None,
        model_call_ids: Callable[[], str] | None = None,
    ) -> None:
        self._model = model
        self._registry = registry
        # The concrete model id belongs to settings; the executor only knows
        # the label it was handed for tracing.
        self._model_label = model_label
        self._clock = clock if clock is not None else _utc_now
        self._model_call_ids = (
            model_call_ids if model_call_ids is not None else new_model_call_id
        )

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        # The frozen protocol names this parameter `emit`; everything below
        # reads better calling the object what it is.
        sink = emit
        await sink.emit(
            RunStarted(
                run_kind=request.run_kind,
                model_profile=request.model_profile,
                tool_names=request.tool_names,
                budget=request.budget,
            )
        )
        if request.context is not None:
            await sink.emit(
                ContextBuilt(
                    chunk_count=len(request.context.chunks),
                    citation_count=len(request.context.citations),
                    token_estimate=request.context.token_estimate,
                    retrieval_trace_id=request.context.retrieval_trace_id,
                )
            )

        nothing_spent = BudgetUsage()
        if cancellation.cancelled:
            return await self._cancelled(request, sink, nothing_spent)

        # Budgets are a ceiling, so they are consulted before the call rather
        # than after it has already been paid for.
        exhausted = request.budget.stop_reason_for(nothing_spent, now=self._clock())
        if exhausted is not None:
            return await self._failed(
                request,
                sink,
                exhausted,
                ErrorInfo(
                    code="budget_exceeded",
                    message=f"the run had no budget left to start: {exhausted}",
                ),
                nothing_spent,
            )

        specs: list[ToolSpec] = []
        for name in request.tool_names:
            binding = self._registry.get(name) if self._registry is not None else None
            if binding is None:
                return await self._failed(
                    request,
                    sink,
                    "error",
                    ErrorInfo(
                        code="unknown_tool",
                        message=(
                            "the run requested a tool this process does not "
                            f"register: {name}"
                        ),
                    ),
                    nothing_spent,
                )
            specs.append(binding.spec)

        return await self._run_model_turn(request, sink, cancellation, tuple(specs))

    async def _run_model_turn(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        cancellation: CancellationToken,
        specs: tuple[ToolSpec, ...],
    ) -> AgentOutcome:
        model_call_id = self._model_call_ids()
        await sink.emit(
            ModelStarted(
                model_call_id=model_call_id,
                model_profile=request.model_profile,
                model_id=self._model_label,
            )
        )

        text_parts: list[str] = []
        proposals: list[ToolCall] = []
        tokens = TokenUsage()
        finish: ModelFinishReason | None = None
        model_error: ErrorInfo | None = None

        try:
            stream = self._model.stream(
                ModelRequest(
                    model_profile=request.model_profile,
                    system_prompt=request.system_prompt,
                    messages=request.messages,
                    tools=specs,
                )
            )
            async for event in stream:
                if isinstance(event, ModelTextDelta):
                    text_parts.append(event.text)
                    await sink.emit(
                        ModelDelta(model_call_id=model_call_id, text=event.text)
                    )
                elif isinstance(event, ModelToolCallProposed):
                    proposals.append(event.call)
                elif isinstance(event, ModelUsageReported):
                    tokens = event.usage
                else:
                    finish = event.finish_reason
                    model_error = event.error
                    if event.usage.total:
                        tokens = event.usage
        except OperationCancelledError:
            return await self._cancelled(request, sink, BudgetUsage(steps=1))
        except Exception as exc:
            # An adapter fault is a run outcome, not a caller's exception: the
            # caller is a graph node that has to record a result either way.
            return await self._failed(
                request,
                sink,
                "error",
                ErrorInfo.from_exception(exc, default_code="provider_error"),
                BudgetUsage(steps=1),
            )

        usage = BudgetUsage(steps=1, tokens=tokens)
        text = _clip("".join(text_parts))

        if finish is None:
            return await self._failed(
                request,
                sink,
                "error",
                ErrorInfo(
                    code="provider_error",
                    message="the model stream ended without a completion event",
                ),
                usage,
            )

        await sink.emit(
            ModelCompleted(
                model_call_id=model_call_id,
                finish_reason=finish,
                usage=tokens,
                text=text,
                tool_call_ids=tuple(call.tool_call_id for call in proposals),
            )
        )
        for call in proposals:
            await sink.emit(
                ToolProposed(
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    argument_bytes=len(
                        canonical_arguments(call.arguments).encode("utf-8")
                    ),
                    argument_sha256=argument_digest(call.arguments),
                )
            )

        return await self._terminal_outcome(
            request,
            sink,
            cancellation,
            finish=finish,
            model_error=model_error,
            proposals=tuple(proposals),
            text=text,
            usage=usage,
        )

    async def _terminal_outcome(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        cancellation: CancellationToken,
        *,
        finish: ModelFinishReason,
        model_error: ErrorInfo | None,
        proposals: tuple[ToolCall, ...],
        text: str,
        usage: BudgetUsage,
    ) -> AgentOutcome:
        if cancellation.cancelled or finish == "cancelled":
            return await self._cancelled(request, sink, usage)

        if model_error is not None or finish == "error":
            return await self._failed(
                request,
                sink,
                "error",
                model_error
                or ErrorInfo(
                    code="provider_error",
                    message="the model reported an error without describing it",
                ),
                usage,
            )

        if proposals:
            return await self._failed(
                request,
                sink,
                "error",
                ErrorInfo(
                    code="internal_error",
                    message=(
                        "this executor owns no tool loop, so "
                        f"{len(proposals)} proposed tool call(s) were recorded "
                        "and left unanswered; the model-tool loop arrives with "
                        "the custom runtime"
                    ),
                ),
                usage,
            )

        if finish == "tool_use":
            return await self._failed(
                request,
                sink,
                "error",
                ErrorInfo(
                    code="provider_error",
                    message="the model finished for tool use but proposed no call",
                ),
                usage,
            )

        if finish == "max_tokens":
            # A cut-off answer must not reach a graph node looking complete.
            return await self._failed(
                request,
                sink,
                "token_budget",
                ErrorInfo(
                    code="budget_exceeded",
                    message="the model stopped at its output token ceiling",
                ),
                usage,
            )

        await sink.emit(RunCompleted(stop_reason="completed", usage=usage))
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text=text,
            usage=usage,
        )

    async def _failed(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        stop_reason: StopReason,
        error: ErrorInfo,
        usage: BudgetUsage,
    ) -> AgentOutcome:
        await sink.emit(RunFailed(error=error, stop_reason=stop_reason, usage=usage))
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="failed",
            stop_reason=stop_reason,
            error=error,
            usage=usage,
        )

    async def _cancelled(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        usage: BudgetUsage,
    ) -> AgentOutcome:
        await sink.emit(RunCancelled(usage=usage))
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="cancelled",
            stop_reason="cancelled",
            usage=usage,
        )


__all__ = [
    "DEFAULT_MODEL_LABEL",
    "MAX_OUTPUT_TEXT",
    "TRUNCATION_MARKER",
    "SingleTurnAgentExecutor",
]
