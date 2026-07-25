"""The custom agent runtime: one model-tool loop, owned in one place.

This is the only component in the system that runs ``model -> tool -> result ->
model``. LangGraph advances a long task, LlamaIndex puts knowledge into an
index, and neither of them takes a turn of this loop; a second executor behind
the same protocol is the failure the architecture baseline exists to prevent.

Two properties hold on every path through it.

Every ``tool_call_id`` the model was shown ends with exactly one
``ToolResult``. Unknown tool, denied call, handler exception, timeout,
cancellation mid-batch: each of them produces a result rather than a gap,
because the model is waiting on the id either way and a missing answer is a
conversation that can never continue.

And results are submitted in the model's own call order. Execution is serial
here, so the two orders happen to coincide; the alignment is applied anyway,
because the parallel scheduler that arrives later must not be able to change
what the model sees by finishing one tool sooner than another.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agent_workbench.domain.errors import ErrorInfo, OperationCancelledError
from agent_workbench.domain.events import (
    ContextBuilt,
    ModelCompleted,
    ModelDelta,
    ModelStarted,
    PermissionResolved,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolCompleted,
    ToolFailed,
    ToolProposed,
    ToolStarted,
)
from agent_workbench.domain.identifiers import new_model_call_id
from agent_workbench.domain.messages import (
    Message,
    assistant_message,
    tool_message,
)
from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    StopReason,
    TokenUsage,
)
from agent_workbench.domain.tools import (
    ToolCall,
    ToolResult,
    ToolRisk,
    ToolSpec,
    align_results,
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
from agent_workbench.ports.policy import PolicyEngine
from agent_workbench.ports.tools import ToolBinding, ToolRegistry
from agent_workbench.runtime.state import RunStateMachine
from agent_workbench.runtime.tool_executor import ToolExecutor

DEFAULT_MODEL_LABEL = "scripted-fake"

# Matches the domain's BoundedText ceiling. Writing a longer answer to the
# artifact store instead of clipping it belongs with context management.
MAX_OUTPUT_TEXT = 4096
TRUNCATION_MARKER = "… [truncated]"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_TEXT:
        return text
    return text[: MAX_OUTPUT_TEXT - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


@dataclass(frozen=True, slots=True)
class _ModelTurn:
    """What one model call produced."""

    text: str
    calls: tuple[ToolCall, ...]
    usage: TokenUsage
    finish: ModelFinishReason | None
    error: ErrorInfo | None


@dataclass(slots=True)
class _RunLedger:
    """Everything that accumulates across turns of one run."""

    messages: list[Message]
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    answer: str = ""


class ClaudeLikeAgentRuntime:
    """Runs one agent's model-tool loop to a terminal outcome."""

    def __init__(
        self,
        *,
        model: ModelPort,
        registry: ToolRegistry,
        policy: PolicyEngine,
        policy_identity: str,
        executor: ToolExecutor | None = None,
        model_label: str = DEFAULT_MODEL_LABEL,
        clock: Callable[[], datetime] | None = None,
        model_call_ids: Callable[[], str] | None = None,
    ) -> None:
        self._model = model
        self._registry = registry
        self._policy = policy
        # The operator label paired with the fingerprint of the rules it claims
        # to describe. It is recorded with every decision this run makes.
        self._policy_identity = policy_identity
        self._executor = executor if executor is not None else ToolExecutor()
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
        machine = RunStateMachine()
        ledger = _RunLedger(messages=list(request.messages))
        context = self._execution_context(request)

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

        specs = self._resolve_advertised_tools(request)
        if isinstance(specs, ErrorInfo):
            return await self._failed(request, sink, machine, "error", specs, ledger)

        while True:
            if cancellation.cancelled:
                return await self._cancelled(request, sink, machine, ledger)

            # Ceilings are consulted before a turn starts. A budget that only
            # triggers after the spend is not a ceiling, and an unbounded loop
            # is exactly what a model proposing tools forever would produce.
            exhausted = request.budget.stop_reason_for(
                ledger.usage,
                now=self._clock(),
            )
            if exhausted is not None:
                return await self._failed(
                    request,
                    sink,
                    machine,
                    exhausted,
                    ErrorInfo(
                        code="budget_exceeded",
                        message=f"the run stopped at its ceiling: {exhausted}",
                    ),
                    ledger,
                )

            machine.to("model_streaming")
            turn = await self._stream_model(request, sink, ledger, specs)
            ledger.usage = ledger.usage.merged(BudgetUsage(steps=1, tokens=turn.usage))

            terminal = await self._terminal_for_turn(
                request,
                sink,
                machine,
                cancellation,
                turn,
                ledger,
            )
            if terminal is not None:
                return terminal

            await self._run_tool_batch(
                sink,
                machine,
                cancellation,
                turn,
                ledger,
                context=context,
            )

            if cancellation.cancelled:
                return await self._cancelled(request, sink, machine, ledger)

    def _execution_context(self, request: AgentRunRequest) -> ExecutionContext:
        return ExecutionContext(
            principal=request.principal,
            envelope=request.envelope,
            agent_run_id=request.trace.agent_run_id,
            policy_identity=self._policy_identity,
            task_id=request.trace.task_id,
            workflow_thread_id=request.trace.workflow_thread_id,
            graph_node_id=request.trace.graph_node_id,
        )

    def _resolve_advertised_tools(
        self,
        request: AgentRunRequest,
    ) -> tuple[ToolSpec, ...] | ErrorInfo:
        specs: list[ToolSpec] = []
        for name in request.tool_names:
            binding = self._registry.get(name)
            if binding is None:
                return ErrorInfo(
                    code="unknown_tool",
                    message=(
                        f"the run requested a tool this process does not "
                        f"register: {name}"
                    ),
                )
            specs.append(binding.spec)
        return tuple(specs)

    async def _stream_model(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        ledger: _RunLedger,
        specs: tuple[ToolSpec, ...],
    ) -> _ModelTurn:
        model_call_id = self._model_call_ids()
        await sink.emit(
            ModelStarted(
                model_call_id=model_call_id,
                model_profile=request.model_profile,
                model_id=self._model_label,
            )
        )

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        tokens = TokenUsage()
        finish: ModelFinishReason | None = None
        error: ErrorInfo | None = None

        try:
            stream = self._model.stream(
                ModelRequest(
                    model_profile=request.model_profile,
                    system_prompt=request.system_prompt,
                    messages=tuple(ledger.messages),
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
                    calls.append(event.call)
                elif isinstance(event, ModelUsageReported):
                    tokens = event.usage
                else:
                    finish = event.finish_reason
                    error = event.error
                    if event.usage.total:
                        tokens = event.usage
        except OperationCancelledError:
            return _ModelTurn("", (), tokens, "cancelled", None)
        except Exception as exc:
            # An adapter fault is a run outcome, not the caller's exception.
            return _ModelTurn(
                "",
                (),
                tokens,
                "error",
                ErrorInfo.from_exception(exc, default_code="provider_error"),
            )

        text = _clip("".join(text_parts))
        if finish is not None:
            await sink.emit(
                ModelCompleted(
                    model_call_id=model_call_id,
                    finish_reason=finish,
                    usage=tokens,
                    text=text,
                    tool_call_ids=tuple(call.tool_call_id for call in calls),
                )
            )
        return _ModelTurn(text, tuple(calls), tokens, finish, error)

    async def _terminal_for_turn(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        machine: RunStateMachine,
        cancellation: CancellationToken,
        turn: _ModelTurn,
        ledger: _RunLedger,
    ) -> AgentOutcome | None:
        """Map one model turn onto a terminal outcome, or ``None`` to continue."""

        if turn.finish == "cancelled" or cancellation.cancelled:
            return await self._cancelled(request, sink, machine, ledger)

        if turn.finish is None:
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                ErrorInfo(
                    code="provider_error",
                    message="the model stream ended without a completion event",
                ),
                ledger,
            )

        if turn.error is not None or turn.finish == "error":
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                turn.error
                or ErrorInfo(
                    code="provider_error",
                    message="the model reported an error without describing it",
                ),
                ledger,
            )

        if turn.finish == "max_tokens":
            # A cut-off answer must not reach its caller looking complete.
            return await self._failed(
                request,
                sink,
                machine,
                "token_budget",
                ErrorInfo(
                    code="budget_exceeded",
                    message="the model stopped at its output token ceiling",
                ),
                ledger,
            )

        if turn.calls:
            return None

        if turn.finish == "tool_use":
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                ErrorInfo(
                    code="provider_error",
                    message="the model finished for tool use but proposed no call",
                ),
                ledger,
            )

        ledger.answer = turn.text
        machine.to("completed")
        await sink.emit(RunCompleted(stop_reason="completed", usage=ledger.usage))
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text=turn.text,
            usage=ledger.usage,
        )

    async def _run_tool_batch(
        self,
        sink: EventSink,
        machine: RunStateMachine,
        cancellation: CancellationToken,
        turn: _ModelTurn,
        ledger: _RunLedger,
        *,
        context: ExecutionContext,
    ) -> None:
        machine.to("validating_tools")
        for call in turn.calls:
            await sink.emit(
                ToolProposed(
                    tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    argument_bytes=len(
                        canonical_arguments(call.arguments).encode("utf-8")
                    ),
                    argument_sha256=argument_digest(call.arguments),
                    risk=self._risk_of(call),
                )
            )

        results: list[ToolResult] = []
        known: list[tuple[ToolBinding, ToolCall]] = []
        for call in turn.calls:
            binding = self._registry.get(call.tool_name)
            if binding is None:
                results.append(
                    await self._record_failure(
                        sink,
                        call,
                        ErrorInfo(
                            code="unknown_tool",
                            message=f"no tool named {call.tool_name}",
                        ),
                    )
                )
            else:
                known.append((binding, call))

        authorized: list[tuple[ToolBinding, ToolCall]] = []
        if known:
            machine.to("authorizing")
            for binding, call in known:
                decision = await self._policy.decide(call, context)
                await sink.emit(
                    PermissionResolved(
                        tool_call_id=call.tool_call_id,
                        effect=decision.effect,
                        reason_code=decision.reason_code,
                    )
                )
                if decision.effect == "allow":
                    authorized.append((binding, call))
                    continue
                if decision.effect == "deny":
                    results.append(
                        await self._record_failure(
                            sink,
                            call,
                            ErrorInfo(
                                code="policy_denied",
                                message=f"denied: {decision.reason_code}",
                            ),
                        )
                    )
                    continue
                # Rewritten arguments have to be re-validated against the tool
                # schema and re-authorized before they may run. The gateway
                # that does both is not here yet, so the call is refused
                # rather than executed on unchecked input.
                results.append(
                    await self._record_failure(
                        sink,
                        call,
                        ErrorInfo(
                            code="policy_denied",
                            message=(
                                "argument rewriting requires the tool gateway's "
                                "re-validation"
                            ),
                        ),
                    )
                )

        if authorized:
            machine.to("executing_tools")
            for binding, call in authorized:
                if cancellation.cancelled:
                    # The run is ending, but this id was already shown to the
                    # model and still owes an answer.
                    results.append(
                        await self._record_failure(
                            sink,
                            call,
                            ErrorInfo(
                                code="cancelled",
                                message="the run was cancelled before this call ran",
                            ),
                        )
                    )
                    continue
                await sink.emit(
                    ToolStarted(
                        tool_call_id=call.tool_call_id,
                        tool_name=call.tool_name,
                    )
                )
                result = await self._executor.execute(
                    binding,
                    call,
                    context=context,
                    cancellation=cancellation,
                )
                await self._record_result(sink, result)
                results.append(result)

        machine.to("recording_results")
        aligned = align_results(turn.calls, results)
        ledger.messages.append(assistant_message(text=turn.text, tool_calls=turn.calls))
        ledger.messages.append(tool_message(aligned))
        ledger.usage = ledger.usage.merged(BudgetUsage(tool_calls=len(turn.calls)))

    def _risk_of(self, call: ToolCall) -> ToolRisk | None:
        binding = self._registry.get(call.tool_name)
        return binding.spec.risk if binding is not None else None

    async def _record_failure(
        self,
        sink: EventSink,
        call: ToolCall,
        error: ErrorInfo,
    ) -> ToolResult:
        result = ToolResult.failed(call, error)
        await self._record_result(sink, result)
        return result

    async def _record_result(self, sink: EventSink, result: ToolResult) -> None:
        if result.status == "error" and result.error is not None:
            await sink.emit(
                ToolFailed(
                    tool_call_id=result.tool_call_id,
                    error=result.error,
                    duration_ms=result.duration_ms or 0,
                )
            )
            return
        await sink.emit(
            ToolCompleted(
                tool_call_id=result.tool_call_id,
                duration_ms=result.duration_ms or 0,
                output_bytes=len(result.content.encode("utf-8")),
                artifact=result.artifact,
            )
        )

    async def _failed(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        machine: RunStateMachine,
        stop_reason: StopReason,
        error: ErrorInfo,
        ledger: _RunLedger,
    ) -> AgentOutcome:
        machine.to("failed")
        await sink.emit(
            RunFailed(error=error, stop_reason=stop_reason, usage=ledger.usage)
        )
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="failed",
            stop_reason=stop_reason,
            output_text=ledger.answer,
            error=error,
            usage=ledger.usage,
        )

    async def _cancelled(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        machine: RunStateMachine,
        ledger: _RunLedger,
    ) -> AgentOutcome:
        machine.to("cancelled")
        await sink.emit(RunCancelled(usage=ledger.usage))
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="cancelled",
            stop_reason="cancelled",
            output_text=ledger.answer,
            usage=ledger.usage,
        )


__all__ = [
    "DEFAULT_MODEL_LABEL",
    "MAX_OUTPUT_TEXT",
    "TRUNCATION_MARKER",
    "ClaudeLikeAgentRuntime",
]
