"""A delegated run inherits its parent's publication fence (ADR-089).

The hazard this file exists for does not look like a bug in review. Code's
service takes a ``ProcessOnlySink`` *in its signature* so that handing it an
ordinary sink does not type-check -- and `EventDelegationChannel.sink_for_child`
returns a bare ``ScopedEventSink`` built straight from the log. The parent
could not emit an answer event onto a Code stream; the child could; and the
type written to make that impossible would never have been consulted.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.adapters.delegation import (
    EventDelegationChannel,
    FencedDelegationChannel,
)
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.application.answer_release import ProcessOnlySink
from agent_workbench.domain.events import AnswerCommitted, ModelStarted
from agent_workbench.ports.event_log import EventScope

PARENT = EventScope(stream_id="stream_1", run_id="run_parent")


def _recorded(log: InMemoryEventLog) -> tuple[str, ...]:
    events = asyncio.run(log.read(PARENT.stream_id))
    return tuple(event.event_type for event in events)


def _runs(log: InMemoryEventLog) -> set[str]:
    return {event.run_id for event in asyncio.run(log.read(PARENT.stream_id))}


def _channel() -> tuple[InMemoryEventLog, FencedDelegationChannel]:
    log = InMemoryEventLog()
    return log, FencedDelegationChannel(
        inner=EventDelegationChannel(log=log, parent_scope=PARENT),
        fence=lambda sink: ProcessOnlySink(inner=sink),
    )


def test_a_child_cannot_publish_an_answer_onto_its_parents_stream() -> None:
    log, channel = _channel()
    sink = channel.sink_for_child("run_child")

    # Refused loudly rather than dropped. Silence would leave a run that
    # believed it had published, which is the half of this that would be
    # discovered by a reader of the stream rather than by the run.
    with pytest.raises(RuntimeError, match="no answer to publish"):
        asyncio.run(sink.emit(AnswerCommitted(text="the answer")))

    assert isinstance(sink, ProcessOnlySink)
    assert _recorded(log) == ()


def test_the_fence_forwards_everything_that_is_not_an_answer() -> None:
    """A fence that swallowed ordinary events would make a delegated run
    invisible, which is a different bug with the same shape."""

    log, channel = _channel()

    asyncio.run(
        channel.sink_for_child("run_child").emit(
            ModelStarted(
                model_call_id="call_1",
                model_profile="main",
                model_id="fake",
            )
        )
    )

    assert _recorded(log) == ("ModelStarted",)


def test_the_unfenced_channel_is_the_thing_this_wraps() -> None:
    """Asserted so the wrapper cannot quietly become a no-op: if the inner
    channel ever started fencing on its own, this file would be dead weight and
    should say so by failing."""

    log = InMemoryEventLog()
    bare = EventDelegationChannel(log=log, parent_scope=PARENT)

    assert not isinstance(bare.sink_for_child("run_child"), ProcessOnlySink)


def test_announcements_still_land_on_the_parent_run() -> None:
    log, channel = _channel()

    async def scenario() -> None:
        async with channel.delegating(
            child_agent_run_id="run_child", definition_name="analyst"
        ):
            pass

    asyncio.run(scenario())

    assert _recorded(log) == ("AgentDelegated", "AgentCompleted")
    # On the parent, which has already passed whatever fence it has.
    assert _runs(log) == {"run_parent"}
