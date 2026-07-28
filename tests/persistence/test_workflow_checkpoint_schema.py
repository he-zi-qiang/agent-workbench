"""The schema a LangGraph checkpoint saver writes into.

There is no saver yet -- ADR-014 decided to write one, and this is its storage.
What can be established before a line of it exists is that the tables hold what
the contract actually produces, so the first thing checked here is not a
synthetic row but every row a real run of the v1 graph would have written.

The rest fix the decisions that are invisible in a column list: a write may name
a checkpoint that is not stored yet, a blob is keyed by (channel, version)
rather than by the checkpoint that produced it, and metadata is JSONB while
everything else is opaque bytes. Each of those is something a later reader could
plausibly "tidy up" into a foreign key, a narrower key or a uniform blob, so
each has a test that fails when they do.

Real PostgreSQL only. Every assertion here is about what the database accepts.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest
from langgraph.checkpoint.memory import (  # pyright: ignore[reportMissingTypeStubs]
    InMemorySaver,
)
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.langgraph.workflow import build_v1_graph
from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.adapters.persistence.models import (
    workflow_checkpoint_blobs,
    workflow_checkpoint_writes,
    workflow_checkpoints,
)
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = "workflow_checkpoints, workflow_checkpoint_blobs, workflow_checkpoint_writes"

THREAD = "thr_schema"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(scenario: Callable[[AsyncEngine], Awaitable[Any]]) -> Any:
    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES}"))
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(execute())


# --------------------------------------------------------------------------
# A real run of the v1 graph, recorded in the shape the tables expect


class RecordingSaver(InMemorySaver):
    """Turns the contract's calls into rows, and stores nothing itself.

    The rows are assembled here rather than by a helper the future saver will
    share, because a shared helper would let the schema and this test agree
    with each other while both disagree with LangGraph.
    """

    def __init__(self) -> None:
        super().__init__()
        self.recorded_checkpoints: list[dict[str, Any]] = []
        self.recorded_blobs: list[dict[str, Any]] = []
        self.recorded_writes: list[dict[str, Any]] = []

    async def aput(
        self,
        config: Any,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any,
    ) -> Any:
        configurable = config["configurable"]
        thread_id = configurable["thread_id"]
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        remainder = dict(checkpoint)
        values = remainder.pop("channel_values")
        payload_type, payload = self.serde.dumps_typed(remainder)
        self.recorded_checkpoints.append(
            {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
                "parent_checkpoint_id": configurable.get("checkpoint_id"),
                "payload_type": payload_type,
                "payload": payload,
                "metadata": dict(metadata),
            }
        )
        for channel, version in new_versions.items():
            blob_type, blob = (
                self.serde.dumps_typed(values[channel])
                if channel in values
                else ("empty", b"")
            )
            self.recorded_blobs.append(
                {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "channel": channel,
                    "version": str(version),
                    "payload_type": blob_type,
                    "payload": blob,
                }
            )
        return await super().aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config["configurable"]
        for index, (channel, value) in enumerate(writes):
            payload_type, payload = self.serde.dumps_typed(value)
            self.recorded_writes.append(
                {
                    "thread_id": configurable["thread_id"],
                    "checkpoint_ns": configurable.get("checkpoint_ns", ""),
                    "checkpoint_id": configurable["checkpoint_id"],
                    "task_id": task_id,
                    "idx": index,
                    "channel": channel,
                    "task_path": task_path,
                    "payload_type": payload_type,
                    "payload": payload,
                }
            )
        await super().aput_writes(config, writes, task_id, task_path)


def _handlers() -> dict[str, Any]:
    """Deterministic stand-ins, one channel each, taken to a passing review."""

    async def understand(state: TaskState) -> dict[str, Any]:
        return {"agent_outcome_refs": ("run_understand",)}

    async def internal(state: TaskState) -> dict[str, Any]:
        return {
            "evidence_refs": ("ev_internal",),
            "agent_outcome_refs": ("run_internal",),
        }

    async def external(state: TaskState) -> dict[str, Any]:
        return {
            "evidence_refs": ("ev_external",),
            "agent_outcome_refs": ("run_external",),
        }

    async def synthesize(state: TaskState) -> dict[str, Any]:
        return {"draft_ref": "draft_1", "review_result": None}

    async def critic(state: TaskState) -> dict[str, Any]:
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


async def _record_a_real_run() -> RecordingSaver:
    saver = RecordingSaver()
    graph = build_v1_graph(_handlers()).compile(checkpointer=saver)
    state = TaskState.model_validate(
        {
            "task_id": "task_1",
            "objective": "Compare retrieval strategies.",
            "plan": (
                TaskStep(
                    step_id="step_1", sequence=1, objective="Gather internal notes."
                ),
            ),
        }
    )
    await graph.ainvoke(state.model_dump(), {"configurable": {"thread_id": THREAD}})
    return saver


def test_the_schema_holds_every_row_a_real_graph_run_produces() -> None:
    """The v1 graph, checkpointed and stored -- unchanged, byte for byte."""

    async def scenario(engine: AsyncEngine) -> dict[str, Any]:
        saver = await _record_a_real_run()
        assert (
            saver.recorded_checkpoints
            and saver.recorded_blobs
            and saver.recorded_writes
        )

        async with engine.begin() as connection:
            await connection.execute(
                insert(workflow_checkpoints), saver.recorded_checkpoints
            )
            await connection.execute(
                insert(workflow_checkpoint_blobs), saver.recorded_blobs
            )
            await connection.execute(
                insert(workflow_checkpoint_writes), saver.recorded_writes
            )

        async with engine.connect() as connection:
            stored = (await connection.execute(select(workflow_checkpoints))).all()
            blobs = (await connection.execute(select(workflow_checkpoint_blobs))).all()
            writes = (
                await connection.execute(select(workflow_checkpoint_writes))
            ).all()

        expected = {row["checkpoint_id"]: row for row in saver.recorded_checkpoints}
        return {
            "recorded": (
                len(saver.recorded_checkpoints),
                len(saver.recorded_blobs),
                len(saver.recorded_writes),
            ),
            "stored": (len(stored), len(blobs), len(writes)),
            # Named rather than counted, so a failure says which row changed.
            "altered": [
                row.checkpoint_id
                for row in stored
                if (row.payload_type, bytes(row.payload), row.parent_checkpoint_id)
                != (
                    expected[row.checkpoint_id]["payload_type"],
                    expected[row.checkpoint_id]["payload"],
                    expected[row.checkpoint_id]["parent_checkpoint_id"],
                )
            ],
            "roots": [
                row.checkpoint_id for row in stored if row.parent_checkpoint_id is None
            ],
            # JSONB, not bytes: `alist(filter=...)` has to be able to see these.
            "metadata": sorted(
                (row.metadata["step"], row.metadata["source"]) for row in stored
            ),
        }

    observed = _run(scenario)

    checkpoints, blobs, writes = observed["recorded"]
    # A run with one checkpoint would prove nothing about a chain of them.
    assert checkpoints > 1
    assert blobs > checkpoints
    assert writes > checkpoints
    assert observed["stored"] == observed["recorded"]
    assert observed["altered"] == []
    # A thread has exactly one checkpoint without a parent; the rest chain back
    # to it, which is what a resume walks.
    assert len(observed["roots"]) == 1
    # The first checkpoint is the input, and the steps after it are the loop.
    assert observed["metadata"][0] == (-1, "input")
    assert {source for _, source in observed["metadata"]} == {"input", "loop"}


def test_a_stored_checkpoint_deserialises_back_into_langgraph_s_own_object() -> None:
    """Storing is not enough: the resume path has to get its object back."""

    async def scenario(engine: AsyncEngine) -> tuple[str, dict[str, Any]]:
        saver = await _record_a_real_run()
        # The last one: the checkpoint a restarted process would resume from.
        latest = saver.recorded_checkpoints[-1]
        async with engine.begin() as connection:
            await connection.execute(insert(workflow_checkpoints), [latest])
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        workflow_checkpoints.c.payload_type,
                        workflow_checkpoints.c.payload,
                    )
                )
            ).one()
        restored = saver.serde.loads_typed((row.payload_type, bytes(row.payload)))
        return latest["checkpoint_id"], restored

    checkpoint_id, restored = _run(scenario)

    assert restored["id"] == checkpoint_id
    # What a resume reads to decide which nodes still have work to do.
    assert set(restored) >= {"id", "channel_versions", "versions_seen"}
    assert restored["channel_versions"]


# --------------------------------------------------------------------------
# The decisions that are not visible in the column list


def test_a_write_may_name_a_checkpoint_that_is_not_stored_yet() -> None:
    """Why there is no foreign key from writes to checkpoints.

    Under LangGraph's default ``durability="async"`` the checkpoint put is not
    awaited before the next step's writes are issued, so a write reaching the
    database first is the ordinary case rather than a corruption. A foreign key
    would reject an ordinary run.
    """

    async def scenario(engine: AsyncEngine) -> int:
        async with engine.begin() as connection:
            await connection.execute(
                insert(workflow_checkpoint_writes),
                [
                    {
                        "thread_id": THREAD,
                        "checkpoint_ns": "",
                        "checkpoint_id": "ckpt_not_written_yet",
                        "task_id": "task_a",
                        "idx": 0,
                        "channel": "evidence_refs",
                        "task_path": "~__pregel_pull, research_internal",
                        "payload_type": "msgpack",
                        "payload": b"\x91\xa2ev",
                    }
                ],
            )
        async with engine.connect() as connection:
            return len(
                (await connection.execute(select(workflow_checkpoint_writes))).all()
            )

    assert _run(scenario) == 1


def test_a_channel_keeps_one_blob_per_version() -> None:
    """Why a blob is keyed by version rather than by a checkpoint id.

    ``aput`` is handed only the channels that changed, so an unchanged channel
    is read back from the version some older checkpoint wrote. Keying a blob by
    the checkpoint that produced it would mean either rewriting every channel on
    every step, or losing the ones that did not change.
    """

    async def scenario(engine: AsyncEngine) -> list[tuple[str, str]]:
        rows = [
            {
                "thread_id": THREAD,
                "checkpoint_ns": "",
                "channel": "evidence_refs",
                "version": version,
                "payload_type": "msgpack",
                "payload": payload,
            }
            for version, payload in (
                ("00000000000000000000000000000002.0.1", b"\x91\xa2v1"),
                ("00000000000000000000000000000006.0.9", b"\x92\xa2v1\xa2v2"),
            )
        ]
        async with engine.begin() as connection:
            await connection.execute(insert(workflow_checkpoint_blobs), rows)
        async with engine.connect() as connection:
            stored = (
                await connection.execute(
                    select(
                        workflow_checkpoint_blobs.c.channel,
                        workflow_checkpoint_blobs.c.version,
                    ).order_by(workflow_checkpoint_blobs.c.version)
                )
            ).all()
        return [(row.channel, row.version) for row in stored]

    versions = _run(scenario)

    assert [channel for channel, _ in versions] == ["evidence_refs"] * 2
    assert len({version for _, version in versions}) == 2


def test_the_same_channel_at_the_same_version_is_stored_once() -> None:
    """The other half of that key: a version is written, never appended to."""

    async def scenario(engine: AsyncEngine) -> None:
        row = {
            "thread_id": THREAD,
            "checkpoint_ns": "",
            "channel": "evidence_refs",
            "version": "00000000000000000000000000000002.0.1",
            "payload_type": "msgpack",
            "payload": b"\x91\xa2v1",
        }
        async with engine.begin() as connection:
            await connection.execute(insert(workflow_checkpoint_blobs), [row])
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(insert(workflow_checkpoint_blobs), [dict(row)])

    _run(scenario)


def test_a_namespace_separates_checkpoints_that_share_an_id() -> None:
    """A subgraph runs in its own namespace, and may reuse a checkpoint id.

    Dropping ``checkpoint_ns`` from the key looks harmless while every graph is
    flat, and merges a subgraph's history into its parent's the first time one
    is added.
    """

    async def scenario(engine: AsyncEngine) -> list[str]:
        shared_id = "1f18a895-81b4-67f2-bfff-3cd0225b7b38"
        rows = [
            {
                "thread_id": THREAD,
                "checkpoint_ns": namespace,
                "checkpoint_id": shared_id,
                "parent_checkpoint_id": None,
                "payload_type": "msgpack",
                "payload": b"\x80",
                "metadata": {"source": "loop", "step": 0, "parents": {}},
            }
            for namespace in ("", "research|1f18a895")
        ]
        async with engine.begin() as connection:
            await connection.execute(insert(workflow_checkpoints), rows)
        async with engine.connect() as connection:
            stored = (
                await connection.execute(
                    select(workflow_checkpoints.c.checkpoint_ns).order_by(
                        workflow_checkpoints.c.checkpoint_ns
                    )
                )
            ).all()
        return [row.checkpoint_ns for row in stored]

    assert _run(scenario) == ["", "research|1f18a895"]


def test_a_task_records_special_writes_beside_its_ordinary_ones() -> None:
    """``WRITES_IDX_MAP`` gives errors, interrupts and resumes negative slots.

    They share a task id with that task's ordinary writes, so an unsigned index
    or a key without ``idx`` would drop one of the two on insert.
    """

    async def scenario(engine: AsyncEngine) -> list[int]:
        rows = [
            {
                "thread_id": THREAD,
                "checkpoint_ns": "",
                "checkpoint_id": "ckpt_1",
                "task_id": "task_a",
                "idx": index,
                "channel": channel,
                "task_path": "~__pregel_pull, critic",
                "payload_type": "msgpack",
                "payload": b"\xa3err",
            }
            for index, channel in ((0, "review_result"), (-1, "__error__"))
        ]
        async with engine.begin() as connection:
            await connection.execute(insert(workflow_checkpoint_writes), rows)
        async with engine.connect() as connection:
            stored = (
                await connection.execute(
                    select(workflow_checkpoint_writes.c.idx).order_by(
                        workflow_checkpoint_writes.c.idx
                    )
                )
            ).all()
        return [row.idx for row in stored]

    assert _run(scenario) == [-1, 0]


def test_a_payload_is_stored_as_bytes_rather_than_text() -> None:
    """msgpack is not valid UTF-8, and a text column would refuse it.

    ``dumps_typed`` returns msgpack for every non-null value the graph writes,
    so this is the ordinary case and not an exotic one.
    """

    async def scenario(engine: AsyncEngine) -> bytes:
        async with engine.begin() as connection:
            await connection.execute(
                insert(workflow_checkpoints),
                [
                    {
                        "thread_id": THREAD,
                        "checkpoint_ns": "",
                        "checkpoint_id": "ckpt_bytes",
                        "parent_checkpoint_id": None,
                        "payload_type": "msgpack",
                        "payload": b"\x81\xa1a\xc0\xff\xfe",
                        "metadata": {"source": "loop", "step": 1, "parents": {}},
                    }
                ],
            )
        async with engine.connect() as connection:
            stored = (
                await connection.execute(select(workflow_checkpoints.c.payload))
            ).scalar_one()
        return bytes(stored)

    assert _run(scenario) == b"\x81\xa1a\xc0\xff\xfe"


def test_a_channel_that_carried_no_value_is_recorded_rather_than_omitted() -> None:
    """``aput`` reports versions for channels that hold nothing; they are rows.

    They arrive as the type ``empty`` with no bytes, so a payload column that
    refused an empty value would turn an ordinary write into a failure -- and
    omitting the row instead would make "never written" and "written as
    nothing" the same absence.
    """

    async def scenario(engine: AsyncEngine) -> tuple[str, int]:
        async with engine.begin() as connection:
            await connection.execute(
                insert(workflow_checkpoint_blobs),
                [
                    {
                        "thread_id": THREAD,
                        "checkpoint_ns": "",
                        "channel": "branch:to:understand",
                        "version": "00000000000000000000000000000002.0.5",
                        "payload_type": "empty",
                        "payload": b"",
                    }
                ],
            )
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        workflow_checkpoint_blobs.c.payload_type,
                        workflow_checkpoint_blobs.c.payload,
                    )
                )
            ).one()
        return row.payload_type, len(bytes(row.payload))

    assert _run(scenario) == ("empty", 0)


def test_metadata_is_queried_by_key_rather_than_loaded_and_filtered() -> None:
    """Why metadata is the one column that is not opaque.

    ``alist(filter=...)`` selects checkpoints by metadata key. Held as bytes it
    could only be answered by loading every row; held as JSONB it is a
    containment predicate the database evaluates.
    """

    async def scenario(engine: AsyncEngine) -> list[str]:
        rows = [
            {
                "thread_id": THREAD,
                "checkpoint_ns": "",
                "checkpoint_id": f"ckpt_{step}",
                "parent_checkpoint_id": None,
                "payload_type": "msgpack",
                "payload": b"\x80",
                "metadata": {"source": source, "step": step, "parents": {}},
            }
            for step, source in ((-1, "input"), (0, "loop"), (1, "update"))
        ]
        async with engine.begin() as connection:
            await connection.execute(insert(workflow_checkpoints), rows)
        async with engine.connect() as connection:
            matched = (
                await connection.execute(
                    select(workflow_checkpoints.c.checkpoint_id)
                    .where(workflow_checkpoints.c.metadata.contains({"source": "loop"}))
                    .order_by(workflow_checkpoints.c.checkpoint_id)
                )
            ).all()
        return [row.checkpoint_id for row in matched]

    assert _run(scenario) == ["ckpt_0"]


def test_metadata_that_json_cannot_hold_fails_the_write() -> None:
    """The cost of that choice, stated rather than discovered later.

    A non-JSON metadata value fails closed here instead of being pickled into
    bytes nobody can filter on.
    """

    async def scenario(engine: AsyncEngine) -> None:
        with pytest.raises((StatementError, TypeError)):
            async with engine.begin() as connection:
                await connection.execute(
                    insert(workflow_checkpoints),
                    [
                        {
                            "thread_id": THREAD,
                            "checkpoint_ns": "",
                            "checkpoint_id": "ckpt_bad_metadata",
                            "parent_checkpoint_id": None,
                            "payload_type": "msgpack",
                            "payload": b"\x80",
                            "metadata": {"source": object()},
                        }
                    ],
                )

    _run(scenario)
