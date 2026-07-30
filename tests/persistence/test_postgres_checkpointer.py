"""The PostgreSQL checkpoint saver, against the contract and against a restart.

Two kinds of assertion live here. The differential ones run the same graph twice
-- once on LangGraph's own ``InMemorySaver`` and once on this saver -- and
require the two to agree; a reference implementation is a better oracle than a
list of expectations written by whoever also wrote the code under test.

The other kind is the one WP06 actually asks for: a graph that dies mid-run, and
a *different* saver on a *different* engine that picks the same thread up and
finishes it without redoing the steps that already happened. That is the claim
``InMemorySaver`` cannot make, so it is checked against a real database rather
than argued for.

Real PostgreSQL only, except the sync-refusal test, which needs no database
because refusing is all it does.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from langgraph.checkpoint.memory import (  # pyright: ignore[reportMissingTypeStubs]
    InMemorySaver,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from agent_workbench.adapters.langgraph import (
    PostgresCheckpointSaver,
    StaleCheckpointWriteError,
    build_v1_graph,
)
from agent_workbench.adapters.langgraph.checkpointer import (
    CheckpointCorruptionError,
    CheckpointFenceRequiredError,
    ThreadStillExecutingError,
    _advisory_lock_parts,
)
from agent_workbench.adapters.persistence import (
    PostgresExecutionGuardFactory,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import (
    task_runs,
    workflow_checkpoint_blobs,
    workflow_checkpoint_writes,
    workflow_checkpoints,
)
from agent_workbench.adapters.testing import FailpointController, InjectedCrash
from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep
from agent_workbench.ports.task_registry import StaleExecutionError, TaskSubmission
from agent_workbench.ports.task_workflow import (
    CHECKPOINT_FENCE_EPOCH_KEY,
    CHECKPOINT_FENCE_GUARD_KEY_KEY,
    CHECKPOINT_FENCE_GUARD_PID_KEY,
    CHECKPOINT_FENCE_TASK_ID_KEY,
    CHECKPOINT_FENCE_WORKER_ID_KEY,
    CheckpointFence,
)

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = (
    "task_runs, workflow_checkpoints, workflow_checkpoint_blobs, "
    "workflow_checkpoint_writes"
)

THREAD = "thr_saver"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _engine() -> Any:
    return create_query_engine(_dsn(), application_name="agent-workbench-tests")


def _run(scenario: Callable[[], Awaitable[Any]]) -> Any:
    """Truncate, then run one scenario. Engines are the scenario's business."""

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


def test_bigint_advisory_keys_map_to_pg_locks_unsigned_32_bit_halves() -> None:
    assert _advisory_lock_parts(0) == (0, 0)
    assert _advisory_lock_parts(1) == (0, 1)
    assert _advisory_lock_parts(-1) == (2**32 - 1, 2**32 - 1)
    assert _advisory_lock_parts(-(2**63)) == (2**31, 0)


# --------------------------------------------------------------------------
# The graph under test


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


def _handlers(counter: dict[str, int] | None = None) -> dict[str, Any]:
    """Deterministic stand-ins that count their own calls when asked to."""

    tally = counter if counter is not None else {}

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

    return {
        "understand": understand,
        "research_internal": internal,
        "research_external": external,
        "synthesize": synthesize,
        "critic": critic,
    }


def _config(thread_id: str = THREAD) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _checkpoint(
    checkpoint_id: str = "1f18a895-81b4-67f2-bfff-3cd0225b7b40",
) -> dict[str, Any]:
    return {
        "v": 2,
        "id": checkpoint_id,
        "ts": "2026-07-29T00:00:00+00:00",
        "channel_values": {"objective": "fenced write"},
        "channel_versions": {"objective": "00000000000000000000000000000001.0"},
        "versions_seen": {},
        "updated_channels": None,
    }


def _fenced_config(
    fence: CheckpointFence,
    *,
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    configurable: dict[str, Any] = {
        "thread_id": THREAD,
        CHECKPOINT_FENCE_TASK_ID_KEY: fence.task_id,
        CHECKPOINT_FENCE_WORKER_ID_KEY: fence.worker_id,
        CHECKPOINT_FENCE_EPOCH_KEY: fence.epoch,
    }
    if fence.guard_backend_pid is not None:
        assert fence.guard_lock_key is not None
        configurable.update(
            {
                CHECKPOINT_FENCE_GUARD_PID_KEY: fence.guard_backend_pid,
                CHECKPOINT_FENCE_GUARD_KEY_KEY: fence.guard_lock_key,
            }
        )
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


@asynccontextmanager
async def _claimed_guard_fence(
    engine: Any,
) -> AsyncGenerator[tuple[CheckpointFence, Any], None]:
    registry = PostgresTaskRegistry(engine)
    await registry.submit(
        TaskSubmission(
            tenant_id="tenant_a",
            owner_id="user_1",
            thread_id=THREAD,
            graph_version="v1",
            input_ref="input_1",
            input_fingerprint="a" * 64,
            submission_dedup_key="dedup_fence",
            run_semantics_snapshot={"model": {"provider": "test"}},
            run_semantics_revision="test-v1",
            submitted_policy_revision="policy-v1",
            submitted_policy_fingerprint="f" * 16,
            submitted_authorization_envelope=AuthorizationEnvelope(),
        )
    )
    claim = await registry.claim_next("worker_current", lease_seconds=60)
    assert claim is not None
    factory = PostgresExecutionGuardFactory(_dsn(), healthcheck_seconds=1)
    guard = await factory.acquire(
        task_id=claim.lease.task_id,
        worker_id=claim.lease.worker_id,
        epoch=claim.lease.epoch,
    )
    try:
        yield (
            CheckpointFence(
                task_id=claim.lease.task_id,
                worker_id=claim.lease.worker_id,
                epoch=claim.lease.epoch,
                guard_backend_pid=guard.backend_pid,
                guard_lock_key=guard.lock_key,
            ),
            guard,
        )
    finally:
        await guard.release()
        await factory.dispose()


async def _drive(saver: Any, *, thread_id: str = THREAD, **kwargs: Any) -> Any:
    graph = build_v1_graph(_handlers()).compile(checkpointer=saver)
    return await graph.ainvoke(_state().model_dump(), _config(thread_id), **kwargs)


# --------------------------------------------------------------------------
# Differential: the reference implementation is the oracle


def test_a_run_through_postgres_matches_a_run_through_the_reference_saver() -> None:
    """Same graph, same input, two savers -- the same durable history."""

    async def scenario() -> tuple[Any, Any]:
        memory = InMemorySaver()
        reference_state = await _drive(memory, thread_id="thr_reference")

        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            stored_state = await _drive(saver)

            reference = [
                (tuple_.metadata["step"], tuple_.metadata["source"])
                async for tuple_ in memory.alist(_config("thr_reference"))
            ]
            stored = [
                (tuple_.metadata["step"], tuple_.metadata["source"])
                async for tuple_ in saver.alist(_config())
            ]
        finally:
            await engine.dispose()
        return (reference_state, reference), (stored_state, stored)

    (reference_state, reference), (stored_state, stored) = _run(scenario)

    # The graph reached the same place by the same route.
    assert stored_state == reference_state
    assert stored == reference
    # And that route was more than one step, or the comparison proves little.
    assert len(stored) > 1


def test_the_latest_checkpoint_carries_the_state_the_run_ended_with() -> None:
    """``aget_tuple`` with no checkpoint id means "where this thread got to"."""

    async def scenario() -> tuple[Any, Any, Any, Any]:
        memory = InMemorySaver()
        await _drive(memory, thread_id="thr_reference")
        reference = await memory.aget_tuple(_config("thr_reference"))

        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            final = await _drive(saver)
            latest = await saver.aget_tuple(_config())
        finally:
            await engine.dispose()
        return (
            final,
            latest.checkpoint["channel_values"],
            reference.checkpoint["channel_values"],
            latest.parent_config,
        )

    final, values, reference_values, parent_config = _run(scenario)

    # Byte-level agreement with the reference saver, blob table and all.
    assert values == reference_values
    # LangGraph's msgpack round trip returns every sequence as a list, so a
    # restored channel is not identical to the live one -- it is the same value
    # in the serialiser's shapes. What has to survive is the domain state the
    # graph will be resumed with, and that is what is compared.
    assert isinstance(values["evidence_refs"], list)
    assert isinstance(final["evidence_refs"], tuple)
    fields = set(TaskState.model_fields)
    assert TaskState.model_validate(
        {key: value for key, value in values.items() if key in fields}
    ) == TaskState.model_validate(
        {key: value for key, value in final.items() if key in fields}
    )
    # The latest checkpoint is not the root, so it names its parent.
    assert parent_config is not None
    assert parent_config["configurable"]["checkpoint_id"]


def test_a_named_checkpoint_is_read_back_instead_of_the_latest() -> None:
    async def scenario() -> tuple[str, str, Any]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await _drive(saver)
            history = [tuple_ async for tuple_ in saver.alist(_config())]
            middle = history[len(history) // 2]
            named = await saver.aget_tuple(
                {
                    "configurable": {
                        "thread_id": THREAD,
                        "checkpoint_id": middle.config["configurable"]["checkpoint_id"],
                    }
                }
            )
            latest = await saver.aget_tuple(_config())
        finally:
            await engine.dispose()
        return (
            named.config["configurable"]["checkpoint_id"],
            latest.config["configurable"]["checkpoint_id"],
            named.metadata["step"] < latest.metadata["step"],
        )

    named_id, latest_id, named_is_earlier = _run(scenario)

    assert named_id != latest_id
    assert named_is_earlier


def test_an_unknown_thread_reads_back_as_nothing() -> None:
    async def scenario() -> Any:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            return await saver.aget_tuple(_config("thr_never_written"))
        finally:
            await engine.dispose()

    assert _run(scenario) is None


# --------------------------------------------------------------------------
# The claim WP06 is actually after


def test_a_new_saver_on_a_new_engine_finishes_a_run_the_old_one_abandoned() -> None:
    """A process restart, as far as the checkpoint is concerned.

    The first run dies inside ``critic``. Everything about the first attempt is
    then thrown away -- handlers, compiled graph, saver, engine, connection pool
    -- and a second one is built from nothing but the thread id. It has to
    finish the run, and it has to not redo the work that already happened.
    """

    async def scenario() -> tuple[dict[str, int], dict[str, int], Any]:
        first_calls: dict[str, int] = {}
        engine = _engine()
        try:
            handlers = _handlers(first_calls)

            async def failing_critic(state: TaskState) -> dict[str, Any]:
                first_calls["critic"] = first_calls.get("critic", 0) + 1
                raise RuntimeError("the model call died mid-run")

            handlers["critic"] = failing_critic
            graph = build_v1_graph(handlers).compile(
                checkpointer=PostgresCheckpointSaver(engine)
            )
            with pytest.raises(RuntimeError, match="died mid-run"):
                await graph.ainvoke(_state().model_dump(), _config())
        finally:
            await engine.dispose()

        # Nothing from the first attempt survives into the second.
        second_calls: dict[str, int] = {}
        engine = _engine()
        try:
            resumed = build_v1_graph(_handlers(second_calls)).compile(
                checkpointer=PostgresCheckpointSaver(engine)
            )
            # No input: the state belongs to the checkpoint, and passing it
            # again is how the original objective gets applied twice.
            final = await resumed.ainvoke(None, _config())
        finally:
            await engine.dispose()
        return first_calls, second_calls, final

    first_calls, second_calls, final = _run(scenario)

    # The first attempt got as far as the critic and died there.
    assert first_calls["understand"] == 1
    assert first_calls["critic"] == 1
    # The second process re-ran the step that failed, and nothing before it.
    assert second_calls.get("understand", 0) == 0
    assert second_calls.get("research_internal", 0) == 0
    assert second_calls["critic"] == 1
    # And the run completed, carrying the evidence the dead process gathered.
    assert final["draft_ref"] == "draft_1"
    assert set(final["evidence_refs"]) == {"ev_internal", "ev_external"}
    assert final["review_result"]["decision"] == "pass"


def test_a_sibling_that_finished_before_the_crash_is_not_run_again() -> None:
    """The half of recovery that only pending writes can deliver.

    ``research_internal`` and ``research_external`` run in the same step. If one
    finishes and the other dies, the survivor's result is already recorded
    against the step's checkpoint but the step itself never completed. On
    resume only the failed branch may re-run: replaying the survivor would
    charge its budget twice and call whatever it called a second time.

    Without ``pending_writes`` on the restored tuple this silently re-runs both,
    which is a correctness difference no other test here can see.
    """

    async def scenario() -> tuple[dict[str, int], dict[str, int], Any]:
        first_calls: dict[str, int] = {}
        engine = _engine()
        try:
            handlers = _handlers(first_calls)

            async def failing_internal(state: TaskState) -> dict[str, Any]:
                first_calls["research_internal"] = (
                    first_calls.get("research_internal", 0) + 1
                )
                raise RuntimeError("the internal search died mid-run")

            handlers["research_internal"] = failing_internal
            graph = build_v1_graph(handlers).compile(
                checkpointer=PostgresCheckpointSaver(engine)
            )
            with pytest.raises(RuntimeError, match="died mid-run"):
                await graph.ainvoke(_state().model_dump(), _config())
        finally:
            await engine.dispose()

        second_calls: dict[str, int] = {}
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            resumed = build_v1_graph(_handlers(second_calls)).compile(
                checkpointer=saver
            )
            pending = (await saver.aget_tuple(_config())).pending_writes
            final = await resumed.ainvoke(None, _config())
        finally:
            await engine.dispose()
        return first_calls, second_calls, (final, pending)

    first_calls, second_calls, (final, pending) = _run(scenario)

    # Both branches were attempted, and only one of them came back.
    assert first_calls["research_external"] == 1
    assert first_calls["research_internal"] == 1
    # The survivor's writes were durable before the crash, error included.
    assert {channel for _, channel, _ in pending} >= {"evidence_refs", "__error__"}
    # On resume only the branch that died runs again.
    assert second_calls["research_internal"] == 1
    assert second_calls.get("research_external", 0) == 0
    # And nothing was lost: the survivor's evidence is in the final state even
    # though the process that produced it never got to write it into one.
    assert set(final["evidence_refs"]) == {"ev_internal", "ev_external"}


def test_the_reference_saver_cannot_make_that_claim() -> None:
    """The control for the test above: an in-memory saver loses the thread.

    Without this, "resumed from the checkpoint" and "re-ran the whole graph
    from its input" would look identical in the assertions above.
    """

    async def scenario() -> tuple[dict[str, int], Any]:
        calls: dict[str, int] = {}
        handlers = _handlers(calls)

        async def failing_critic(state: TaskState) -> dict[str, Any]:
            raise RuntimeError("the model call died mid-run")

        handlers["critic"] = failing_critic
        graph = build_v1_graph(handlers).compile(checkpointer=InMemorySaver())
        with pytest.raises(RuntimeError, match="died mid-run"):
            await graph.ainvoke(_state().model_dump(), _config())

        # A restart, modelled the same way: a brand new saver.
        second: dict[str, int] = {}
        resumed = build_v1_graph(_handlers(second)).compile(
            checkpointer=InMemorySaver()
        )
        error: Any = None
        try:
            await resumed.ainvoke(None, _config())
        except Exception as exc:
            error = type(exc).__name__
        return second, error

    second_calls, error = _run(scenario)

    # It either refuses outright or starts over; either way it did not resume.
    assert error is not None or second_calls.get("understand", 0) == 1


# --------------------------------------------------------------------------
# Listing


def test_a_listing_runs_newest_first_and_honours_its_limit() -> None:
    async def scenario() -> tuple[list[int], list[int]]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await _drive(saver)
            everything = [
                tuple_.metadata["step"] async for tuple_ in saver.alist(_config())
            ]
            capped = [
                tuple_.metadata["step"]
                async for tuple_ in saver.alist(_config(), limit=3)
            ]
        finally:
            await engine.dispose()
        return everything, capped

    everything, capped = _run(scenario)

    assert everything == sorted(everything, reverse=True)
    assert len(capped) == 3
    assert capped == everything[:3]


def test_before_lists_only_what_came_earlier() -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await _drive(saver)
            history = [
                tuple_.config["configurable"]["checkpoint_id"]
                async for tuple_ in saver.alist(_config())
            ]
            pivot = history[len(history) // 2]
            earlier = [
                tuple_.config["configurable"]["checkpoint_id"]
                async for tuple_ in saver.alist(
                    _config(),
                    before={
                        "configurable": {"thread_id": THREAD, "checkpoint_id": pivot}
                    },
                )
            ]
        finally:
            await engine.dispose()
        return history, earlier

    history, earlier = _run(scenario)

    pivot_index = len(history) // 2
    assert earlier == history[pivot_index + 1 :]
    assert earlier


def test_a_metadata_filter_selects_by_key() -> None:
    async def scenario() -> tuple[list[str], list[int]]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await _drive(saver)
            sources = [
                tuple_.metadata["source"]
                async for tuple_ in saver.alist(_config(), filter={"source": "loop"})
            ]
            steps = [
                tuple_.metadata["step"]
                async for tuple_ in saver.alist(_config(), filter={"step": 0})
            ]
        finally:
            await engine.dispose()
        return sources, steps

    sources, steps = _run(scenario)

    assert sources and set(sources) == {"loop"}
    assert steps == [0]


def test_one_thread_never_appears_in_another_s_listing() -> None:
    async def scenario() -> tuple[int, int, int]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await _drive(saver, thread_id="thr_a")
            await _drive(saver, thread_id="thr_b")
            a = len([_ async for _ in saver.alist(_config("thr_a"))])
            b = len([_ async for _ in saver.alist(_config("thr_b"))])
            everything = len([_ async for _ in saver.alist(None)])
        finally:
            await engine.dispose()
        return a, b, everything

    a, b, everything = _run(scenario)

    assert a > 1
    assert a == b
    # `alist(None)` is the contract's "every thread", so the two add up.
    assert everything == a + b


# --------------------------------------------------------------------------
# Write semantics


def test_writing_the_same_checkpoint_twice_leaves_one_row() -> None:
    """A retried ``aput`` is the same position, not a second one."""

    async def scenario() -> tuple[int, Any]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            checkpoint = {
                "v": 2,
                "id": "1f18a895-81b4-67f2-bfff-3cd0225b7b38",
                "ts": "2026-07-28T00:00:00+00:00",
                "channel_values": {"objective": "first"},
                "channel_versions": {"objective": "00000000000000000000000000000001.0"},
                "versions_seen": {},
                "updated_channels": None,
            }
            versions = {"objective": "00000000000000000000000000000001.0"}
            await saver.aput(
                _config(), checkpoint, {"source": "loop", "step": 0}, versions
            )
            await saver.aput(
                _config(),
                {**checkpoint, "channel_values": {"objective": "second"}},
                {"source": "update", "step": 0},
                versions,
            )
            restored = await saver.aget_tuple(_config())
            async with engine.connect() as connection:
                rows = len(
                    (await connection.execute(select(workflow_checkpoints))).all()
                )
        finally:
            await engine.dispose()
        return rows, restored

    rows, restored = _run(scenario)

    assert rows == 1
    # The later write wins, in the checkpoint and in its metadata.
    assert restored.checkpoint["channel_values"]["objective"] == "second"
    assert restored.metadata["source"] == "update"


def test_an_ordinary_write_keeps_the_first_value_and_a_special_one_the_last() -> None:
    """The two collision rules, which are not the same rule.

    A retried task must not replace writes that were already durable, or a
    resumed step sees a second result for work already recorded. An error or an
    interrupt is the opposite: the newest one is that task's current state.
    """

    async def scenario() -> tuple[Any, Any]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            config = {
                "configurable": {
                    "thread_id": THREAD,
                    "checkpoint_ns": "",
                    "checkpoint_id": "ckpt_1",
                }
            }
            await saver.aput_writes(config, [("evidence_refs", "first")], "task_a")
            await saver.aput_writes(config, [("evidence_refs", "second")], "task_a")
            await saver.aput_writes(config, [("__error__", "first failure")], "task_a")
            await saver.aput_writes(config, [("__error__", "later failure")], "task_a")

            async with engine.connect() as connection:
                rows = (
                    (
                        await connection.execute(
                            select(
                                workflow_checkpoint_writes.c.idx,
                                workflow_checkpoint_writes.c.channel,
                                workflow_checkpoint_writes.c.payload_type,
                                workflow_checkpoint_writes.c.payload,
                            ).order_by(workflow_checkpoint_writes.c.idx)
                        )
                    )
                    .mappings()
                    .all()
                )
            decoded = [
                (
                    row["idx"],
                    row["channel"],
                    saver.serde.loads_typed(
                        (row["payload_type"], bytes(row["payload"]))
                    ),
                )
                for row in rows
            ]
        finally:
            await engine.dispose()
        return decoded, len(decoded)

    decoded, count = _run(scenario)

    assert count == 2
    assert (-1, "__error__", "later failure") in decoded
    assert (0, "evidence_refs", "first") in decoded


def test_a_channel_written_as_empty_comes_back_absent_not_as_none() -> None:
    """The ``empty`` sentinel is a stored fact, and not a channel value."""

    async def scenario() -> tuple[dict[str, Any], int]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            checkpoint = {
                "v": 2,
                "id": "1f18a895-81b4-67f2-bfff-3cd0225b7b39",
                "ts": "2026-07-28T00:00:00+00:00",
                # `held` has a version but no value: LangGraph does this for
                # every branch channel that has not fired.
                "channel_values": {"objective": "ask"},
                "channel_versions": {
                    "objective": "00000000000000000000000000000001.0",
                    "held": "00000000000000000000000000000001.0",
                },
                "versions_seen": {},
                "updated_channels": None,
            }
            await saver.aput(
                _config(),
                checkpoint,
                {"source": "loop", "step": 0},
                {
                    "objective": "00000000000000000000000000000001.0",
                    "held": "00000000000000000000000000000001.0",
                },
            )
            restored = await saver.aget_tuple(_config())
            async with engine.connect() as connection:
                blobs = len(
                    (await connection.execute(select(workflow_checkpoint_blobs))).all()
                )
        finally:
            await engine.dispose()
        return restored.checkpoint["channel_values"], blobs

    values, blobs = _run(scenario)

    # Both versions were recorded...
    assert blobs == 2
    # ...but only one of them is a value.
    assert values == {"objective": "ask"}
    assert "held" not in values


def test_a_subgraph_namespace_is_not_read_as_the_parent_thread() -> None:
    async def scenario() -> tuple[Any, Any]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            for namespace, objective in (("", "parent"), ("child|abc", "subgraph")):
                await saver.aput(
                    {
                        "configurable": {
                            "thread_id": THREAD,
                            "checkpoint_ns": namespace,
                        }
                    },
                    {
                        "v": 2,
                        "id": "1f18a895-81b4-67f2-bfff-3cd0225b7b40",
                        "ts": "2026-07-28T00:00:00+00:00",
                        "channel_values": {"objective": objective},
                        "channel_versions": {
                            "objective": "00000000000000000000000000000001.0"
                        },
                        "versions_seen": {},
                        "updated_channels": None,
                    },
                    {"source": "loop", "step": 0},
                    {"objective": "00000000000000000000000000000001.0"},
                )
            parent = await saver.aget_tuple(_config())
            child = await saver.aget_tuple(
                {"configurable": {"thread_id": THREAD, "checkpoint_ns": "child|abc"}}
            )
        finally:
            await engine.dispose()
        return (
            parent.checkpoint["channel_values"]["objective"],
            child.checkpoint["channel_values"]["objective"],
        )

    assert _run(scenario) == ("parent", "subgraph")


# --------------------------------------------------------------------------
# E2 fencing and corruption: only a live lease may advance a durable position


def test_fenced_saver_rejects_an_old_epoch_without_writing_checkpoint_or_writes() -> (
    None
):
    async def scenario() -> tuple[int, int]:
        engine = _engine()
        try:
            async with _claimed_guard_fence(engine) as (stale, _):
                # Model a later claim without changing Registry claim behaviour in
                # this saver-focused test: epoch 1 is now an old Worker token.
                current = stale.model_copy(update={"epoch": stale.epoch + 1})
                async with engine.begin() as connection:
                    await connection.execute(
                        task_runs.update()
                        .where(task_runs.c.task_id == stale.task_id)
                        .values(lease_epoch=current.epoch)
                    )
                saver = PostgresCheckpointSaver(engine, require_fence=True)
                with pytest.raises(StaleExecutionError):
                    await saver.aput(
                        _fenced_config(stale),
                        _checkpoint(),
                        {"source": "loop", "step": 0},
                        {"objective": "00000000000000000000000000000001.0"},
                    )
                with pytest.raises(StaleExecutionError):
                    await saver.aput_writes(
                        _fenced_config(stale, checkpoint_id="checkpoint_old"),
                        [("objective", "must not persist")],
                        "task_node",
                    )
            async with engine.connect() as connection:
                checkpoints = len(
                    (await connection.execute(select(workflow_checkpoints))).all()
                )
                writes = len(
                    (await connection.execute(select(workflow_checkpoint_writes))).all()
                )
            return checkpoints, writes
        finally:
            await engine.dispose()

    assert _run(scenario) == (0, 0)


async def _refused_write(engine: Any, fence: CheckpointFence) -> tuple[int, int]:
    """Attempt both fenced writes, require both to be refused, count the rows.

    Both halves matter: `aput` and `aput_writes` fence independently, and a
    guard that only covered one would leave the other writing under a token
    the database has already invalidated.
    """

    saver = PostgresCheckpointSaver(engine, require_fence=True)
    with pytest.raises(StaleExecutionError):
        await saver.aput(
            _fenced_config(fence),
            _checkpoint(),
            {"source": "loop", "step": 0},
            {"objective": "00000000000000000000000000000001.0"},
        )
    with pytest.raises(StaleExecutionError):
        await saver.aput_writes(
            _fenced_config(fence, checkpoint_id="checkpoint_refused"),
            [("objective", "must not persist")],
            "task_node",
        )
    async with engine.connect() as connection:
        checkpoints = len(
            (await connection.execute(select(workflow_checkpoints))).all()
        )
        writes = len(
            (await connection.execute(select(workflow_checkpoint_writes))).all()
        )
    return checkpoints, writes


def test_fenced_saver_refuses_to_write_for_a_task_that_is_no_longer_running() -> None:
    """A settled Task's thread is not a thread to keep writing.

    The row moves to a terminal status and drops its lease together -- the
    lease-lifecycle constraint requires exactly that, so a cancelled Task can
    never still carry an owner or a deadline.

    Which makes the fence's own ``status = 'running'`` condition redundant
    today: the fence already demands a matching owner and an unexpired
    deadline, and the constraint means only a running row can have either.
    Removing that condition alone changes nothing observable, and a sabotage of
    exactly that shape confirmed it. It stays as the direct statement of the
    rule, because the day the constraint is relaxed -- keeping a terminal row's
    lease for audit, say -- it becomes the only thing standing between a
    settled Task and further writes to its thread.
    """

    async def scenario() -> tuple[int, int]:
        engine = _engine()
        try:
            async with _claimed_guard_fence(engine) as (fence, _):
                async with engine.begin() as connection:
                    await connection.execute(
                        task_runs.update()
                        .where(task_runs.c.task_id == fence.task_id)
                        .values(
                            status="cancelled",
                            status_detail="the owner asked",
                            lease_owner=None,
                            lease_until=None,
                            heartbeat_at=None,
                        )
                    )
                return await _refused_write(engine, fence)
        finally:
            await engine.dispose()

    assert _run(scenario) == (0, 0)


def test_fenced_saver_refuses_a_fence_naming_another_worker() -> None:
    """Holding *a* live lease is not holding *this* lease.

    Without the owner condition, any Worker's fence would satisfy any other
    Worker's row, and two processes could interleave writes on one thread while
    both believed they were fenced.
    """

    async def scenario() -> tuple[int, int]:
        engine = _engine()
        try:
            async with _claimed_guard_fence(engine) as (fence, _):
                async with engine.begin() as connection:
                    await connection.execute(
                        task_runs.update()
                        .where(task_runs.c.task_id == fence.task_id)
                        .values(lease_owner="worker_somebody_else")
                    )
                return await _refused_write(engine, fence)
        finally:
            await engine.dispose()

    assert _run(scenario) == (0, 0)


def test_fenced_saver_refuses_a_lease_that_has_already_expired() -> None:
    """An expired lease is the ordinary way a Worker loses its claim.

    Nothing revokes it actively: the reaper notices later, and until then the
    row still carries this Worker and this epoch. The deadline is the only
    thing that says the claim is over.
    """

    async def scenario() -> tuple[int, int]:
        engine = _engine()
        try:
            async with _claimed_guard_fence(engine) as (fence, _):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE task_runs SET lease_until = "
                            "statement_timestamp() - interval '1 second' "
                            "WHERE task_id = :task_id"
                        ),
                        {"task_id": fence.task_id},
                    )
                return await _refused_write(engine, fence)
        finally:
            await engine.dispose()

    assert _run(scenario) == (0, 0)


def test_fenced_saver_refuses_a_fence_for_a_task_on_another_thread() -> None:
    """The fence authorizes one thread, not every thread its Task can name.

    Without the thread condition a fence would be a Worker-wide permit: a
    correct lease for task A would authorize writes to any thread the caller
    passed, including one belonging to another Task entirely.
    """

    async def scenario() -> tuple[int, int]:
        engine = _engine()
        try:
            async with _claimed_guard_fence(engine) as (fence, _):
                # The Task keeps its live lease; only the thread it owns moves.
                async with engine.begin() as connection:
                    await connection.execute(
                        task_runs.update()
                        .where(task_runs.c.task_id == fence.task_id)
                        .values(thread_id="thr_some_other_task")
                    )
                return await _refused_write(engine, fence)
        finally:
            await engine.dispose()

    assert _run(scenario) == (0, 0)


def test_a_required_fence_is_not_satisfied_by_one_without_a_guard() -> None:
    """The advisory guard is part of the fence, not an optional extra.

    A fence carrying a live lease but no guard session cannot be checked
    against a lost guard at all, so accepting it would silently downgrade every
    write to lease-only fencing -- which is exactly what the guard exists to
    backstop, because a lease can look live to a Worker whose connection is
    already gone.
    """

    async def scenario() -> tuple[type[BaseException] | None, int]:
        engine = _engine()
        try:
            async with _claimed_guard_fence(engine) as (fence, _):
                guardless = fence.model_copy(
                    update={"guard_backend_pid": None, "guard_lock_key": None}
                )
                saver = PostgresCheckpointSaver(engine, require_fence=True)
                raised: type[BaseException] | None = None
                try:
                    await saver.aput(
                        _fenced_config(guardless),
                        _checkpoint(),
                        {"source": "loop", "step": 0},
                        {"objective": "00000000000000000000000000000001.0"},
                    )
                except CheckpointFenceRequiredError as error:
                    raised = type(error)
                async with engine.connect() as connection:
                    written = len(
                        (await connection.execute(select(workflow_checkpoints))).all()
                    )
                return raised, written
        finally:
            await engine.dispose()

    raised, written = _run(scenario)

    assert raised is CheckpointFenceRequiredError
    assert written == 0


def test_fenced_saver_requires_a_fence_and_propagates_a_current_one() -> None:
    async def scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int]:
        engine = _engine()
        try:
            async with _claimed_guard_fence(engine) as (fence, _):
                saver = PostgresCheckpointSaver(engine, require_fence=True)
                with pytest.raises(CheckpointFenceRequiredError):
                    await saver.aput(
                        _config(),
                        _checkpoint(),
                        {"source": "loop", "step": 0},
                        {"objective": "00000000000000000000000000000001.0"},
                    )
                first = await saver.aput(
                    _fenced_config(fence),
                    _checkpoint(),
                    {"source": "loop", "step": 0},
                    {"objective": "00000000000000000000000000000001.0"},
                )
                returned = await saver.aput(
                    first,
                    _checkpoint("1f18a895-81b4-67f2-bfff-3cd0225b7b41"),
                    {"source": "loop", "step": 1},
                    {"objective": "00000000000000000000000000000002.0"},
                )
                # LangGraph passes the saver-returned config to subsequent write
                # calls. These fields must survive that round trip or a pending
                # write would unexpectedly lose the lease that authorized it.
                await saver.aput_writes(
                    returned,
                    [("objective", "present")],
                    "task_node",
                )
                restored = await saver.aget_tuple(_config())
                async with engine.connect() as connection:
                    writes = len(
                        (
                            await connection.execute(select(workflow_checkpoint_writes))
                        ).all()
                    )
                assert restored is not None
                assert restored.parent_config is not None
                return (
                    returned["configurable"],
                    restored.config["configurable"],
                    restored.parent_config["configurable"],
                    writes,
                )
        finally:
            await engine.dispose()

    returned, restored, parent, writes = _run(scenario)
    for configurable in (returned, restored, parent):
        assert configurable[CHECKPOINT_FENCE_TASK_ID_KEY].startswith("task_")
        assert configurable[CHECKPOINT_FENCE_WORKER_ID_KEY] == "worker_current"
        assert configurable[CHECKPOINT_FENCE_EPOCH_KEY] == 1
        assert configurable[CHECKPOINT_FENCE_GUARD_PID_KEY] > 0
        assert isinstance(configurable[CHECKPOINT_FENCE_GUARD_KEY_KEY], int)
    assert writes == 1


def test_guard_fence_rejects_a_terminated_backend_with_a_live_registry_lease() -> None:
    """A lost advisory session fences writes before the lease's TTL expires."""

    async def scenario() -> tuple[int, int]:
        engine = _engine()
        try:
            async with _claimed_guard_fence(engine) as (fence, guard):
                saver = PostgresCheckpointSaver(engine, require_fence=True)
                async with engine.begin() as connection:
                    terminated = bool(
                        (
                            await connection.execute(
                                text(
                                    "SELECT pg_terminate_backend(CAST(:pid AS integer))"
                                ),
                                {"pid": guard.backend_pid},
                            )
                        ).scalar_one()
                    )
                assert terminated

                # The Registry row remains ``running`` with its unexpired
                # lease. Only the independently pinned guard fact changed.
                with pytest.raises(StaleCheckpointWriteError, match="stale"):
                    await saver.aput(
                        _fenced_config(fence),
                        _checkpoint(),
                        {"source": "loop", "step": 0},
                        {"objective": "00000000000000000000000000000001.0"},
                    )
                with pytest.raises(StaleCheckpointWriteError, match="stale"):
                    await saver.aput_writes(
                        _fenced_config(fence, checkpoint_id="checkpoint_guard_lost"),
                        [("objective", "must not persist")],
                        "task_node",
                    )
                async with engine.connect() as connection:
                    checkpoints = len(
                        (await connection.execute(select(workflow_checkpoints))).all()
                    )
                    writes = len(
                        (
                            await connection.execute(select(workflow_checkpoint_writes))
                        ).all()
                    )
                return checkpoints, writes
        finally:
            await engine.dispose()

    assert _run(scenario) == (0, 0)


def test_inside_checkpoint_put_crash_rolls_back_the_blobs_and_checkpoint_together() -> (
    None
):
    async def scenario() -> tuple[int, int]:
        engine = _engine()
        try:
            async with _claimed_guard_fence(engine) as (fence, _):
                controller = FailpointController(frozenset({"inside_checkpoint_put"}))
                controller.arm("inside_checkpoint_put", mode="crash")
                saver = PostgresCheckpointSaver(
                    engine,
                    require_fence=True,
                    fault_injector=controller,
                )
                with pytest.raises(InjectedCrash):
                    await saver.aput(
                        _fenced_config(fence),
                        _checkpoint(),
                        {"source": "loop", "step": 0},
                        {"objective": "00000000000000000000000000000001.0"},
                    )
                await controller.wait_until_hit("inside_checkpoint_put")
                async with engine.connect() as connection:
                    blobs = len(
                        (
                            await connection.execute(select(workflow_checkpoint_blobs))
                        ).all()
                    )
                    checkpoints = len(
                        (await connection.execute(select(workflow_checkpoints))).all()
                    )
                return blobs, checkpoints
        finally:
            await engine.dispose()

    assert _run(scenario) == (0, 0)


def test_missing_nonempty_blob_fails_closed_instead_of_dropping_a_channel() -> None:
    async def scenario() -> None:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await saver.aput(
                _config(),
                _checkpoint(),
                {"source": "loop", "step": 0},
                {"objective": "00000000000000000000000000000001.0"},
            )
            async with engine.begin() as connection:
                await connection.execute(workflow_checkpoint_blobs.delete())
            with pytest.raises(CheckpointCorruptionError, match="missing channel blob"):
                await saver.aget_tuple(_config())
        finally:
            await engine.dispose()

    _run(scenario)


def test_fence_row_lock_and_checkpoint_write_share_one_transaction() -> None:
    class _BarrierSaver(PostgresCheckpointSaver):
        def __init__(
            self,
            *args: Any,
            locked: asyncio.Event,
            release: asyncio.Event,
            **kwargs: Any,
        ) -> None:
            super().__init__(*args, **kwargs)
            self._locked = locked
            self._release = release

        async def _assert_fence(
            self, connection: Any, thread_id: str, fence: CheckpointFence | None
        ) -> None:
            await super()._assert_fence(connection, thread_id, fence)
            self._locked.set()
            await self._release.wait()

    async def scenario() -> int:
        engine = _engine()
        try:
            async with _claimed_guard_fence(engine) as (fence, _):
                locked = asyncio.Event()
                release = asyncio.Event()
                saver = _BarrierSaver(
                    engine,
                    require_fence=True,
                    locked=locked,
                    release=release,
                )
                write = asyncio.create_task(
                    saver.aput(
                        _fenced_config(fence),
                        _checkpoint(),
                        {"source": "loop", "step": 0},
                        {"objective": "00000000000000000000000000000001.0"},
                    )
                )
                await locked.wait()
                # NOWAIT is deterministic: the fence query has already acquired
                # its row lock, so a competing lease mutation cannot slip between
                # validation and the checkpoint write.
                with pytest.raises(DBAPIError):
                    async with engine.connect() as connection, connection.begin():
                        await connection.execute(
                            select(task_runs.c.task_id)
                            .where(task_runs.c.task_id == fence.task_id)
                            .with_for_update(nowait=True)
                        )
                async with engine.connect() as connection:
                    assert (
                        len(
                            (
                                await connection.execute(select(workflow_checkpoints))
                            ).all()
                        )
                        == 0
                    )
                release.set()
                await write
                async with engine.connect() as connection:
                    return len(
                        (await connection.execute(select(workflow_checkpoints))).all()
                    )
        finally:
            await engine.dispose()

    assert _run(scenario) == 1


# --------------------------------------------------------------------------
# Versions, and the sync half


def test_versions_increase_and_two_writers_never_mint_the_same_one() -> None:
    saver = PostgresCheckpointSaver.__new__(PostgresCheckpointSaver)

    first = saver.get_next_version(None, None)
    second = saver.get_next_version(first, None)
    rival = saver.get_next_version(first, None)

    # Ordering is what the pregel loop uses to decide which nodes have already
    # seen a channel, and these are strings, so it has to survive `>`.
    assert second > first
    assert rival > first
    # Two processes stepping the same channel from the same version produce
    # different blob keys instead of overwriting each other.
    assert second != rival
    assert second.split(".")[0] == rival.split(".")[0]


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("get_tuple", ({"configurable": {"thread_id": "t"}},)),
        ("list", (None,)),
        ("put", ({"configurable": {"thread_id": "t"}}, {}, {}, {})),
        ("put_writes", ({"configurable": {"thread_id": "t"}}, [], "task")),
        ("delete_thread", ("t",)),
    ],
)
def test_the_synchronous_half_refuses_rather_than_starting_a_loop(
    method: str, arguments: tuple[Any, ...]
) -> None:
    """A sync entry point here would deadlock the first caller that has a loop."""

    saver = PostgresCheckpointSaver.__new__(PostgresCheckpointSaver)

    with pytest.raises(NotImplementedError, match="async-only"):
        getattr(saver, method)(*arguments)


# --------------------------------------------------------------------------
# Retention: deleting a thread's checkpoints (1.3)


async def _counts(engine: Any) -> tuple[int, int, int]:
    async with engine.connect() as connection:
        return (
            len((await connection.execute(select(workflow_checkpoints))).all()),
            len((await connection.execute(select(workflow_checkpoint_blobs))).all()),
            len((await connection.execute(select(workflow_checkpoint_writes))).all()),
        )


def test_deleting_a_thread_removes_its_checkpoints_blobs_and_writes() -> None:
    """All three tables, or the thread is not gone -- it is broken.

    Blobs whose checkpoints were removed are unreachable bytes; checkpoints
    whose blobs were removed fail closed on read. A half-deleted thread is
    worse than either keeping it or removing it.
    """

    async def scenario() -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await _drive(saver)
            before = await _counts(engine)
            await saver.adelete_thread(THREAD)
            return before, await _counts(engine)
        finally:
            await engine.dispose()

    before, after = _run(scenario)

    # The run really did write to all three.
    assert all(count > 0 for count in before)
    assert after == (0, 0, 0)


def test_deleting_a_thread_leaves_other_threads_alone() -> None:
    async def scenario() -> tuple[int, int, int]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await _drive(saver, thread_id="thr_keep")
            await _drive(saver, thread_id="thr_drop")
            await saver.adelete_thread("thr_drop")
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        select(workflow_checkpoints.c.thread_id).distinct()
                    )
                ).all()
            remaining = {row.thread_id for row in rows}
            assert remaining == {"thr_keep"}
            return await _counts(engine)
        finally:
            await engine.dispose()

    assert all(count > 0 for count in _run(scenario))


def test_an_orphan_write_is_removed_with_its_thread() -> None:
    """Writes naming a checkpoint that never committed need no separate sweep.

    They are routine -- under LangGraph's default durability a step's writes are
    issued before its checkpoint commits -- and they still carry the thread they
    belong to, so nothing can strand them.
    """

    async def scenario() -> tuple[int, tuple[int, int, int]]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            orphan = {
                "configurable": {
                    "thread_id": THREAD,
                    "checkpoint_id": "1f18a895-0000-0000-0000-never-committed",
                }
            }
            await saver.aput_writes(
                orphan,
                [("objective", "a write whose checkpoint never landed")],
                "task_orphan",
            )
            async with engine.connect() as connection:
                orphans = len(
                    (await connection.execute(select(workflow_checkpoint_writes))).all()
                )
            await saver.adelete_thread(THREAD)
            return orphans, await _counts(engine)
        finally:
            await engine.dispose()

    orphans, after = _run(scenario)

    assert orphans == 1
    assert after == (0, 0, 0)


def test_a_threads_checkpoints_survive_while_its_task_can_still_run() -> None:
    """Refused, not best-effort: this is the recovery position itself.

    Deleting it for an unfinished Task would look, to the next Worker, exactly
    like a Task that had never started -- so it would start over rather than
    resume, and the loss would be invisible.
    """

    async def scenario() -> tuple[str, str, tuple[int, int, int]]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await _drive(saver)
            registry = PostgresTaskRegistry(engine)
            await registry.submit(
                TaskSubmission(
                    tenant_id="tenant_a",
                    owner_id="user_1",
                    thread_id=THREAD,
                    graph_version="v1",
                    input_ref="input_1",
                    input_fingerprint="a" * 64,
                    submission_dedup_key="dedup_retention",
                    run_semantics_snapshot={"model": {"provider": "test"}},
                    run_semantics_revision="test-v1",
                    submitted_policy_revision="policy-v1",
                    submitted_policy_fingerprint="f" * 16,
                    submitted_authorization_envelope=AuthorizationEnvelope(),
                )
            )
            with pytest.raises(ThreadStillExecutingError) as queued:
                await saver.adelete_thread(THREAD)
            claim = await registry.claim_next("worker_1", lease_seconds=60)
            assert claim is not None
            with pytest.raises(ThreadStillExecutingError) as running:
                await saver.adelete_thread(THREAD)
            return queued.value.status, running.value.status, await _counts(engine)
        finally:
            await engine.dispose()

    queued_status, running_status, counts = _run(scenario)

    assert queued_status == "queued"
    assert running_status == "running"
    # Nothing was removed on the way to being refused.
    assert all(count > 0 for count in counts)


def test_a_finished_tasks_thread_can_be_collected() -> None:
    """The other half of the same rule: terminal work releases its position."""

    async def scenario() -> tuple[int, int, int]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await _drive(saver)
            registry = PostgresTaskRegistry(engine)
            await registry.submit(
                TaskSubmission(
                    tenant_id="tenant_a",
                    owner_id="user_1",
                    thread_id=THREAD,
                    graph_version="v1",
                    input_ref="input_1",
                    input_fingerprint="a" * 64,
                    submission_dedup_key="dedup_retention",
                    run_semantics_snapshot={"model": {"provider": "test"}},
                    run_semantics_revision="test-v1",
                    submitted_policy_revision="policy-v1",
                    submitted_policy_fingerprint="f" * 16,
                    submitted_authorization_envelope=AuthorizationEnvelope(),
                )
            )
            claim = await registry.claim_next("worker_1", lease_seconds=60)
            assert claim is not None
            await registry.mark_succeeded(claim.lease)
            await saver.adelete_thread(THREAD)
            return await _counts(engine)
        finally:
            await engine.dispose()

    assert _run(scenario) == (0, 0, 0)


def test_a_thread_no_task_owns_can_be_collected() -> None:
    """A thread with no Registry row is a stray, and strays are collectable."""

    async def scenario() -> tuple[int, int, int]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await _drive(saver)
            await saver.adelete_thread(THREAD)
            return await _counts(engine)
        finally:
            await engine.dispose()

    assert _run(scenario) == (0, 0, 0)


def test_deleting_a_thread_that_was_never_written_is_not_an_error() -> None:
    async def scenario() -> tuple[int, int, int]:
        engine = _engine()
        try:
            saver = PostgresCheckpointSaver(engine)
            await saver.adelete_thread("thr_never_existed")
            return await _counts(engine)
        finally:
            await engine.dispose()

    assert _run(scenario) == (0, 0, 0)
