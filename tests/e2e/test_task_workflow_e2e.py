"""E2E 2 of 3: two branches in parallel, a critic that sends it back, a report.

The baseline names three fixed demonstrations and this is the second: "研究 Task
→ 两 Researcher 并行 → Critic → 修订". M3a states the same shape as acceptance --
one fan-out, one fan-in, every conditional edge exercised, and completed parallel
nodes not re-run.

``tests/persistence/test_task_worker.py`` covers the Worker against the real
Registry, saver and graph. What it does not do is walk the product path a person
would describe: submit a research objective, watch both branches run, watch the
critic refuse the first draft, watch the second one pass, and end with a report.
This does that in one run, over a real database, and asserts the parts that only
the whole path can show -- that the revision actually re-ran synthesis, that it
did *not* re-run the research that had already finished, and that the fan-in
produced one deterministic order rather than whichever branch returned first.

The nodes are deterministic. A model would make "the critic asked for a revision"
a thing that happens sometimes.

Real PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text

from agent_workbench.adapters.langgraph import (
    LangGraphTaskWorkflow,
    PostgresCheckpointSaver,
)
from agent_workbench.adapters.langgraph.workflow import GRAPH_DEFINITIONS
from agent_workbench.adapters.persistence import (
    PostgresEventLog,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep
from agent_workbench.ports.task_registry import TaskRun, TaskSubmission
from agent_workbench.workers.task import TaskWorker

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = (
    "approvals, task_runs, events, event_streams, workflow_checkpoints, "
    "workflow_checkpoint_blobs, workflow_checkpoint_writes, tool_executions"
)

VERSIONS = ("v1",)
GRAPHS = {"v1": GRAPH_DEFINITIONS["v1"]}

TENANT = "tenant_a"
OWNER = "user_1"

#: Every node call this file has seen, in order, across every Worker. Module
#: level because "the second draft did not re-run research" is a claim about
#: what a *later* run did not do, and a counter owned by one run cannot make it.
CALLS: list[str] = []

#: The state each node was handed, by node name, last call wins. The Worker
#: reports a status rather than a final state -- that lives in the checkpoint --
#: and what these tests want to know is what the graph carried *into* the node
#: that acted on it, which is the same question a reader of the run asks.
SEEN: dict[str, TaskState] = {}


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _engine() -> Any:
    return create_query_engine(_dsn(), application_name="agent-workbench-tests")


def _run(scenario: Callable[[], Awaitable[Any]]) -> Any:
    _dsn()
    CALLS.clear()
    SEEN.clear()

    async def execute() -> Any:
        engine = _engine()
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        finally:
            await engine.dispose()
        return await scenario()

    return asyncio.run(execute())


def _submission() -> TaskSubmission:
    return TaskSubmission.model_validate(
        {
            "tenant_id": TENANT,
            "owner_id": OWNER,
            "thread_id": "thr_1",
            "graph_version": "v1",
            "input_ref": "input_1",
            "input_fingerprint": hashlib.sha256(b"input_1").hexdigest(),
            "submission_dedup_key": "dedup_1",
            "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
            "run_semantics_revision": "1.2:v1.3:abc0123456789def",
            "submitted_policy_revision": "policy-1",
            "submitted_policy_fingerprint": "f" * 16,
            "submitted_authorization_envelope": {},
        }
    )


async def _load_state(task: TaskRun) -> TaskState:
    return TaskState.model_validate(
        {
            "task_id": task.task_id,
            "objective": "Compare hybrid retrieval strategies.",
            "plan": (
                TaskStep(
                    step_id="step_internal",
                    sequence=1,
                    objective="Gather internal notes.",
                ),
                TaskStep(
                    step_id="step_external",
                    sequence=2,
                    objective="Cross-check public sources.",
                ),
            ),
            # One revision allowed, so the critic's refusal has somewhere to go
            # and the second pass is the last one.
            "max_revisions": 1,
        }
    )


def _handlers() -> dict[str, Any]:
    """The fixed research graph, with each node reduced to what it contributes.

    The critic refuses the first draft and passes the second. That is scripted
    rather than modelled: a critic that sometimes revised would make the
    revision edge a thing this demonstration exercises on some runs.
    """

    async def understand(state: TaskState) -> dict[str, Any]:
        CALLS.append("understand")
        SEEN["understand"] = state
        return {}

    async def internal(state: TaskState) -> dict[str, Any]:
        CALLS.append("research_internal")
        SEEN["research_internal"] = state
        return {"evidence_refs": ("art_internal",)}

    async def external(state: TaskState) -> dict[str, Any]:
        CALLS.append("research_external")
        SEEN["research_external"] = state
        return {"evidence_refs": ("art_external",)}

    async def synthesize(state: TaskState) -> dict[str, Any]:
        CALLS.append("synthesize")
        SEEN["synthesize"] = state
        # A new draft each time, so the critic's verdict is bound to the exact
        # revision it reviewed rather than to a draft id that never moves.
        return {
            "draft_ref": f"art_draft_{state.revision_count}",
            "review_result": None,
        }

    async def critic(state: TaskState) -> dict[str, Any]:
        CALLS.append("critic")
        SEEN["critic"] = state
        assert state.draft_ref is not None
        first_pass = state.revision_count == 0
        return {
            "review_result": ReviewResult(
                decision="revise" if first_pass else "pass",
                reviewed_draft_ref=state.draft_ref,
                revision_number=state.revision_count,
                summary=(
                    "The external branch is not represented."
                    if first_pass
                    else "Both branches are represented and attributed."
                ),
                issues=("external evidence is missing",) if first_pass else (),
                score=48 if first_pass else 91,
            ).model_dump()
        }

    async def approval(state: TaskState) -> dict[str, Any]:
        CALLS.append("approval")
        SEEN["approval"] = state
        # This demonstration is about the research loop. The human is the
        # subject of the third one, which runs the interrupting node.
        return {
            "approval_id": "apr_e2e",
            "approval_decision": "approved",
        }

    async def export(state: TaskState) -> dict[str, Any]:
        CALLS.append("export")
        SEEN["export"] = state
        assert state.draft_ref is not None
        return {"export_ref": f"art_report_of_{state.draft_ref}"}

    return {
        "understand": understand,
        "research_internal": internal,
        "research_external": external,
        "synthesize": synthesize,
        "critic": critic,
        "approval": approval,
        "export": export,
    }


def _worker(engine: Any) -> TaskWorker:
    return TaskWorker(
        registry=PostgresTaskRegistry(engine, events=PostgresEventLog(engine)),
        workflow=LangGraphTaskWorkflow(
            handlers=_handlers(),
            checkpointer=PostgresCheckpointSaver(engine),
            graphs=GRAPHS,
        ),
        load_state=_load_state,
        buildable_versions=VERSIONS,
    )


async def _finish() -> tuple[TaskRun, dict[str, TaskState]]:
    engine = _engine()
    try:
        task = await PostgresTaskRegistry(
            engine, events=PostgresEventLog(engine)
        ).submit(_submission())
        outcome = await _worker(engine).run_once()
        assert outcome is not None
        settled = await PostgresTaskRegistry(engine).get(task.task_id)
        assert settled is not None
        return settled, dict(SEEN)

    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# The demonstration
# --------------------------------------------------------------------------


def test_a_research_task_runs_both_branches_revises_once_and_reports() -> None:
    """The second fixed demonstration, in one run."""

    settled, _ = _run(_finish)

    assert settled.status == "succeeded"
    # Both researchers ran, and each ran once: a fan-out that dispatched one
    # branch twice would satisfy a bare "two research calls".
    assert CALLS.count("research_internal") == 1
    assert CALLS.count("research_external") == 1
    # The critic refused once and passed once, so the revise edge was taken.
    assert CALLS.count("critic") == 2
    assert CALLS.count("synthesize") == 2
    assert CALLS.count("export") == 1


def test_the_revision_does_not_re_run_the_research_that_finished() -> None:
    """M3a's acceptance in one line: completed parallel nodes are not re-run.

    A graph that restarted from the top on every revision would still produce
    a report, and would still pass every assertion above except this one.
    """

    _run(_finish)

    assert CALLS.count("understand") == 1
    # The loop is synthesize → critic → quality_gate → synthesize, so a second
    # pass adds those and nothing earlier.
    assert CALLS.index("critic") < CALLS.index("synthesize", CALLS.index("critic"))


def test_the_report_is_of_the_draft_the_critic_passed() -> None:
    """Not of the one it rejected.

    The revision exists precisely because the first draft was inadequate, so a
    report built from it is the failure this edge was added to prevent.
    """

    _, seen = _run(_finish)

    exported = seen["export"]
    assert exported.review_result is not None
    assert exported.review_result.decision == "pass"
    # The second draft, not the one the critic sent back.
    assert exported.draft_ref == "art_draft_1"
    assert exported.review_result.reviewed_draft_ref == "art_draft_1"


def test_the_fan_in_order_does_not_depend_on_which_branch_finished_first() -> None:
    """Two runs, one order.

    The reducer sorts; without that, the evidence list would carry whichever
    branch happened to return first, and every downstream id built from it
    would move between runs that did the same work.
    """

    first = _run(_finish)[1]["synthesize"]
    second = _run(_finish)[1]["synthesize"]

    assert first.evidence_refs == second.evidence_refs
    assert first.evidence_refs == ("art_external", "art_internal")
