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

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from agent_workbench.domain.errors import (
    AgentWorkbenchError,
    ErrorInfo,
    OperationCancelledError,
    ToolPairingError,
)
from agent_workbench.domain.events import (
    ContextBuilt,
    ModelCompleted,
    ModelDelta,
    ModelStarted,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
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
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec, align_results
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.ports.model import (
    ModelEvent,
    ModelFinishReason,
    ModelPort,
    ModelRequest,
    ModelTextDelta,
    ModelToolCallProposed,
    ModelUsageReported,
)
from agent_workbench.runtime.budgets import (
    effective_model_deadline,
    remaining_run_seconds,
)
from agent_workbench.runtime.state import RunStateMachine
from agent_workbench.runtime.tool_gateway import PreparedCall, ToolGateway
from agent_workbench.runtime.tool_scheduler import (
    DEFAULT_MAX_PARALLEL_READS,
    plan_tool_batches,
)

DEFAULT_MODEL_LABEL = "scripted-fake"

# Mirrors runtime.model_timeout_seconds. It is the runtime's own envelope for
# any single model call; the adapter still applies the model profile's timeout
# inside it, so the shorter of the two fires first.
DEFAULT_MODEL_TIMEOUT_SECONDS = 120.0

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
    # Set when the turn failed for a reason the loop must report as something
    # other than a plain error, such as running out of run deadline.
    stop_reason: StopReason | None = None


@dataclass(slots=True)
class _RunLedger:
    """Everything that accumulates across turns of one run."""

    messages: list[Message]
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    answer: str = ""


@runtime_checkable
class _Closable(Protocol):
    """Anything that can be told the caller is finished with it."""

    async def aclose(self) -> None: ...


async def _aclose(stream: AsyncIterator[ModelEvent]) -> None:
    """Close a stream that can be closed, whatever concrete type it is.

    ``aclose`` is the protocol; ``AsyncGenerator`` is only the most common
    thing that satisfies it.
    """

    if isinstance(stream, _Closable):
        await stream.aclose()


def _repeated_call_ids(calls: Sequence[ToolCall]) -> tuple[str, ...]:
    """Ids proposed more than once in one turn, in the order first repeated.

    A tool_call_id is what a result answers to. Two calls sharing one leave no
    way to say which result belongs to which, so the turn is not something this
    runtime can execute -- whatever the model meant by it.
    """

    seen: set[str] = set()
    repeated: list[str] = []
    for call in calls:
        if call.tool_call_id in seen and call.tool_call_id not in repeated:
            repeated.append(call.tool_call_id)
        seen.add(call.tool_call_id)
    return tuple(repeated)


class ClaudeLikeAgentRuntime:
    """Runs one agent's model-tool loop to a terminal outcome."""

    def __init__(
        self,
        *,
        model: ModelPort,
        gateway: ToolGateway,
        policy_identity: str,
        model_label: str = DEFAULT_MODEL_LABEL,
        model_timeout_seconds: float | None = DEFAULT_MODEL_TIMEOUT_SECONDS,
        max_parallel_read_tools: int = DEFAULT_MAX_PARALLEL_READS,
        clock: Callable[[], datetime] | None = None,
        model_call_ids: Callable[[], str] | None = None,
    ) -> None:
        self._model = model
        # Tools are reached only through the gateway: resolving, validating,
        # authorizing and running them is one component's job, not the loop's.
        self._gateway = gateway
        # The operator label paired with the fingerprint of the rules it claims
        # to describe. It is recorded with every decision this run makes.
        self._policy_identity = policy_identity
        self._model_label = model_label
        self._model_timeout_seconds = model_timeout_seconds
        # Mirrors runtime.max_parallel_read_tools. Exclusive tools ignore it:
        # they are always a group of one.
        self._max_parallel_read_tools = max_parallel_read_tools
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

        if request.budget.max_cost_micro_usd is not None:
            # Nothing produces cost_micro_usd: no pricer exists, so this
            # ceiling would sit at zero for the whole run and never fire. A
            # limit that cannot be enforced must not be accepted as one --
            # the caller asked for a guarantee, and silently not providing it
            # is worse than refusing. Delete this branch in the change that
            # introduces a cost meter, not before.
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                ErrorInfo(
                    code="invalid_tool_input",
                    message=(
                        "max_cost_micro_usd was requested, but this runtime has "
                        "no cost meter and cannot enforce a cost ceiling"
                    ),
                ),
                ledger,
            )

        try:
            specs = self._gateway.advertise(request.tool_names)
        except AgentWorkbenchError as exc:
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                exc.to_error_info(),
                ledger,
            )

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
            turn = await self._stream_model(
                request,
                sink,
                ledger,
                specs,
                cancellation,
            )
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

            terminal = await self._run_tool_batch(
                request,
                sink,
                machine,
                cancellation,
                turn,
                ledger,
                context=context,
            )
            if terminal is not None:
                return terminal

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

    async def _stream_model(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        ledger: _RunLedger,
        specs: tuple[ToolSpec, ...],
        cancellation: CancellationToken,
    ) -> _ModelTurn:
        deadline = effective_model_deadline(
            envelope_seconds=self._model_timeout_seconds,
            run_deadline=request.budget.deadline,
            now=self._clock(),
        )
        if deadline.expired:
            # No time left to start: reported before a model call is paid for.
            return _ModelTurn(
                text="",
                calls=(),
                usage=TokenUsage(),
                finish="error",
                error=deadline.to_error(),
                stop_reason=deadline.stop_reason(),
            )

        model_call_id = self._model_call_ids()
        await sink.emit(
            ModelStarted(
                model_call_id=model_call_id,
                model_profile=request.model_profile,
                model_id=self._model_label,
            )
        )

        stream = self._model.stream(
            ModelRequest(
                model_profile=request.model_profile,
                system_prompt=request.system_prompt,
                messages=tuple(ledger.messages),
                tools=specs,
            )
        )
        try:
            async with asyncio.timeout(deadline.seconds):
                turn = await self._consume(stream, sink, model_call_id, cancellation)
        except TimeoutError:
            return _ModelTurn(
                text="",
                calls=(),
                usage=TokenUsage(),
                finish="error",
                error=deadline.to_error(),
                stop_reason=deadline.stop_reason(),
            )
        except OperationCancelledError:
            return _ModelTurn("", (), TokenUsage(), "cancelled", None)
        except Exception as exc:
            # An adapter fault is a run outcome, not the caller's exception.
            return _ModelTurn(
                text="",
                calls=(),
                usage=TokenUsage(),
                finish="error",
                error=ErrorInfo.from_exception(exc, default_code="provider_error"),
            )

        if turn.finish is not None and turn.finish != "cancelled":
            await sink.emit(
                ModelCompleted(
                    model_call_id=model_call_id,
                    finish_reason=turn.finish,
                    usage=turn.usage,
                    text=turn.text,
                    tool_call_ids=tuple(call.tool_call_id for call in turn.calls),
                )
            )
        return turn

    async def _consume(
        self,
        stream: AsyncIterator[ModelEvent],
        sink: EventSink,
        model_call_id: str,
        cancellation: CancellationToken,
    ) -> _ModelTurn:
        """Drain one model stream, stopping early if the run was cancelled."""

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        tokens = TokenUsage()
        finish: ModelFinishReason | None = None
        error: ErrorInfo | None = None

        try:
            async for event in stream:
                if cancellation.cancelled:
                    # Observed at the next event boundary. A stream that goes
                    # quiet instead is bounded by the deadline above.
                    return _ModelTurn("", (), tokens, "cancelled", None)
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
        finally:
            # Closing the stream is how cancellation and deadlines reach the
            # adapter, and through it whatever connection it holds open. The
            # port promises an AsyncIterator, not an AsyncGenerator, so asking
            # for the concrete type meant an adapter that returned any other
            # closable iterator was simply never closed -- and a leak of that
            # shape shows up as exhausted connections under load, far from the
            # line that caused it.
            await _aclose(stream)

        return _ModelTurn(
            _clip("".join(text_parts)), tuple(calls), tokens, finish, error
        )

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
                turn.stop_reason or "error",
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

        # Checked here, after this turn's tokens are in the ledger and before
        # either continuing or completing. The top-of-loop check runs before a
        # turn and so cannot see what that turn spent; without this, a run that
        # blew its token ceiling reported "completed", and one that blew it
        # while proposing tools went on to run them.
        exceeded = request.budget.stop_reason_for(ledger.usage, now=self._clock())
        if exceeded is not None:
            return await self._failed(
                request,
                sink,
                machine,
                exceeded,
                ErrorInfo(
                    code="budget_exceeded",
                    message=f"the run passed its ceiling: {exceeded}",
                ),
                ledger,
            )

        if turn.calls:
            duplicated = _repeated_call_ids(turn.calls)
            if duplicated:
                # Checked before anything is prepared, authorized or run. The
                # pairing rule that catches this otherwise runs after the
                # handlers, which meant a model repeating an id got its tool
                # executed once per repetition and the run then died on the
                # bookkeeping. A malformed proposal must cost nothing.
                return await self._failed(
                    request,
                    sink,
                    machine,
                    "error",
                    ErrorInfo(
                        code="provider_error",
                        message=(
                            "the model proposed the same tool_call_id more "
                            f"than once: {', '.join(duplicated)}"
                        ),
                    ),
                    ledger,
                )
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
        request: AgentRunRequest,
        sink: EventSink,
        machine: RunStateMachine,
        cancellation: CancellationToken,
        turn: _ModelTurn,
        ledger: _RunLedger,
        *,
        context: ExecutionContext,
    ) -> AgentOutcome | None:
        """Take one batch of proposed calls through the gateway's phases."""

        machine.to("validating_tools")
        for call in turn.calls:
            await self._gateway.propose(call, sink=sink)

        # The ceiling is spent before the batch runs, not counted after it. A
        # turn proposing more calls than remain used to run all of them and
        # report the overrun afterwards, which is an accounting entry, not a
        # limit: the side effects had already happened.
        allowance = max(request.budget.max_tool_calls - ledger.usage.tool_calls, 0)
        admitted = turn.calls[:allowance]

        # Re-read for each phase: the run's deadline bounds the policy engine
        # and the hooks exactly as it bounds the tools they guard, and a slow
        # phase leaves less for the next.
        def remaining() -> float | None:
            return remaining_run_seconds(request.budget.deadline, now=self._clock())

        results: list[ToolResult] = []
        for call in turn.calls[allowance:]:
            results.append(
                await self._gateway.refuse(
                    call,
                    ErrorInfo(
                        code="budget_exceeded",
                        message=(
                            "the run reached its tool-call ceiling before this "
                            "call was dispatched"
                        ),
                    ),
                    sink=sink,
                )
            )

        prepared: list[PreparedCall] = []
        for call in admitted:
            outcome = await self._gateway.prepare(
                call,
                context=context,
                sink=sink,
                remaining_run_seconds=remaining(),
            )
            if isinstance(outcome, ToolResult):
                results.append(outcome)
            else:
                prepared.append(outcome)

        authorized: list[PreparedCall] = []
        if prepared:
            machine.to("authorizing")
            for candidate in prepared:
                outcome = await self._gateway.authorize(
                    candidate,
                    context=context,
                    sink=sink,
                    remaining_run_seconds=remaining(),
                )
                if isinstance(outcome, ToolResult):
                    results.append(outcome)
                else:
                    authorized.append(outcome)

        if authorized:
            machine.to("executing_tools")
            for group in plan_tool_batches(
                authorized,
                max_parallel=self._max_parallel_read_tools,
            ):
                if cancellation.cancelled:
                    # The run is ending, but these ids were already shown to
                    # the model and still owe an answer. Groups already in
                    # flight keep their real results.
                    results.extend(await self._refuse_cancelled(group, sink=sink))
                    continue
                results.extend(
                    await self._run_group(
                        group,
                        request=request,
                        context=context,
                        cancellation=cancellation,
                        sink=sink,
                    )
                )

        machine.to("recording_results")
        try:
            aligned = align_results(turn.calls, results)
        except ToolPairingError as exc:
            # Unreachable by way of duplicate ids, which are refused before
            # dispatch. It stays because the caller was promised a terminal
            # outcome: a graph node needs something it can record and route on,
            # and a traceback is neither. An invariant this runtime broke is
            # still this runtime's to report.
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                exc.to_error_info(),
                ledger,
            )
        ledger.messages.append(assistant_message(text=turn.text, tool_calls=turn.calls))
        ledger.messages.append(tool_message(aligned))
        # Only the admitted calls are charged. A call refused *because* the
        # ceiling was reached must not itself consume the ceiling, or the
        # ledger would report spending more than the budget allowed.
        ledger.usage = ledger.usage.merged(BudgetUsage(tool_calls=len(admitted)))

    async def _run_group(
        self,
        group: tuple[PreparedCall, ...],
        *,
        request: AgentRunRequest,
        context: ExecutionContext,
        cancellation: CancellationToken,
        sink: EventSink,
    ) -> tuple[ToolResult, ...]:
        """Run one group, concurrently when it holds more than one call."""

        # Recomputed per group: a slow group leaves less for the next, and the
        # run's deadline bounds every one of them.
        budget = remaining_run_seconds(request.budget.deadline, now=self._clock())

        async def invoke(prepared: PreparedCall) -> ToolResult:
            return await self._gateway.invoke(
                prepared,
                context=context,
                cancellation=cancellation,
                sink=sink,
                run_budget_seconds=budget,
            )

        if len(group) == 1:
            return (await invoke(group[0]),)
        return tuple(await asyncio.gather(*(invoke(prepared) for prepared in group)))

    async def _refuse_cancelled(
        self,
        group: tuple[PreparedCall, ...],
        *,
        sink: EventSink,
    ) -> tuple[ToolResult, ...]:
        return tuple(
            [
                await self._gateway.refuse(
                    prepared.call,
                    ErrorInfo(
                        code="cancelled",
                        message="the run was cancelled before this call ran",
                    ),
                    sink=sink,
                )
                for prepared in group
            ]
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
