"""The workflow port, across a process boundary.

``tests/workflows`` checks the adapter's logic with the in-memory saver, which
can only ever model two objects in one process. This file is the same contract
with the process actually gone: every object from the first attempt is dropped,
including the engine and its pool, and a second adapter built from nothing but
the ``thread_id`` has to answer the two questions the port asks -- does this
thread exist, and which graph wrote it -- from the database.

Real PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select, text

from agent_workbench.adapters.langgraph import (
    LangGraphTaskWorkflow,
    PostgresCheckpointSaver,
)
from agent_workbench.adapters.langgraph.workflow import (
    GRAPH_DEFINITIONS,
    GRAPH_VERSION_KEY,
    UNRECORDED_GRAPH_VERSION,
)
from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.adapters.persistence.models import workflow_checkpoints
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep
from agent_workbench.ports.task_workflow import (
    WorkflowGraphVersionMismatchError,
    WorkflowThreadAlreadyExistsError,
    WorkflowThreadNotFoundError,
)

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = "workflow_checkpoints, workflow_checkpoint_blobs, workflow_checkpoint_writes"

THREAD = "thr_recovery"

# Two registered versions, so "the checkpoint was written by another graph" is
# distinguishable from "that version is not registered here".
GRAPHS = {"v1": GRAPH_DEFINITIONS["v1"], "v2": GRAPH_DEFINITIONS["v1"]}


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _engine() -> Any:
    return create_query_engine(_dsn(), application_name="agent-workbench-tests")


def _run(scenario: Callable[[], Awaitable[Any]]) -> Any:
    _dsn()

    async def execute() -> Any:
        engine = _engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES}"))
        finally:
            await engine.dispose()
        return await scenario()

    return asyncio.run(execute())


def _state(**overrides: object) -> TaskState:
    base: dict[str, object] = {
        "task_id": "task_1",
        "objective": "Compare retrieval strategies.",
        "plan": (
            TaskStep(step_id="step_1", sequence=1, objective="Gather internal notes."),
        ),
    }
    base.update(overrides)
    return TaskState.model_validate(base)


def _handlers(calls: dict[str, int] | None = None) -> dict[str, Any]:
    tally = calls if calls is not None else {}

    def count(name: str) -> None:
        tally[name] = tally.get(name, 0) + 1

    async def understand(state: TaskState) -> dict[str, Any]:
        count("understand")
        return {"agent_outcome_refs": ("run_understand",)}

    async def internal(state: TaskState) -> dict[str, Any]:
        count("research_internal")
        return {
            "evidence_refs": ("ev_internal",),
            "agent_outcome_refs": ("run_internal",),
        }

    async def external(state: TaskState) -> dict[str, Any]:
        count("research_external")
        return {
            "evidence_refs": ("ev_external",),
            "agent_outcome_refs": ("run_external",),
        }

    async def synthesize(state: TaskState) -> dict[str, Any]:
        count("synthesize")
        return {"draft_ref": "draft_1", "review_result": None}

    async def critic(state: TaskState) -> dict[str, Any]:
        count("critic")
        return {
            "review_result": ReviewResult(
                decision="pass",
                reviewed_draft_ref="draft_1",
                revision_number=state.revision_count,
                summary="Grounded in the evidence.",
                score=90,
            ).model_dump()
        }

    async def approval(state: TaskState) -> dict[str, Any]:
        # Answers its own gate. The interrupting node is the adapter's
        # build_approval_node; what these tests exercise is persistence, and a
        # graph whose approval node returns nothing now fails closed at the
        # router rather than exporting unapproved.
        return {"approval_id": "apr_1", "approval_decision": "approved"}

    return {
        "understand": understand,
        "research_internal": internal,
        "research_external": external,
        "synthesize": synthesize,
        "critic": critic,
        "approval": approval,
    }


def _workflow(engine: Any, handlers: dict[str, Any] | None = None) -> Any:
    return LangGraphTaskWorkflow(
        handlers=handlers if handlers is not None else _handlers(),
        checkpointer=PostgresCheckpointSaver(engine),
        graphs=GRAPHS,
    )


# --------------------------------------------------------------------------


def test_a_restarted_process_resumes_the_thread_it_never_started() -> None:
    """The WP06 acceptance criterion, through the port rather than the saver.

    The first process dies inside ``critic``. The second is given the
    ``thread_id`` and the graph version and nothing else -- no adapter, no
    saver, no engine, no handler closure from the first attempt.
    """

    async def scenario() -> tuple[dict[str, int], dict[str, int], Any]:
        first: dict[str, int] = {}
        engine = _engine()
        try:
            handlers = _handlers(first)

            async def failing_critic(state: TaskState) -> dict[str, Any]:
                first["critic"] = first.get("critic", 0) + 1
                raise RuntimeError("the model call died mid-run")

            handlers["critic"] = failing_critic
            with pytest.raises(RuntimeError, match="died mid-run"):
                await _workflow(engine, handlers).run(
                    _state(), thread_id=THREAD, graph_version="v1"
                )
        finally:
            await engine.dispose()

        second: dict[str, int] = {}
        engine = _engine()
        try:
            result = await _workflow(engine, _handlers(second)).resume(
                thread_id=THREAD, graph_version="v1"
            )
        finally:
            await engine.dispose()
        return first, second, result

    first, second, result = _run(scenario)

    assert first["understand"] == 1
    assert first["critic"] == 1
    # Only the step that died runs again.
    assert second.get("understand", 0) == 0
    assert second.get("research_internal", 0) == 0
    assert second["critic"] == 1
    assert result.disposition == "completed"
    assert result.thread_id == THREAD
    assert result.graph_version == "v1"
    # The state the port hands back is the dead process's work, carried
    # forward through the checkpoint rather than recomputed.
    assert result.state.evidence_refs == ("ev_external", "ev_internal")
    assert result.state.draft_ref == "draft_1"


def test_a_restarted_process_refuses_to_start_a_thread_that_already_ran() -> None:
    """``run`` asks the checkpoint, so another process's thread is not free."""

    async def scenario() -> Any:
        engine = _engine()
        try:
            await _workflow(engine).run(_state(), thread_id=THREAD, graph_version="v1")
        finally:
            await engine.dispose()

        engine = _engine()
        try:
            with pytest.raises(WorkflowThreadAlreadyExistsError) as captured:
                await _workflow(engine).run(
                    _state(), thread_id=THREAD, graph_version="v1"
                )
        finally:
            await engine.dispose()
        return captured.value.thread_id

    assert _run(scenario) == THREAD


def test_a_restarted_process_still_refuses_the_wrong_graph_version() -> None:
    """Version mismatch is the point of recording it, and it must survive.

    The rejected resume also has to leave the checkpoint alone: the correct
    version still finishes the run afterwards.
    """

    async def scenario() -> tuple[str, str, Any, int]:
        engine = _engine()
        try:
            handlers = _handlers()

            async def failing_critic(state: TaskState) -> dict[str, Any]:
                raise RuntimeError("the model call died mid-run")

            handlers["critic"] = failing_critic
            with pytest.raises(RuntimeError, match="died mid-run"):
                await _workflow(engine, handlers).run(
                    _state(), thread_id=THREAD, graph_version="v1"
                )
            async with engine.connect() as connection:
                before = len(
                    (await connection.execute(select(workflow_checkpoints))).all()
                )
        finally:
            await engine.dispose()

        engine = _engine()
        try:
            with pytest.raises(WorkflowGraphVersionMismatchError) as captured:
                await _workflow(engine).resume(thread_id=THREAD, graph_version="v2")
            async with engine.connect() as connection:
                after = len(
                    (await connection.execute(select(workflow_checkpoints))).all()
                )
            assert after == before
            recovered = await _workflow(engine).resume(
                thread_id=THREAD, graph_version="v1"
            )
        finally:
            await engine.dispose()
        return (
            captured.value.checkpoint_graph_version,
            captured.value.requested_graph_version,
            recovered.disposition,
            before,
        )

    checkpoint_version, requested, disposition, checkpoints = _run(scenario)

    assert checkpoint_version == "v1"
    assert requested == "v2"
    assert disposition == "completed"
    assert checkpoints > 1


def test_an_unwritten_thread_is_still_not_found_after_a_restart() -> None:
    async def scenario() -> None:
        engine = _engine()
        try:
            with pytest.raises(WorkflowThreadNotFoundError):
                await _workflow(engine).resume(
                    thread_id="thr_never_written", graph_version="v1"
                )
        finally:
            await engine.dispose()

    _run(scenario)


def test_the_recorded_version_is_a_column_a_query_can_select_on() -> None:
    """What storing metadata as JSONB rather than bytes bought.

    Two threads on two graph versions; asking the database which threads a
    given version wrote is a predicate, not a scan that deserialises every
    checkpoint in the table.
    """

    async def scenario() -> tuple[list[str], list[str]]:
        engine = _engine()
        try:
            await _workflow(engine).run(
                _state(), thread_id="thr_on_v1", graph_version="v1"
            )
            await _workflow(engine).run(
                _state(), thread_id="thr_on_v2", graph_version="v2"
            )
            async with engine.connect() as connection:
                on_v2 = [
                    row.thread_id
                    for row in (
                        await connection.execute(
                            select(workflow_checkpoints.c.thread_id)
                            .where(
                                workflow_checkpoints.c.metadata.contains(
                                    {GRAPH_VERSION_KEY: "v2"}
                                )
                            )
                            .distinct()
                        )
                    ).all()
                ]
                unversioned = [
                    row.thread_id
                    for row in (
                        await connection.execute(
                            select(workflow_checkpoints.c.thread_id)
                            .where(
                                ~workflow_checkpoints.c.metadata.has_key(
                                    GRAPH_VERSION_KEY
                                )
                            )
                            .distinct()
                        )
                    ).all()
                ]
        finally:
            await engine.dispose()
        return on_v2, unversioned

    on_v2, unversioned = _run(scenario)

    assert on_v2 == ["thr_on_v2"]
    # Every checkpoint this adapter writes records its version, so nothing is
    # left for `resume` to report as unrecorded.
    assert unversioned == []
    assert UNRECORDED_GRAPH_VERSION not in on_v2
