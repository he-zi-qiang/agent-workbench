"""A run with no answer to publish, and the fence that keeps it that way.

The Chat fence exists because an answer must not be shown early. This one
exists because there is no answer at all: a Code session writes files and ends
with a report, and its steps are the record.

Which makes the interesting question not "what does it blank" but "what does it
let through". Almost everything -- and the report in particular, because
``ModelCompleted.text`` on a Code run is the product rather than a candidate
awaiting release. So the tests below are mostly controls: the fence has to be
narrow enough that a run with no answer still looks like a run.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.application.answer_release import ProcessOnlySink
from agent_workbench.domain.events import (
    AnswerCommitted,
    AnswerWithheld,
    EventPayload,
    ModelCompleted,
    ModelDelta,
    ToolProposed,
    UngroundedAnswerCommitted,
)
from agent_workbench.domain.runs import TokenUsage
from agent_workbench.ports.event_log import EventScope

SCOPE = EventScope(stream_id="ses_code_1", run_id="run_1")

REPORT = "Wrote notes.md and plan.md; the failing test now passes."


def _sink() -> tuple[ProcessOnlySink, InMemoryEventLog]:
    log = InMemoryEventLog()
    return ProcessOnlySink(ScopedEventSink(log=log, scope=SCOPE)), log


def _run(scenario: Any) -> Any:
    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        AnswerCommitted(text="an answer", citations=()),
        UngroundedAnswerCommitted(text="an answer"),
        AnswerWithheld(text="a source stopped being readable"),
    ],
    ids=["committed", "ungrounded", "withheld"],
)
def test_no_answer_event_may_leave_a_run_that_has_no_answer(
    payload: EventPayload,
) -> None:
    """All three, or the missing one is the single event that could get out."""

    sink, log = _sink()

    async def scenario() -> int:
        with pytest.raises(RuntimeError, match="no answer to publish"):
            await sink.emit(payload)
        return len(await log.read(SCOPE.stream_id))

    assert _run(scenario) == 0


def test_the_report_itself_passes_through_untouched() -> None:
    """The control that matters, and the difference from the Chat fence.

    ``AnswerReleaseSink`` blanks ``ModelCompleted.text`` under either policy,
    because there it is a candidate awaiting a release decision. Here it is
    what the session was asked for. A fence copied from the Chat one would
    leave every Code turn ending in an empty report, and every test that only
    checked "no answer event was emitted" would still pass.
    """

    sink, log = _sink()

    async def scenario() -> str:
        await sink.emit(
            ModelCompleted(
                model_call_id="mc_1",
                finish_reason="stop",
                text=REPORT,
                usage=TokenUsage(),
            )
        )
        stored = await log.read(SCOPE.stream_id)
        payload = stored[0].payload
        assert isinstance(payload, ModelCompleted)
        return payload.text

    assert _run(scenario) == REPORT


def test_the_ordinary_step_events_pass_through() -> None:
    """A run whose steps were filtered would be a run nobody could watch."""

    sink, log = _sink()

    async def scenario() -> list[str]:
        await sink.emit(
            ToolProposed(
                tool_call_id="toolu_1",
                tool_name="workspace_write",
                argument_bytes=12,
                argument_sha256="a" * 64,
            )
        )
        await sink.emit(ModelDelta(model_call_id="mc_1", text="thinking"))
        return [envelope.event_type for envelope in await log.read(SCOPE.stream_id)]

    # ModelDelta is transient, so the durable log holds only the proposal --
    # the delta still reached the inner sink, which is what tees it live.
    assert _run(scenario) == ["ToolProposed"]


def test_the_fence_offers_no_way_to_publish_one() -> None:
    """Not "does not call them" -- does not have them.

    ``AnswerReleaseSink`` refuses direct emits *and* provides three deliberate
    publication methods. Inheriting that shape here would leave a Code service
    one attribute away from committing an answer.
    """

    for method in ("commit", "commit_ungrounded", "withhold"):
        assert not hasattr(ProcessOnlySink, method)
