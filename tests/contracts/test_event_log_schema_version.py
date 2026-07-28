"""The PostgreSQL event log persists the envelope contract it replays."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text, update

from agent_workbench.adapters.persistence import PostgresEventLog, create_query_engine
from agent_workbench.adapters.persistence.models import events
from agent_workbench.domain.events import RunStarted
from agent_workbench.domain.runs import RunBudget
from agent_workbench.domain.schema import DOMAIN_SCHEMA_VERSION
from agent_workbench.ports.event_log import EventScope

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
REQUIRED_TEST_DATABASE_SUFFIX = "_test"

SCOPE = EventScope(stream_id="str_schema_version", run_id="run_schema_version")
BUDGET = RunBudget(max_steps=4, max_tool_calls=4)
RECORDED_AT = datetime(2026, 7, 28, 12, 34, 56, tzinfo=UTC)


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    database = dsn.rsplit("/", maxsplit=1)[-1].split("?", maxsplit=1)[0]
    if not database.endswith(REQUIRED_TEST_DATABASE_SUFFIX):
        raise AssertionError(
            f"{TEST_DSN_ENV_VAR} must name a database ending in "
            f"{REQUIRED_TEST_DATABASE_SUFFIX!r}; this suite truncates it"
        )
    return dsn


def _started() -> RunStarted:
    return RunStarted(run_kind="chat", model_profile="main", budget=BUDGET)


def test_round_trip_preserves_envelope_version_and_producer_time() -> None:
    async def scenario() -> tuple[int, int, int, datetime, datetime]:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text("TRUNCATE events, event_streams CASCADE"))

            log = PostgresEventLog(engine, clock=lambda: RECORDED_AT)
            written = await log.append(SCOPE, _started())
            async with engine.connect() as connection:
                persisted = (
                    await connection.execute(
                        select(
                            events.c.schema_version,
                            events.c.recorded_at,
                        ).where(events.c.event_id == written.event_id)
                    )
                ).one()
            replayed = (await log.read(SCOPE.stream_id))[0]
            return (
                written.schema_version,
                int(persisted.schema_version),
                replayed.schema_version,
                persisted.recorded_at,
                replayed.timestamp,
            )
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (
        DOMAIN_SCHEMA_VERSION,
        DOMAIN_SCHEMA_VERSION,
        DOMAIN_SCHEMA_VERSION,
        RECORDED_AT,
        RECORDED_AT,
    )


def test_replay_fails_closed_on_an_unknown_persisted_version() -> None:
    async def scenario() -> None:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text("TRUNCATE events, event_streams CASCADE"))

            log = PostgresEventLog(engine)
            written = await log.append(SCOPE, _started())
            async with engine.begin() as connection:
                await connection.execute(
                    update(events)
                    .where(events.c.event_id == written.event_id)
                    .values(schema_version=DOMAIN_SCHEMA_VERSION + 1)
                )

            await log.read(SCOPE.stream_id)
        finally:
            await engine.dispose()

    with pytest.raises(ValidationError, match="unsupported domain schema version"):
        asyncio.run(scenario())
