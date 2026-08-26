"""The narrowed read has to be a lookup, not a scan of the stream.

An index is the one kind of change whose whole value is invisible to a
correctness test: ``read(stream_id, run_id=...)`` returns exactly the same rows
with it and without it. So this asserts the **plan**, which is the only thing
that can tell the two apart, and it does it on data shaped like the case the
index exists for -- a long Task in which one sub-agent wrote a handful of events.

Two halves, and the second is what stops the first from being a tautology: the
plan has to use the index, *and* it must not sort. Sorting is the failure the
column order was chosen to avoid -- an index on ``(stream_id, run_id)`` alone
finds the rows and then has to put a whole stream in order to return twelve of
them, which is most of the cost the index was added to remove.

Real PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.adapters.persistence.models import events

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

INDEX_NAME = "ix_events_stream_run_sequence"
STREAM = "str_index_a"
PARENT_RUN = "run_index_parent"
CHILD_RUN = "run_index_child"

#: Long enough that PostgreSQL prefers an index over reading the table.
#:
#: A few dozen rows are cheaper to scan than to look up however good the index
#: is, and a test written on twenty rows would assert the planner's opinion
#: about a table too small for the question. This is the size at which "one
#: sub-agent inside a long Task" starts being the real shape.
STREAM_EVENTS = 4_000
CHILD_EVENTS = 12


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
                await connection.execute(text("TRUNCATE events CASCADE"))
            return await scenario(engine)
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _row(index: int, run_id: str) -> dict[str, Any]:
    return {
        "event_id": f"evt_{index:012d}",
        "stream_id": STREAM,
        "run_id": run_id,
        "sequence": index,
        "event_key": None,
        "schema_version": 1,
        "event_type": "ToolStarted",
        "payload": {"kind": "ToolStarted", "tool_call_id": "t1", "tool_name": "x"},
        "task_id": None,
        "graph_node_id": None,
        "parent_event_id": None,
        "recorded_at": datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    }


async def _seed(engine: AsyncEngine) -> None:
    """One long stream in which the child wrote a short run near the end.

    Written straight through ``insert`` rather than through the store, and this
    is the one place in the suite where that is right: what is under test is the
    *planner's* choice over a table of a certain size and shape, so the rows only
    have to be real rows. Nothing here asserts anything about the store's
    behaviour.
    """

    rows = [_row(index, PARENT_RUN) for index in range(1, STREAM_EVENTS + 1)]
    rows.extend(
        _row(STREAM_EVENTS + offset, CHILD_RUN) for offset in range(1, CHILD_EVENTS + 1)
    )
    async with engine.begin() as connection:
        for start in range(0, len(rows), 500):
            await connection.execute(insert(events), rows[start : start + 500])
        # Without this the planner is working from whatever statistics the
        # table had when it was empty, and the test would measure the freshness
        # of `pg_stat` rather than the index.
        await connection.execute(text("ANALYZE events"))


async def _plan(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "EXPLAIN SELECT * FROM events "
                "WHERE stream_id = :stream AND run_id = :run "
                "ORDER BY sequence LIMIT 500"
            ),
            {"stream": STREAM, "run": CHILD_RUN},
        )
        return "\n".join(str(row[0]) for row in result)


def test_a_narrowed_read_does_not_scan_the_whole_stream() -> None:
    async def scenario(engine: AsyncEngine) -> str:
        await _seed(engine)
        return await _plan(engine)

    plan = _run(scenario)

    assert INDEX_NAME in plan, (
        "the narrowed read is not using the index it was added for:\n" + plan
    )


def test_a_narrowed_read_does_not_sort_a_stream_to_return_twelve_rows() -> None:
    """Why ``sequence`` is the third column rather than left off.

    An index on ``(stream_id, run_id)`` would satisfy the test above and still
    make PostgreSQL sort, because the read is ordered. This is the half that
    fails if somebody 'simplifies' the index later.
    """

    async def scenario(engine: AsyncEngine) -> str:
        await _seed(engine)
        return await _plan(engine)

    plan = _run(scenario)

    assert "Sort" not in plan, (
        "the narrowed read sorts, so the index is not providing the order:\n" + plan
    )
