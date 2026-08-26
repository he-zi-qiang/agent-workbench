"""What a Task timeline does with a page it could not fully decode.

No database. The corruption itself is the event log's problem and has its own
tests against real PostgreSQL; what is under test here is the arithmetic this
layer performs on the page it is handed -- where the returned cursor points,
and whether a caller can tell a complete slice from a short one. Both are
decisions made in ``TaskService.timeline``, and a fake log is the only way to
present the exact pages that make them visible.

The pages below are real ``ReplayPage`` values rather than a stand-in shape, so
a change to what the isolating read returns fails here rather than being
absorbed by a test-local imitation of it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_workbench.adapters.persistence.event_log import (
    QuarantinedEvent,
    ReplayPage,
)
from agent_workbench.application.tasks import (
    MAX_TIMELINE_LIMIT,
    IsolatingEventLog,
    SubmittedSemantics,
    TaskService,
    TaskTimeline,
)
from agent_workbench.domain.events import EventEnvelope, RunCompleted, RunStarted
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventCursor
from agent_workbench.ports.task_registry import TaskRun
from agent_workbench.workflows.research_graph import GRAPH_VERSION_V1

SEMANTICS = SubmittedSemantics(
    run_semantics_snapshot={"model": {"provider": "deepseek"}},
    run_semantics_revision="1.2:v1.3:abc0123456789def",
    policy_revision="policy-1",
    policy_fingerprint="f" * 16,
    authorization_envelope=AuthorizationEnvelope(),
)

OWNER = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")
STREAM = "thr_1"
BUDGET = RunBudget(max_steps=4, max_tool_calls=4)


def _task() -> TaskRun:
    now = datetime(2026, 8, 11, tzinfo=UTC)
    return TaskRun.model_validate(
        {
            "task_id": "task_1",
            "tenant_id": "tenant_a",
            "owner_id": "user_1",
            "thread_id": STREAM,
            "graph_version": GRAPH_VERSION_V1,
            "input_ref": "input_1",
            "input_fingerprint": "a" * 64,
            "submission_dedup_key": "dedup_1",
            "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
            "run_semantics_revision": "1.2:v1.3:abc0123456789def",
            "submitted_policy_revision": "policy-1",
            "submitted_policy_fingerprint": "f" * 16,
            "submitted_authorization_envelope": {},
            "status": "queued",
            "available_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )


class _FakeRegistry:
    async def get(self, task_id: str) -> TaskRun:
        return _task()


def _envelope(sequence: int) -> EventEnvelope:
    payload = (
        RunStarted(run_kind="task", model_profile="main", budget=BUDGET)
        if sequence == 1
        else RunCompleted(stop_reason="completed")
    )
    return EventEnvelope.for_payload(
        payload,
        stream_id=STREAM,
        run_id="run_1",
        timestamp=datetime(2026, 8, 11, tzinfo=UTC),
        sequence=sequence,
    )


def _quarantined(sequence: int) -> QuarantinedEvent:
    return QuarantinedEvent(
        stream_id=STREAM,
        sequence=sequence,
        event_id=f"evt_{sequence}",
        event_type="ToolStarted",
        schema_version=1,
        reason="payload.run_kind: Field required",
    )


class _IsolatingLog:
    """Hands back one prepared page, and refuses the strict read.

    Refusing ``read`` is what gives these tests teeth: a timeline that still
    took the strict path would raise here instead of quietly producing the
    same answer for the pages that happen to be clean.
    """

    def __init__(self, page: ReplayPage) -> None:
        self._page = page
        self.limits: list[int] = []
        self.after: list[int | None] = []

    async def append(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("the timeline never appends")

    async def read(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a log that can isolate must not be read strictly")

    async def read_isolating(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
    ) -> ReplayPage:
        self.limits.append(limit)
        self.after.append(after_sequence)
        return self._page


class _StrictOnlyLog:
    """A log that never grew the capability: the contract before this change."""

    def __init__(self, recorded: tuple[EventEnvelope, ...]) -> None:
        self._recorded = recorded
        self.limits: list[int] = []

    async def append(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("the timeline never appends")

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
        run_id: str | None = None,
    ) -> tuple[EventEnvelope, ...]:
        self.limits.append(limit)
        return self._recorded


def _service(log: Any) -> TaskService:
    return TaskService(
        registry=_FakeRegistry(), events=log, semantics=lambda: SEMANTICS
    )


def _timeline(log: Any, **kwargs: Any) -> TaskTimeline:
    return asyncio.run(_service(log).timeline(OWNER, "task_1", **kwargs))


# --- the skip is delivered, not swallowed ------------------------------------


def test_a_quarantined_row_does_not_hide_the_events_around_it() -> None:
    """The block this replaces: one damaged row made the whole Task unreadable."""

    log = _IsolatingLog(
        ReplayPage(
            events=(_envelope(1), _envelope(3)),
            quarantined=(_quarantined(2),),
            resume_after=3,
        )
    )

    timeline = _timeline(log)

    assert [event.sequence for event in timeline.events] == [1, 3]


def test_the_caller_is_told_which_positions_it_did_not_receive() -> None:
    """A shorter tuple is also what the end of a stream looks like.

    Without this field a partial history is indistinguishable from a complete
    one, and the only thing worse than a replay that stops is a replay that is
    quietly short.
    """

    log = _IsolatingLog(
        ReplayPage(
            events=(_envelope(1), _envelope(4)),
            quarantined=(_quarantined(2), _quarantined(3)),
            resume_after=4,
        )
    )

    timeline = _timeline(log)

    assert timeline.skipped_sequences == (2, 3)
    assert timeline.skipped == 2


def test_a_clean_page_claims_nothing_was_skipped() -> None:
    """The control group an over-eager implementation fails.

    One that always reported a skip, or that dropped a readable event on the
    way through the isolating path, would satisfy every assertion above.
    """

    page = ReplayPage(
        events=(_envelope(1), _envelope(2), _envelope(3)),
        quarantined=(),
        resume_after=3,
    )
    log = _IsolatingLog(page)

    timeline = _timeline(log)

    assert timeline.events == page.events
    assert timeline.skipped_sequences == ()
    assert timeline.skipped == 0
    assert timeline.cursor == EventCursor(stream_id=STREAM, sequence=3)


# --- the two ways a caller gets the cursor wrong -----------------------------


def test_an_empty_page_leaves_the_callers_cursor_where_it_was() -> None:
    """The trap in ``cursor = page.resume_after``.

    ``None`` on an empty page means "your own cursor is still the truth". Sent
    back to the client as the new cursor it becomes "no cursor", which is how a
    client asks to start at the beginning -- so a caught-up timeline would
    replay the entire Task on every poll.
    """

    resumed = EventCursor(stream_id=STREAM, sequence=5)
    log = _IsolatingLog(ReplayPage(events=(), quarantined=(), resume_after=None))

    timeline = _timeline(log, after=resumed)

    assert timeline.cursor == resumed
    assert log.after == [5]


def test_a_page_of_only_quarantined_rows_still_moves_the_cursor_forward() -> None:
    """The trap in "the cursor is the last event I delivered".

    This page delivered none, and its resume position is already past every row
    it looked at. A cursor taken from the events would sit in front of the
    damage, and the next request would read the same unreadable rows -- the
    client polls forever and never advances.
    """

    log = _IsolatingLog(
        ReplayPage(
            events=(),
            quarantined=(_quarantined(1), _quarantined(2)),
            resume_after=2,
        )
    )

    timeline = _timeline(log)

    assert timeline.events == ()
    assert timeline.cursor == EventCursor(stream_id=STREAM, sequence=2)
    assert timeline.skipped_sequences == (1, 2)


def test_a_trailing_quarantined_row_is_not_read_again_on_the_next_page() -> None:
    """The same trap where it actually bites: the damage is last on the page."""

    log = _IsolatingLog(
        ReplayPage(
            events=(_envelope(1), _envelope(2)),
            quarantined=(_quarantined(3),),
            resume_after=3,
        )
    )

    timeline = _timeline(log)

    assert timeline.cursor == EventCursor(stream_id=STREAM, sequence=3)


# --- what did not change -----------------------------------------------------


def test_the_isolating_read_is_capped_like_the_strict_one() -> None:
    """A second read path that forgot the bound would be the way around it."""

    log = _IsolatingLog(ReplayPage(events=(), quarantined=(), resume_after=None))

    _timeline(log, limit=MAX_TIMELINE_LIMIT * 10)
    _timeline(log, limit=7)

    assert log.limits == [MAX_TIMELINE_LIMIT, 7]


def test_a_log_without_the_capability_keeps_the_strict_read() -> None:
    """Isolation is opt-in, and the timeline did not start requiring it."""

    log = _StrictOnlyLog((_envelope(1), _envelope(2)))
    assert not isinstance(log, IsolatingEventLog)

    timeline = _timeline(log)

    assert [event.sequence for event in timeline.events] == [1, 2]
    assert timeline.cursor == EventCursor(stream_id=STREAM, sequence=2)
    assert timeline.skipped_sequences == ()


def test_an_isolating_log_is_never_asked_for_the_strict_read() -> None:
    """Stated as its own case, because every test above depends on it."""

    log = _IsolatingLog(ReplayPage(events=(), quarantined=(), resume_after=None))

    with pytest.raises(AssertionError, match="must not be read strictly"):
        asyncio.run(log.read(STREAM))
