"""One handler in, exactly one result out -- whatever the handler does."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from itertools import count

from agent_workbench.domain.errors import ErrorInfo, OperationCancelledError
from agent_workbench.domain.events import EventEnvelope, EventPayload, ToolProgress
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventKey
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
    """A result the domain refuses becomes a tool error, not a crashed run.

    The ceiling this crosses is ``ToolOutputText``, the backstop for a tool with
    no budget of its own. It used to be ``BoundedText`` at 4096, which a normal
    retrieval result also crossed -- so this property was being asserted with a
    number that made an ordinary search fail too. The property is unchanged;
    the size that violates it is now one no real tool produces.
    """

    async def handler(invocation: ToolInvocation) -> ToolResult:
        # Past the backstop. Artifacting a result this large belongs to context
        # management, and until then it is a failure.
        return ToolResult.succeeded(invocation.call, content="x" * 70_000)

    result = _execute(handler)

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_failed"


def test_a_result_a_real_tool_produces_is_not_refused() -> None:
    """The control group, and the regression this pairs with.

    ``knowledge_search`` renders ``MAX_TOP_K`` passages of this project's
    512-token chunks, which is about 42,000 characters. Under the old ceiling
    the executor turned that into a tool error, and the model reported that
    search was "failing with a validation error on every attempt".
    """

    async def handler(invocation: ToolInvocation) -> ToolResult:
        return ToolResult.succeeded(invocation.call, content="x" * 42_000)

    result = _execute(handler)

    assert result.status == "ok"


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


def test_a_ceiling_cut_blames_the_ceiling_and_not_the_run() -> None:
    """Three bounds can cut a call, and only one of them is worth raising.

    Reporting the wrong one is how `external_search` spent a day looking like a
    slow network: the message has to name the limit the reader can act on.
    """

    async def handler(invocation: ToolInvocation) -> ToolResult:
        await asyncio.sleep(10)
        raise AssertionError("the timeout should have fired first")

    executor = ToolExecutor(monotonic=_ticking(), deployment_ceiling_seconds=0.01)
    binding = ToolBinding(spec=_spec(timeout_seconds=90), handler=handler)

    result = asyncio.run(
        executor.execute(
            binding,
            CALL,
            context=CONTEXT,
            cancellation=NullCancellationToken(),
        )
    )

    assert result.error is not None
    assert result.error.code == "tool_timeout"
    assert "deployment" in result.error.message
    assert "run's remaining" not in result.error.message


def test_without_a_ceiling_a_tool_keeps_its_own_declared_timeout() -> None:
    """The control for the test above: unset is the shipped default."""

    async def handler(invocation: ToolInvocation) -> ToolResult:
        return ToolResult.succeeded(invocation.call, content="done")

    executor = ToolExecutor(monotonic=_ticking())
    binding = ToolBinding(spec=_spec(timeout_seconds=90), handler=handler)

    result = asyncio.run(
        executor.execute(
            binding,
            CALL,
            context=CONTEXT,
            cancellation=NullCancellationToken(),
        )
    )

    assert result.status == "ok"


# --- Saying so while it runs (ADR-068) -------------------------------------
#
# The executor's other job. Everything above asserts that exactly one result
# comes out; these assert that the stretch *before* that result is not silent.


class _RecordingSink:
    """Every payload emitted, in order. Enough of an EventSink for this."""

    def __init__(self, fail: bool = False) -> None:
        self.payloads: list[EventPayload] = []
        self._fail = fail

    async def emit(
        self,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        if self._fail:
            raise RuntimeError("the subscriber buffer is full")
        self.payloads.append(payload)
        return EventEnvelope(
            stream_id="stream_1",
            event_type=payload.kind,
            payload=payload,
        )

    def progress(self) -> list[ToolProgress]:
        return [held for held in self.payloads if isinstance(held, ToolProgress)]


def _execute_watched(
    handler: ToolHandler,
    *,
    sink: _RecordingSink | None = None,
    timeout_seconds: int = 1,
    heartbeat_seconds: float | None = None,
) -> tuple[ToolResult, _RecordingSink]:
    watcher = sink if sink is not None else _RecordingSink()
    executor = ToolExecutor(
        monotonic=_ticking(),
        progress_heartbeat_seconds=heartbeat_seconds,
    )
    binding = ToolBinding(spec=_spec(timeout_seconds), handler=handler)
    result = asyncio.run(
        executor.execute(
            binding,
            CALL,
            context=CONTEXT,
            cancellation=NullCancellationToken(),
            sink=watcher,
        )
    )
    return result, watcher


def test_a_handler_that_reports_is_heard_on_the_call_it_was_given() -> None:
    async def handler(invocation: ToolInvocation) -> ToolResult:
        await invocation.progress("staging 2 input file(s)")
        await invocation.progress("executing in the sandbox")
        return ToolResult.succeeded(invocation.call, content="done")

    result, sink = _execute_watched(handler)

    assert result.status == "ok"
    reported = sink.progress()
    assert [held.message for held in reported] == [
        "staging 2 input file(s)",
        "executing in the sandbox",
    ]
    # The id the handler never supplied, and therefore never got wrong.
    assert {held.tool_call_id for held in reported} == {CALL.tool_call_id}


def test_a_silent_handler_still_says_how_long_it_has_been_running() -> None:
    """The half of this that every existing tool gets for free."""

    async def handler(invocation: ToolInvocation) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult.succeeded(invocation.call, content="done")

    _, sink = _execute_watched(handler, heartbeat_seconds=0.01)

    beats = sink.progress()
    assert beats, "a call lasting five heartbeat intervals reported none"
    # A beat is a clock and nothing else: no invented sentence, and no percent
    # inferred from elapsed-against-timeout, which is not a completion fraction.
    assert all(held.message is None for held in beats)
    assert all(held.percent is None for held in beats)
    assert all(held.elapsed_ms is not None for held in beats)


def test_a_call_shorter_than_one_interval_reports_nothing() -> None:
    """A beat means "this is taking a while", from the first one.

    The overwhelming majority of calls -- a workspace read, a list -- return in
    milliseconds. If those emitted a beat, the signal would be noise and the
    live buffer would carry it for every tool call the system ever makes.
    """

    async def handler(invocation: ToolInvocation) -> ToolResult:
        return ToolResult.succeeded(invocation.call, content="done")

    _, sink = _execute_watched(handler, heartbeat_seconds=30.0)

    assert sink.progress() == []


def test_the_clock_stops_when_the_handler_returns() -> None:
    """A settled step must not keep moving.

    A beat parked on `asyncio.sleep` when the handler returns would fire after
    the result -- reopening on screen a step whose outcome is already drawn.
    """

    async def handler(invocation: ToolInvocation) -> ToolResult:
        await asyncio.sleep(0.02)
        return ToolResult.succeeded(invocation.call, content="done")

    async def scenario() -> list[ToolProgress]:
        sink = _RecordingSink()
        executor = ToolExecutor(monotonic=_ticking(), progress_heartbeat_seconds=0.005)
        binding = ToolBinding(spec=_spec(), handler=handler)
        await executor.execute(
            binding,
            CALL,
            context=CONTEXT,
            cancellation=NullCancellationToken(),
            sink=sink,
        )
        during = len(sink.progress())
        # Long enough for several more beats, had any survived.
        await asyncio.sleep(0.05)
        assert len(sink.progress()) == during
        return sink.progress()

    assert asyncio.run(scenario())


def test_a_report_after_the_handler_returned_is_dropped() -> None:
    """The same guarantee, against a handler that leaked a task."""

    leaked: list[ToolInvocation] = []

    async def handler(invocation: ToolInvocation) -> ToolResult:
        leaked.append(invocation)
        return ToolResult.succeeded(invocation.call, content="done")

    async def scenario() -> list[ToolProgress]:
        sink = _RecordingSink()
        executor = ToolExecutor(monotonic=_ticking())
        binding = ToolBinding(spec=_spec(), handler=handler)
        await executor.execute(
            binding,
            CALL,
            context=CONTEXT,
            cancellation=NullCancellationToken(),
            sink=sink,
        )
        await leaked[0].progress("still going, honest")
        return sink.progress()

    assert asyncio.run(scenario()) == []


def test_a_sink_that_raises_does_not_fail_the_tool() -> None:
    """Progress is an observation of the call, not a participant in it."""

    async def handler(invocation: ToolInvocation) -> ToolResult:
        await invocation.progress("executing in the sandbox")
        return ToolResult.succeeded(invocation.call, content="done")

    result, _ = _execute_watched(handler, sink=_RecordingSink(fail=True))

    assert result.status == "ok"
    assert result.content == "done"


def test_a_handler_reporting_nowhere_is_not_a_special_case() -> None:
    """`execute` without a sink still runs the tool, and the handler still
    reports -- into the discarding reporter, without knowing it."""

    async def handler(invocation: ToolInvocation) -> ToolResult:
        await invocation.progress("executing in the sandbox")
        return ToolResult.succeeded(invocation.call, content="done")

    assert _execute(handler).status == "ok"


def test_a_report_is_normalized_rather_than_refused() -> None:
    """Raising inside a handler is the one thing this must never do.

    A sentence over `ShortText`'s 256 and a percentage outside 0-100 are both
    things a handler can produce by accident. Turning either into a validation
    error would turn an attempt to describe work into the reason that work
    failed.
    """

    async def handler(invocation: ToolInvocation) -> ToolResult:
        await invocation.progress("x" * 400, percent=140)
        await invocation.progress("   ")  # nothing to say
        await invocation.progress("counting", percent=-5)
        return ToolResult.succeeded(invocation.call, content="done")

    result, sink = _execute_watched(handler)

    assert result.status == "ok"
    reported = sink.progress()
    assert [held.percent for held in reported] == [100, 0]
    assert reported[0].message is not None
    assert len(reported[0].message) == 256
    assert reported[0].message.endswith("…")
    assert reported[1].message == "counting"


def test_a_call_cut_short_by_its_timeout_still_stops_reporting() -> None:
    async def handler(invocation: ToolInvocation) -> ToolResult:
        await invocation.progress("executing in the sandbox")
        await asyncio.sleep(5)
        return ToolResult.succeeded(invocation.call, content="never")

    async def scenario() -> tuple[ToolResult, int]:
        sink = _RecordingSink()
        # `timeout_seconds` is an int on the spec, so the shortest bound this
        # call can be given is the deployment ceiling -- the same clamp a real
        # deployment applies, and the one that takes a float.
        executor = ToolExecutor(
            monotonic=_ticking(),
            deployment_ceiling_seconds=0.02,
            progress_heartbeat_seconds=0.005,
        )
        binding = ToolBinding(spec=_spec(), handler=handler)
        result = await executor.execute(
            binding,
            CALL,
            context=CONTEXT,
            cancellation=NullCancellationToken(),
            sink=sink,
        )
        after = len(sink.progress())
        await asyncio.sleep(0.05)
        assert len(sink.progress()) == after
        return result, after

    result, reported = asyncio.run(scenario())

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_timeout"
    # The point: a call that had to be killed still said what it was doing
    # before it was, which is exactly the case a reader needs it for.
    assert reported >= 1


def test_a_non_positive_heartbeat_is_refused_at_construction() -> None:
    try:
        ToolExecutor(progress_heartbeat_seconds=0)
    except ValueError as error:
        assert "positive" in str(error)
    else:
        raise AssertionError("a zero-second heartbeat was accepted")
