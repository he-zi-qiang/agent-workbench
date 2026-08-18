"""Running one authorized handler, always answering it, and saying so meanwhile.

This is the narrow place where a tool call becomes a tool result. Whatever the
handler does -- return, raise, hang, or come back with something the result
contract rejects -- exactly one ``ToolResult`` leaves this method, because the
model is already holding a ``tool_call_id`` that has to be closed.

The timeout lives here rather than in the loop above it. A serial loop has no
other way to survive a handler that never returns: budgets bound how much a run
may spend, not how long a single await may block.

Three limits apply: the tool's own declared timeout, whatever is left of the
run's deadline, and the deployment's optional ceiling on any one tool call. The
shortest wins, because a tool allowed an hour inside a run with ten seconds left
would outlive the run that authorized it. Only the first can be raised to give a
tool more time -- the other two may shorten a call and never lengthen it.

**And it is the place that says the call is still running** (ADR-068). The
runtime already streams what the model is writing, token by token, through
``ModelDelta`` and ``ModelThinkingDelta``; between ``ToolStarted`` and
``ToolCompleted`` it streamed nothing at all, for as long as a tool takes --
which for ``sandbox_run`` is a declared 300 seconds. A reader watching that gap
cannot tell a script that is working from a script that has hung, and neither
can they tell it from a Worker that died. Two things close the gap, and they
are different in kind:

* what the handler chooses to say, through the reporter on its invocation;
* what this executor knows without asking anyone, which is how long the call
  has been running. Every tool gets that one, including every tool that has
  never heard of this channel.

Both are best-effort observations. A progress report that cannot be delivered
is dropped, never raised: the call's result is the thing this module owes its
caller, and a broken observer must not become a failed tool.

What is deliberately missing is argument validation against the tool schema and
the hook that may rewrite those arguments. Both belong to the tool gateway, and
both change what "final arguments" means, so they arrive together rather than
half now.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import perf_counter
from typing import Final

from agent_workbench.domain.errors import ErrorInfo, OperationCancelledError
from agent_workbench.domain.events import ToolProgress
from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.schema import ShortText
from agent_workbench.domain.tools import ToolCall, ToolResult
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.ports.tools import ToolBinding, ToolInvocation
from agent_workbench.runtime.budgets import effective_tool_timeout

#: How often a running call reports its own clock, when nothing else is being
#: reported.
#:
#: Five seconds, from both ends of what the number has to satisfy. Below it is
#: the reader: the gap this exists to close is measured in minutes, and a clock
#: that ticks slower than roughly this often is one a reader checks, sees
#: unchanged, and stops trusting. Above it is the buffer: these are transient
#: events sharing ``event_stream.subscriber_buffer_events`` (256) with model
#: deltas, which arrive orders of magnitude faster, so the beat must cost
#: nothing next to them -- at five seconds a call at the sandbox's full 300
#: produces 60 of them.
#:
#: Not a configuration key, and that is a decision rather than an omission
#: (ADR-068 §4). Nothing about this number changes what a run may do, what it
#: may reach, or what is recorded -- ``ToolProgress`` is transient and never
#: persisted. `runtime.tool_timeout_seconds` is the cautionary case: a knob
#: every profile restated and no code read. A deployment that needs a different
#: cadence passes it to the constructor.
DEFAULT_PROGRESS_HEARTBEAT_SECONDS: Final[float] = 5.0

#: What ``ToolProgress.message`` can hold, restated here because this module is
#: the one that has to keep a handler's sentence from becoming a validation
#: error. See ``_ProgressChannel.report``.
_MESSAGE_LIMIT: Final[int] = 256


class _ProgressChannel:
    """The reporter one call gets, and the clock that beats beside it.

    Holds the ``tool_call_id`` rather than accepting one, which is the whole
    reason a handler is given this instead of the sink: a handler cannot report
    progress against a call other than its own, and cannot report progress that
    is missing the id a console needs to place it.
    """

    __slots__ = ("_closed", "_sink", "_tool_call_id")

    def __init__(self, tool_call_id: str, sink: EventSink | None) -> None:
        self._tool_call_id = tool_call_id
        self._sink = sink
        self._closed = False

    async def report(self, message: str, *, percent: int | None = None) -> None:
        """The handler-facing half. Satisfies ``ToolProgressReporter``.

        Every argument is normalized rather than validated, because the caller
        is a handler in the middle of doing its job and the alternative to
        normalizing is raising *inside* it -- turning an attempt to describe
        work into the reason that work failed. A sentence too long is cut, a
        percentage out of range is clamped, and a message with nothing in it is
        simply not sent.
        """

        text = message.strip()
        if not text:
            return
        await self._emit(
            message=text[: _MESSAGE_LIMIT - 1] + "…"
            if len(text) > _MESSAGE_LIMIT
            else text,
            percent=None if percent is None else max(0, min(100, percent)),
            elapsed_ms=None,
        )

    async def beat(self, elapsed_ms: int) -> None:
        """The executor's half: the clock, with nothing said about it.

        No ``percent``, deliberately. Elapsed time against a declared timeout
        looks like a progress fraction and is not one -- a script 30 seconds
        into a 300-second allowance is not 10% finished, and a bar that fills
        at a rate unrelated to the work is worse than no bar, because a reader
        who believes it waits instead of intervening.
        """

        await self._emit(message=None, percent=None, elapsed_ms=elapsed_ms)

    def close(self) -> None:
        """Stop accepting reports.

        A handler that leaves a task behind, or a beat already in flight when
        the handler returned, would otherwise emit progress for a call that has
        already reported its result -- reopening a settled step on screen.
        """

        self._closed = True

    async def _emit(
        self,
        *,
        message: ShortText | None,
        percent: int | None,
        elapsed_ms: int | None,
    ) -> None:
        if self._closed or self._sink is None:
            return
        try:
            await self._sink.emit(
                ToolProgress(
                    tool_call_id=self._tool_call_id,
                    message=message,
                    percent=percent,
                    elapsed_ms=elapsed_ms,
                )
            )
        except asyncio.CancelledError:
            # The run is being torn down, and the teardown is entitled to see
            # this. Swallowing it here would make a cancelled tool call one
            # that reports progress forever.
            raise
        except Exception:
            # Everything else: the sink is an observer of this call, not a
            # participant in it. A full subscriber buffer, a closed stream or a
            # persistence fault must not travel back into a handler that was
            # only saying what it was doing.
            return


class ToolExecutor:
    """Executes one already-authorized call under its declared timeout."""

    __slots__ = ("_deployment_ceiling_seconds", "_heartbeat_seconds", "_monotonic")

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] | None = None,
        deployment_ceiling_seconds: float | None = None,
        progress_heartbeat_seconds: float | None = DEFAULT_PROGRESS_HEARTBEAT_SECONDS,
    ) -> None:
        # Injected so a demo or a golden test can produce stable durations;
        # production reads the real monotonic clock.
        self._monotonic = monotonic if monotonic is not None else perf_counter
        # `runtime.tool_timeout_seconds`, normally unset. See
        # `effective_tool_timeout`: it may only shorten a call, never lengthen.
        self._deployment_ceiling_seconds = deployment_ceiling_seconds
        # `None` switches the clock off entirely, which is what a test that
        # counts a call's events wants; handler-authored reports still flow.
        if progress_heartbeat_seconds is not None and progress_heartbeat_seconds <= 0:
            raise ValueError("progress_heartbeat_seconds must be positive, or None")
        self._heartbeat_seconds = progress_heartbeat_seconds

    async def execute(
        self,
        binding: ToolBinding,
        call: ToolCall,
        *,
        context: ExecutionContext,
        cancellation: CancellationToken,
        run_budget_seconds: float | None = None,
        sink: EventSink | None = None,
    ) -> ToolResult:
        limit = effective_tool_timeout(
            binding.spec.timeout_seconds,
            run_budget_seconds=run_budget_seconds,
            deployment_ceiling_seconds=self._deployment_ceiling_seconds,
        )
        started = self._monotonic()

        if limit <= 0:
            # Before the channel exists, and before the clock starts. Nothing
            # ran, so there is no progress to report about it -- the call is
            # already over by the time this executor was asked.
            return self._failure(
                call, self._timeout_error(call, limit, binding), started
            )

        # `sink` is optional because this executor's contract is to return one
        # `ToolResult`, and it can do that with nobody watching. A direct unit
        # test and a golden demo both call it that way; the gateway, which is
        # the only caller in a running system, always passes one.
        channel = _ProgressChannel(call.tool_call_id, sink)
        invocation = ToolInvocation(
            call=call,
            context=context,
            cancellation=cancellation,
            timeout_seconds=binding.spec.timeout_seconds,
            progress=channel.report,
        )
        heartbeat = self._start_heartbeat(channel, started)

        try:
            return await self._run(binding, call, invocation, limit, started)
        finally:
            # Order matters: close first, so that a beat which wakes up between
            # these two lines finds the channel shut rather than emitting after
            # the result.
            channel.close()
            if heartbeat is not None:
                heartbeat.cancel()

    def _start_heartbeat(
        self, channel: _ProgressChannel, started: float
    ) -> asyncio.Task[None] | None:
        if self._heartbeat_seconds is None:
            return None
        task = asyncio.ensure_future(self._beat(channel, started))
        # A cancelled task whose result nobody retrieves is one asyncio prints
        # in full at interpreter shutdown, and this one is cancelled on every
        # single tool call. Same reasoning as the tool gateway's abandoned
        # approval waiter.
        task.add_done_callback(_discard_outcome)
        return task

    async def _beat(self, channel: _ProgressChannel, started: float) -> None:
        """Report elapsed time until cancelled.

        The first beat waits a full interval, which is what keeps this from
        being noise: the overwhelming majority of tool calls -- a workspace
        read, a search -- finish in milliseconds and emit nothing at all. A
        beat means "this one is taking a while", and it means it from the
        first one.
        """

        assert self._heartbeat_seconds is not None
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            await channel.beat(self._elapsed_ms(started))

    async def _run(
        self,
        binding: ToolBinding,
        call: ToolCall,
        invocation: ToolInvocation,
        limit: float,
        started: float,
    ) -> ToolResult:
        try:
            async with asyncio.timeout(limit):
                result = await binding.handler(invocation)
        except TimeoutError:
            return self._failure(
                call, self._timeout_error(call, limit, binding), started
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

    def _timeout_error(
        self,
        call: ToolCall,
        limit: float,
        binding: ToolBinding,
    ) -> ErrorInfo:
        # Name the bound that actually cut the call. Three can, and reporting
        # the wrong one sends the reader to raise a number that was never the
        # constraint.
        if limit >= binding.spec.timeout_seconds:
            bound = f"its {limit:g}s timeout"
        elif (
            self._deployment_ceiling_seconds is not None
            and limit >= self._deployment_ceiling_seconds
        ):
            bound = f"this deployment's {limit:g}s tool ceiling"
        else:
            bound = f"the run's remaining {limit:g}s"
        return ErrorInfo(
            code="tool_timeout",
            message=f"{call.tool_name} exceeded {bound}",
            retryable=True,
        )

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


def _discard_outcome(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


__all__ = ["DEFAULT_PROGRESS_HEARTBEAT_SECONDS", "ToolExecutor"]
