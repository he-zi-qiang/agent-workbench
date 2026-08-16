"""The single Worker, over the real Registry, the real saver and the real graph.

Everything below this file has already been tested in isolation. What only a
whole stack can show is that the pieces line up: that the Registry's status,
the checkpoint's position and the reconciliation's answer describe the same
Task, and that a Worker which dies mid-graph leaves behind something a *new*
Worker can finish.

Every Worker here is built fresh from a fresh engine. That is not ceremony --
it is the only way to distinguish "recovered from durable state" from "the same
objects were still in memory".

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
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep
from agent_workbench.ports.task_registry import TaskRun, TaskSubmission
from agent_workbench.workers.task import TaskWorker

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = (
    "task_runs, workflow_checkpoints, workflow_checkpoint_blobs, "
    "workflow_checkpoint_writes"
)

VERSIONS = ("v1", "v2")
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
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        finally:
            await engine.dispose()
        return await scenario()

    return asyncio.run(execute())


def _submission(**overrides: Any) -> TaskSubmission:
    base: dict[str, Any] = {
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
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
    base.update(overrides)
    return TaskSubmission.model_validate(base)


async def _load_state(task: TaskRun) -> TaskState:
    """Stands in for the submission store, which arrives with WP07-02."""

    return TaskState.model_validate(
        {
            "task_id": task.task_id,
            "objective": "Compare retrieval strategies.",
            "plan": (
                TaskStep(
                    step_id="step_1", sequence=1, objective="Gather internal notes."
                ),
            ),
        }
    )


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


def _worker(engine: Any, handlers: dict[str, Any] | None = None) -> TaskWorker:
    """A whole Worker, wired to one engine. Discarding it is a process ending."""

    return TaskWorker(
        registry=PostgresTaskRegistry(engine),
        workflow=LangGraphTaskWorkflow(
            handlers=handlers if handlers is not None else _handlers(),
            checkpointer=PostgresCheckpointSaver(engine),
            graphs=GRAPHS,
        ),
        load_state=_load_state,
        buildable_versions=VERSIONS,
    )


# --------------------------------------------------------------------------


def test_a_worker_takes_a_submitted_task_all_the_way_to_succeeded() -> None:
    """One pass: claim, decide "start", run the graph, decide again, settle."""

    async def scenario() -> tuple[Any, list[str], int]:
        engine = _engine()
        try:
            await PostgresTaskRegistry(engine).submit(_submission())
            outcome = await _worker(engine).run_once()
        finally:
            await engine.dispose()
        assert outcome is not None
        return (
            outcome.final_status,
            [decision.action for decision in outcome.decisions],
            len(outcome.decisions),
        )

    status, actions, count = _run(scenario)

    assert status == "succeeded"
    # Two decisions, not one: running is expressed as deciding again.
    assert actions == ["start", "settle_succeeded"]
    assert count == 2


def test_an_exhausted_critic_rejection_settles_succeeded_with_the_caveat() -> None:
    """ADR-060, end to end against real PostgreSQL and a real checkpoint.

    The graph reaches ``END`` after its quality gate runs out of revisions
    with the critic still asking for changes. That used to settle ``failed``;
    now the durable checkpoint carries the unanswered review, and a fresh
    reconciliation must deliver it to ``mark_succeeded(detail=...)`` -- the
    row is where the console reads what the work shipped with.
    """

    async def exhausted_state(task: TaskRun) -> TaskState:
        state = await _load_state(task)
        return TaskState.model_validate({**state.model_dump(), "max_revisions": 0})

    async def scenario() -> tuple[Any, Any, list[str]]:
        engine = _engine()
        try:
            await PostgresTaskRegistry(engine).submit(_submission())
            handlers = _handlers()

            async def revising_critic(state: TaskState) -> dict[str, Any]:
                return {
                    "review_result": ReviewResult(
                        decision="revise",
                        reviewed_draft_ref="draft_1",
                        revision_number=state.revision_count,
                        summary="The evidence remains insufficient.",
                        issues=("Add evidence.",),
                        score=30,
                    ).model_dump()
                }

            handlers["critic"] = revising_critic
            worker = TaskWorker(
                registry=PostgresTaskRegistry(engine),
                workflow=LangGraphTaskWorkflow(
                    handlers=handlers,
                    checkpointer=PostgresCheckpointSaver(engine),
                    graphs=GRAPHS,
                ),
                load_state=exhausted_state,
                buildable_versions=VERSIONS,
            )
            outcome = await worker.run_once()
        finally:
            await engine.dispose()
        assert outcome is not None
        return (
            outcome.task.status,
            outcome.task.status_detail,
            [decision.action for decision in outcome.decisions],
        )

    status, detail, actions = _run(scenario)

    assert status == "succeeded"
    # The caveat names the standing dispute, down to the reviewer's own issue.
    assert detail is not None and "unresolved" in detail
    assert "Add evidence." in detail
    assert actions == ["start", "settle_succeeded"]


def test_a_worker_with_nothing_queued_does_nothing() -> None:
    async def scenario() -> Any:
        engine = _engine()
        try:
            return await _worker(engine).run_once()
        finally:
            await engine.dispose()

    assert _run(scenario) is None


def test_a_second_worker_finishes_what_a_dead_one_started() -> None:
    """The end of the chain this whole work package has been building.

    The first Worker dies inside ``critic``. Its engine, saver, workflow,
    registry and handlers are all discarded. A second Worker, built from
    nothing, claims the requeued Task and finishes it -- and does not re-run
    the nodes the dead one already completed.
    """

    async def scenario() -> tuple[dict[str, int], dict[str, int], Any, list[str]]:
        first: dict[str, int] = {}
        engine = _engine()
        try:
            registry = PostgresTaskRegistry(engine)
            task = await registry.submit(_submission())
            handlers = _handlers(first)

            async def failing_critic(state: TaskState) -> dict[str, Any]:
                first["critic"] = first.get("critic", 0) + 1
                raise RuntimeError("the model call died mid-run")

            handlers["critic"] = failing_critic
            outcome = await _worker(engine, handlers).run_once()
            assert outcome is not None
            assert outcome.final_status == "failed"
            # Requeued by hand: the reaper that does this on a lease expiry is
            # WP08, and this test is about what happens after, not who does it.
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE task_runs SET status = 'queued', "
                        "status_detail = NULL WHERE task_id = :task_id"
                    ),
                    {"task_id": task.task_id},
                )
        finally:
            await engine.dispose()

        second: dict[str, int] = {}
        engine = _engine()
        try:
            outcome = await _worker(engine, _handlers(second)).run_once()
        finally:
            await engine.dispose()
        assert outcome is not None
        return (
            first,
            second,
            outcome.final_status,
            [decision.action for decision in outcome.decisions],
        )

    first, second, status, actions = _run(scenario)

    assert first["understand"] == 1
    assert first["critic"] == 1
    # The second Worker resumed rather than started: the checkpoint was there.
    assert actions == ["resume", "settle_succeeded"]
    assert status == "succeeded"
    # And it re-ran only the step that died.
    assert second.get("understand", 0) == 0
    assert second.get("research_internal", 0) == 0
    assert second["critic"] == 1


def test_a_graph_that_raises_leaves_the_task_failed_with_a_reason() -> None:
    async def scenario() -> tuple[Any, Any]:
        engine = _engine()
        try:
            await PostgresTaskRegistry(engine).submit(_submission())
            handlers = _handlers()

            async def failing_understand(state: TaskState) -> dict[str, Any]:
                raise RuntimeError("the model call died mid-run")

            handlers["understand"] = failing_understand
            outcome = await _worker(engine, handlers).run_once()
        finally:
            await engine.dispose()
        assert outcome is not None
        return outcome.task.status, outcome.task.status_detail

    status, detail = _run(scenario)

    assert status == "failed"
    assert detail is not None
    # The exception type, not its message: a provider's text carries request
    # bodies and prompt fragments, and this string reaches events and callers.
    assert "RuntimeError" in detail
    assert "died mid-run" not in detail


def test_a_task_registered_for_a_graph_this_worker_cannot_build_is_parked() -> None:
    """``waiting_migration``, which is a WP06 exit condition in its own right."""

    async def scenario() -> tuple[Any, Any, list[str]]:
        engine = _engine()
        try:
            await PostgresTaskRegistry(engine).submit(_submission(graph_version="v9"))
            outcome = await _worker(engine).run_once()
        finally:
            await engine.dispose()
        assert outcome is not None
        return (
            outcome.task.status,
            outcome.task.status_detail,
            [decision.action for decision in outcome.decisions],
        )

    status, detail, actions = _run(scenario)

    assert status == "waiting_migration"
    assert actions == ["wait_for_migration"]
    # Parked with the reason a human needs, not just a status.
    assert detail is not None and "v9" in detail


def test_a_checkpoint_written_by_another_graph_parks_the_task() -> None:
    """The two facts disagree: the Registry says v2, the checkpoint says v1."""

    async def scenario() -> tuple[Any, Any]:
        engine = _engine()
        try:
            registry = PostgresTaskRegistry(engine)
            task = await registry.submit(_submission())
            # A first Worker runs it under v1, which is what the checkpoint
            # then records.
            await _worker(engine).run_once()
            # Somebody re-registers the same thread under v2 and requeues it.
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE task_runs SET status = 'queued', "
                        "status_detail = NULL, graph_version = 'v2' "
                        "WHERE task_id = :task_id"
                    ),
                    {"task_id": task.task_id},
                )
            outcome = await _worker(engine).run_once()
        finally:
            await engine.dispose()
        assert outcome is not None
        return outcome.task.status, outcome.task.status_detail

    status, detail = _run(scenario)

    assert status == "waiting_migration"
    assert detail is not None
    assert "v1" in detail and "v2" in detail


def test_a_checkpoint_this_worker_cannot_build_is_parked_by_what_wrote_it() -> None:
    """A Worker deployed without the graph that wrote the thread.

    Distinct from the two cases above: the Registry's version is one this
    Worker builds fine, so nothing is refused before the checkpoint is read.
    The checkpoint's own version is the unbuildable one, and pending nodes
    cannot be computed for it -- so the position reports the version it found
    and nothing else, and the mismatch parks the Task on that alone.

    Reporting the current graph's version here instead would make an unreadable
    checkpoint look finished, and settle the Task as succeeded.
    """

    async def scenario() -> tuple[Any, Any]:
        engine = _engine()
        try:
            registry = PostgresTaskRegistry(engine)
            task = await registry.submit(_submission(graph_version="v9"))
            # A Worker that does have v9 runs it, so the checkpoint records v9.
            capable = TaskWorker(
                registry=registry,
                workflow=LangGraphTaskWorkflow(
                    handlers=_handlers(),
                    checkpointer=PostgresCheckpointSaver(engine),
                    graphs={**GRAPHS, "v9": GRAPH_DEFINITIONS["v1"]},
                ),
                load_state=_load_state,
                buildable_versions=(*VERSIONS, "v9"),
            )
            await capable.run_once()
            # Requeued, and re-registered under a version this Worker does
            # build -- so only the checkpoint is out of reach.
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE task_runs SET status = 'queued', "
                        "status_detail = NULL, graph_version = 'v1' "
                        "WHERE task_id = :t"
                    ),
                    {"t": task.task_id},
                )
            outcome = await _worker(engine).run_once()
        finally:
            await engine.dispose()
        assert outcome is not None
        return outcome.task.status, outcome.task.status_detail

    status, detail = _run(scenario)

    assert status == "waiting_migration"
    assert detail is not None
    # Parked by what actually wrote it, not by what this process happens to run.
    assert "v9" in detail and "v1" in detail


def test_a_cancelled_task_is_never_handed_to_a_worker_at_all() -> None:
    """The cheapest of the two defences: `cancelled` is not a claimable status.

    The other one -- a cancellation that lands *after* the claim -- is the test
    below. Both exist because they fail differently: this one never starts a
    graph, and that one has to notice mid-flight.
    """

    async def scenario() -> tuple[dict[str, int], Any, list[str]]:
        calls: dict[str, int] = {}
        engine = _engine()
        try:
            registry = PostgresTaskRegistry(engine)
            task = await registry.submit(_submission())
            worker = _worker(engine, _handlers(calls))

            # Claim it, then cancel it, then let the Worker decide. This is the
            # ordering a cancel racing a claim produces.
            await registry.claim_next("worker_setup", lease_seconds=60)
            await registry.cancel(task.task_id, reason="the owner asked")
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE task_runs SET status = 'queued', "
                        "status_detail = NULL WHERE task_id = :t"
                    ),
                    {"t": task.task_id},
                )
            # Re-queued so the Worker can claim it, then cancelled again while
            # it holds the claim -- which is what run_once has to notice.
            claimed = await registry.claim_next("worker_setup", lease_seconds=60)
            assert claimed is not None
            await registry.cancel(task.task_id, reason="the owner asked")

            outcome = await worker.run_once()
        finally:
            await engine.dispose()
        return (
            calls,
            outcome.task.status if outcome else None,
            [d.action for d in outcome.decisions] if outcome else [],
        )

    calls, status, actions = _run(scenario)

    # Nothing queued for the Worker to claim, so it did nothing at all -- and
    # in particular it did not run a cancelled Task's graph.
    assert calls == {}
    assert status is None
    assert actions == []


def test_a_worker_that_claims_a_cancelled_task_propagates_it() -> None:
    """The same branch, reached the other way: cancelled while running.

    Here the Task *is* claimable, and the cancellation lands after the claim.
    The Worker re-reads, sees a terminal fact and leaves it alone.
    """

    async def scenario() -> tuple[dict[str, int], Any, list[str]]:
        calls: dict[str, int] = {}
        engine = _engine()
        try:
            registry = PostgresTaskRegistry(engine)
            task = await registry.submit(_submission())

            class CancellingRegistry(PostgresTaskRegistry):
                """Cancel between the claim and the Worker's re-read."""

                async def claim_next(
                    self, worker_id: str, *, lease_seconds: int
                ) -> Any:
                    claimed = await super().claim_next(
                        worker_id, lease_seconds=lease_seconds
                    )
                    if claimed is not None:
                        await super().cancel(
                            claimed.task.task_id, reason="the owner asked"
                        )
                    return claimed

            worker = TaskWorker(
                registry=CancellingRegistry(engine),
                workflow=LangGraphTaskWorkflow(
                    handlers=_handlers(calls),
                    checkpointer=PostgresCheckpointSaver(engine),
                    graphs=GRAPHS,
                ),
                load_state=_load_state,
                buildable_versions=VERSIONS,
            )
            outcome = await worker.run_once()
            stored = await registry.get(task.task_id)
        finally:
            await engine.dispose()
        assert outcome is not None and stored is not None
        return calls, stored.status, [d.action for d in outcome.decisions]

    calls, status, actions = _run(scenario)

    assert actions == ["propagate_terminal"]
    assert status == "cancelled"
    # The graph never ran.
    assert calls == {}


def test_a_long_graph_execution_is_kept_alive_by_an_independent_heartbeat() -> None:
    """The graph may run longer than a heartbeat interval without losing lease.

    This uses the real Registry and graph adapter.  Counting ``heartbeat`` at
    the Registry boundary proves the concurrent loop ran while a node awaited,
    rather than merely observing a final successful lifecycle transition.
    """

    async def scenario() -> tuple[str, int]:
        engine = _engine()
        try:

            class RecordingRegistry(PostgresTaskRegistry):
                heartbeat_count = 0

                async def heartbeat(self, *args: Any, **kwargs: Any) -> TaskRun:
                    self.heartbeat_count += 1
                    return await super().heartbeat(*args, **kwargs)

            registry = RecordingRegistry(engine)
            await registry.submit(_submission())
            handlers = _handlers()
            normal_understand = handlers["understand"]

            async def slow_understand(state: TaskState) -> dict[str, Any]:
                await asyncio.sleep(1.1)
                return await normal_understand(state)

            handlers["understand"] = slow_understand
            worker = TaskWorker(
                registry=registry,
                workflow=LangGraphTaskWorkflow(
                    handlers=handlers,
                    checkpointer=PostgresCheckpointSaver(engine),
                    graphs=GRAPHS,
                ),
                load_state=_load_state,
                buildable_versions=VERSIONS,
                lease_seconds=4,
                heartbeat_seconds=1,
            )
            outcome = await worker.run_once()
        finally:
            await engine.dispose()
        assert outcome is not None
        return outcome.final_status, registry.heartbeat_count

    status, heartbeat_count = _run(scenario)

    assert status == "succeeded"
    assert heartbeat_count >= 1


def test_a_worker_gives_up_rather_than_deciding_forever() -> None:
    """A graph that never settles is recorded, not looped on.

    Modelled with a workflow whose runs leave the position unchanged, which is
    what a graph that neither finishes nor waits would look like.
    """

    async def scenario() -> tuple[Any, Any, int]:
        engine = _engine()
        try:
            registry = PostgresTaskRegistry(engine)
            await registry.submit(_submission())

            class StuckWorkflow(LangGraphTaskWorkflow):
                async def run(self, state: Any, **kwargs: Any) -> Any:
                    return None

                async def resume(self, **kwargs: Any) -> Any:
                    return None

                async def inspect(self, thread_id: str) -> Any:
                    from agent_workbench.ports.task_workflow import CheckpointPosition

                    return CheckpointPosition(
                        graph_version="v1", pending_nodes=("critic",)
                    )

            worker = TaskWorker(
                registry=registry,
                workflow=StuckWorkflow(
                    handlers=_handlers(),
                    checkpointer=PostgresCheckpointSaver(engine),
                    graphs=GRAPHS,
                ),
                load_state=_load_state,
                buildable_versions=VERSIONS,
            )
            outcome = await worker.run_once()
        finally:
            await engine.dispose()
        assert outcome is not None
        return outcome.task.status, outcome.task.status_detail, len(outcome.decisions)

    status, detail, decisions = _run(scenario)

    assert status == "failed"
    assert detail is not None and "did not settle" in detail
    # It stopped at the budget rather than at some other accident.
    assert decisions == 3
