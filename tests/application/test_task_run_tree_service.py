"""Reading a Task's runs as a tree, and reading one of them on its own.

Two methods, split because they cost very different amounts. ``run_tree`` walks
the stream once and returns something small; ``timeline(run_id=...)`` is an index
lookup somebody can ask for repeatedly as they click around. Merging them would
make every timeline page pay for the walk.

No database. The in-memory log is a real implementation of the port -- the same
one the contract suite pins against PostgreSQL -- so what is under test here is
what this layer decides, and the store's half is pinned next door in
``tests/contracts/test_event_log_narrowed_read.py``.

The section that matters most is the last one: **authorization comes first, and
narrowing is not authorization.** A ``run_id`` selects among events the caller
was already entitled to; a caller who may not read the Task gets the same 404
they got before this parameter existed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.application.tasks import TaskService
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.events import (
    AgentCompleted,
    AgentDelegated,
    RunCompleted,
    RunStarted,
    ToolStarted,
)
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.task_registry import TaskRun

OWNER = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")
STRANGER = PrincipalContext(principal_id="user_2", tenant_id="tenant_b")

THREAD = "thr_1"
PARENT = "run_parent"
CHILD = "run_child"
BUDGET = RunBudget(max_steps=4, max_tool_calls=4)


def _task() -> TaskRun:
    now = datetime(2026, 8, 26, tzinfo=UTC)
    return TaskRun(
        task_id="task_1",
        tenant_id="tenant_a",
        owner_id="user_1",
        thread_id=THREAD,
        graph_version="v1",
        input_ref="input_1",
        input_fingerprint="a" * 64,
        submission_dedup_key="dedup_1",
        run_semantics_snapshot={"model": {"provider": "deepseek"}},
        run_semantics_revision="1.2:v1.3:abc0123456789def",
        submitted_policy_revision="policy-1",
        submitted_policy_fingerprint="f" * 16,
        submitted_authorization_envelope=AuthorizationEnvelope(),
        status="running",
        created_at=now,
        updated_at=now,
        available_at=now,
    )


class _Registry:
    def __init__(self, task: TaskRun) -> None:
        self._task = task

    async def get(self, task_id: str) -> TaskRun | None:
        return self._task if task_id == self._task.task_id else None


async def _seed(log: InMemoryEventLog) -> None:
    """A parent that delegated one child, with tool work on both sides."""

    parent = EventScope(stream_id=THREAD, run_id=PARENT)
    child = EventScope(stream_id=THREAD, run_id=CHILD)
    started = RunStarted(run_kind="task", model_profile="main", budget=BUDGET)

    await log.append(parent, started)
    await log.append(parent, ToolStarted(tool_call_id="t1", tool_name="delegate_agent"))
    await log.append(
        parent, AgentDelegated(child_agent_run_id=CHILD, profile_name="analyst")
    )
    await log.append(child, started)
    await log.append(
        child, ToolStarted(tool_call_id="t2", tool_name="knowledge_search")
    )
    await log.append(child, RunCompleted(stop_reason="completed"))
    await log.append(
        parent,
        AgentCompleted(
            child_agent_run_id=CHILD, status="completed", stop_reason="completed"
        ),
    )
    await log.append(parent, RunCompleted(stop_reason="completed"))


def _service(log: InMemoryEventLog) -> TaskService:
    return TaskService(
        registry=_Registry(_task()),  # type: ignore[arg-type]
        events=log,
        semantics=lambda: None,  # type: ignore[arg-type,return-value]
    )


def _run(scenario: Any) -> Any:
    async def execute() -> Any:
        log = InMemoryEventLog()
        await _seed(log)
        return await scenario(_service(log))

    return asyncio.run(execute())


class TestTheTreeSaysWhatIsInTheStream:
    def test_the_delegated_run_hangs_off_the_run_that_started_it(self) -> None:
        async def scenario(service: TaskService) -> Any:
            return await service.run_tree(OWNER, "task_1")

        tree = _run(scenario)

        assert tree.stream_id == THREAD
        assert [root.run_id for root in tree.roots] == [PARENT]
        assert [child.run_id for child in tree.roots[0].children] == [CHILD]
        assert tree.roots[0].children[0].definition_name == "analyst"

    def test_a_tree_that_saw_the_whole_stream_says_so(self) -> None:
        """``complete`` is a positive claim, so a client can only present a
        truncated tree as a whole one by ignoring a field it was handed."""

        async def scenario(service: TaskService) -> Any:
            return await service.run_tree(OWNER, "task_1")

        assert _run(scenario).complete is True

    def test_a_stranger_is_told_the_task_does_not_exist(self) -> None:
        """The same answer ``get`` gives, and it has to be: a tree read that
        answered differently for another owner would disclose exactly what the
        Task read refuses to."""

        async def scenario(service: TaskService) -> Any:
            with pytest.raises(NotFoundError):
                await service.run_tree(STRANGER, "task_1")
            return True

        assert _run(scenario) is True


class TestOneRunCanBeReadOnItsOwn:
    def test_a_narrowed_timeline_returns_only_that_runs_events(self) -> None:
        async def scenario(service: TaskService) -> Any:
            return await service.timeline(OWNER, "task_1", run_id=CHILD)

        page = _run(scenario)

        assert [event.event_type for event in page.events] == [
            "RunStarted",
            "ToolStarted",
            "RunCompleted",
        ]
        assert {event.run_id for event in page.events} == {CHILD}

    def test_the_unnarrowed_timeline_still_returns_everything(self) -> None:
        """The control. Without it the test above would pass on a service that
        had quietly started filtering every read."""

        async def scenario(service: TaskService) -> Any:
            return await service.timeline(OWNER, "task_1")

        assert len(_run(scenario).events) == 8

    def test_the_cursor_stays_a_position_in_the_stream(self) -> None:
        """Not an index into the filtered result. That is what lets a client
        hold one cursor and change its mind about the filter."""

        async def scenario(service: TaskService) -> Any:
            return await service.timeline(OWNER, "task_1", run_id=CHILD)

        page = _run(scenario)

        assert page.cursor is not None
        # The child's last event is the sixth of the eight in the stream.
        assert page.cursor.sequence == 6

    def test_an_unknown_run_id_is_an_empty_page_not_a_disclosure(self) -> None:
        """Narrowing is not authorization: it selects among events this caller
        may already read. So an id that names nothing answers the same way as
        one that names a run with no events yet -- with nothing."""

        async def scenario(service: TaskService) -> Any:
            return await service.timeline(OWNER, "task_1", run_id="run_nobody")

        assert _run(scenario).events == ()

    def test_a_stranger_narrowing_is_still_a_stranger(self) -> None:
        async def scenario(service: TaskService) -> Any:
            with pytest.raises(NotFoundError):
                await service.timeline(STRANGER, "task_1", run_id=CHILD)
            return True

        assert _run(scenario) is True
