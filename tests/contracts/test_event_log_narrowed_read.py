"""Reading one run out of a stream, against both implementations.

Several runs per stream is not new -- a Chat session is the stream and each turn
is a run -- but nothing ever asked for one of them until delegation put a child's
events into its parent's stream (ADR-082). "Show me only what this sub-agent did"
is now a question a person clicks on, and it has to mean the same thing in both
stores or the feature works in tests and not in production.

The property that makes this belong in the store rather than in a caller is the
last section: **the narrowing happens before the page is cut**. Filtering a page
the caller already pulled answers a different question -- "the events of this run
that happened to fall in the first 500 of the stream" -- and a delegated run near
the end of a long Task is then invisible rather than empty.
"""

from __future__ import annotations

from typing import Any

from harness import StoreHarness

from agent_workbench.domain.events import RunCompleted, RunStarted, ToolStarted
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventScope

STREAM = "str_narrowed_a"
PARENT = EventScope(stream_id=STREAM, run_id="run_parent")
CHILD = EventScope(stream_id=STREAM, run_id="run_child")
SIBLING = EventScope(stream_id=STREAM, run_id="run_sibling")
BUDGET = RunBudget(max_steps=4, max_tool_calls=4)


def _started() -> RunStarted:
    return RunStarted(run_kind="task", model_profile="main", budget=BUDGET)


def test_a_read_narrowed_to_one_run_returns_that_runs_events_and_no_others(
    event_logs: StoreHarness,
) -> None:
    async def scenario(log: Any) -> list[str]:
        await log.append(PARENT, _started())
        await log.append(CHILD, _started())
        await log.append(CHILD, ToolStarted(tool_call_id="t1", tool_name="x"))
        await log.append(PARENT, RunCompleted(stop_reason="completed"))
        await log.append(CHILD, RunCompleted(stop_reason="completed"))
        page = await log.read(STREAM, run_id="run_child")
        return [envelope.event_type for envelope in page]

    assert event_logs.run(scenario) == ["RunStarted", "ToolStarted", "RunCompleted"]


def test_an_unnarrowed_read_still_returns_the_whole_stream(
    event_logs: StoreHarness,
) -> None:
    """The control. Without it the test above would pass on an implementation
    that had quietly started filtering every read."""

    async def scenario(log: Any) -> int:
        await log.append(PARENT, _started())
        await log.append(CHILD, _started())
        await log.append(SIBLING, _started())
        return len(await log.read(STREAM))

    assert event_logs.run(scenario) == 3


def test_a_run_that_wrote_nothing_narrows_to_an_empty_page(
    event_logs: StoreHarness,
) -> None:
    """Empty, not an error. A parent announces a child before the child writes
    anything, so this is a real state a reader passes through."""

    async def scenario(log: Any) -> int:
        await log.append(PARENT, _started())
        return len(await log.read(STREAM, run_id="run_child"))

    assert event_logs.run(scenario) == 0


def test_a_narrowed_read_keeps_the_streams_own_positions(
    event_logs: StoreHarness,
) -> None:
    """The cursor stays a position in the *stream*, not an index into the
    filtered result. That is what lets a client hold one cursor and change its
    mind about the filter."""

    async def scenario(log: Any) -> list[int]:
        await log.append(PARENT, _started())
        await log.append(CHILD, _started())
        await log.append(PARENT, ToolStarted(tool_call_id="t1", tool_name="x"))
        await log.append(CHILD, RunCompleted(stop_reason="completed"))
        page = await log.read(STREAM, run_id="run_child")
        return [envelope.sequence for envelope in page]

    assert event_logs.run(scenario) == [2, 4]


def test_a_narrowed_read_resumes_after_a_stream_cursor(
    event_logs: StoreHarness,
) -> None:
    async def scenario(log: Any) -> list[int]:
        await log.append(PARENT, _started())
        await log.append(CHILD, _started())
        await log.append(PARENT, ToolStarted(tool_call_id="t1", tool_name="x"))
        await log.append(CHILD, RunCompleted(stop_reason="completed"))
        page = await log.read(STREAM, run_id="run_child", after_sequence=2)
        return [envelope.sequence for envelope in page]

    assert event_logs.run(scenario) == [4]


def test_the_narrowing_happens_before_the_page_is_cut(
    event_logs: StoreHarness,
) -> None:
    """The reason this is a store capability and not a caller's list
    comprehension.

    Twelve events, of which the child wrote the last two, read with a page size
    of three. Narrowing afterwards returns nothing at all -- the first three of
    the stream are all the parent's -- and a sub-agent that ran late in a long
    Task would be invisible rather than empty.
    """

    async def scenario(log: Any) -> list[str]:
        for _ in range(10):
            await log.append(PARENT, ToolStarted(tool_call_id="t1", tool_name="x"))
        await log.append(CHILD, _started())
        await log.append(CHILD, RunCompleted(stop_reason="completed"))
        page = await log.read(STREAM, run_id="run_child", limit=3)
        return [envelope.event_type for envelope in page]

    assert event_logs.run(scenario) == ["RunStarted", "RunCompleted"]
