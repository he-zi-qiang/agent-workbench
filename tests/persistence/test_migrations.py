"""The migration and the model metadata have to describe the same schema.

Two definitions of one schema drift the first time somebody edits a column in
the place that felt closer. Alembic can compare them, so the disagreement is a
failing test rather than a surprise the next time autogenerate runs.

The downgrade is exercised too. A downgrade nobody has run is discovered during
the incident that needed it.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text

from agent_workbench.adapters.persistence import create_query_engine, metadata
from agent_workbench.bootstrap.paths import PROJECT_ROOT
from agent_workbench.domain.schema import DOMAIN_SCHEMA_VERSION

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
# Alembic's env.py reads the DSN from the same variable the application does.
MIGRATION_DSN_ENV_VAR = "AW_DATABASE__DSN"

# Alembic's own bookkeeping table is not part of the application's schema, so a
# comparison naturally reports it as unexpected. It is the one exception.
ALEMBIC_VERSION_TABLE = "alembic_version"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return config


def _mentions_alembic_table(difference: object) -> bool:
    return ALEMBIC_VERSION_TABLE in repr(difference)


def _differences(dsn: str) -> list[object]:
    async def compare() -> list[object]:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.connect() as connection:

                def run(sync_connection: Connection) -> list[object]:
                    context = MigrationContext.configure(sync_connection)
                    return list(compare_metadata(context, metadata))

                return await connection.run_sync(run)
        finally:
            await engine.dispose()

    return [
        difference
        for difference in asyncio.run(compare())
        if not _mentions_alembic_table(difference)
    ]


def test_the_migrated_schema_matches_the_model_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = _dsn()
    monkeypatch.setenv(MIGRATION_DSN_ENV_VAR, dsn)

    command.upgrade(_config(), "head")

    assert _differences(dsn) == []


def test_the_migration_can_be_undone_and_reapplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = _dsn()
    monkeypatch.setenv(MIGRATION_DSN_ENV_VAR, dsn)
    config = _config()

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert _differences(dsn) == []


def test_event_schema_version_backfills_existing_rows_without_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows from 0005 become explicit v1 rows; future writes cannot omit it."""

    dsn = _dsn()
    monkeypatch.setenv(MIGRATION_DSN_ENV_VAR, dsn)
    config = _config()
    command.upgrade(config, "head")
    command.downgrade(config, "0005_last_applied_revision")

    async def seed_old_event() -> None:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text("TRUNCATE events, event_streams CASCADE"))
                await connection.execute(
                    text(
                        "INSERT INTO event_streams (stream_id, last_sequence) "
                        "VALUES ('str_before_schema_version', 1)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO events "
                        "(event_id, stream_id, run_id, sequence, event_type, payload) "
                        "VALUES "
                        "('evt_before_schema_version', 'str_before_schema_version', "
                        "'run_before_schema_version', 1, 'RunCompleted', "
                        "CAST(:payload AS jsonb))"
                    ),
                    {"payload": '{"kind":"RunCompleted","stop_reason":"completed"}'},
                )
        finally:
            await engine.dispose()

    async def inspect_upgraded_event() -> tuple[int, str, str | None]:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.connect() as connection:
                version = (
                    await connection.execute(
                        text(
                            "SELECT schema_version FROM events "
                            "WHERE event_id = 'evt_before_schema_version'"
                        )
                    )
                ).scalar_one()
                column = (
                    await connection.execute(
                        text(
                            "SELECT is_nullable, column_default "
                            "FROM information_schema.columns "
                            "WHERE table_schema = current_schema() "
                            "AND table_name = 'events' "
                            "AND column_name = 'schema_version'"
                        )
                    )
                ).one()
                return int(version), str(column.is_nullable), column.column_default
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed_old_event())
        command.upgrade(config, "head")
        observed = asyncio.run(inspect_upgraded_event())
    finally:
        # Leave the shared integration database at the revision every other
        # persistence test expects, even if the assertion or inspection fails.
        command.upgrade(config, "head")

    assert observed == (DOMAIN_SCHEMA_VERSION, "NO", None)


def test_migrations_refuse_to_run_without_a_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration against an unset DSN would pick whatever the driver defaults to."""

    _dsn()
    monkeypatch.delenv(MIGRATION_DSN_ENV_VAR, raising=False)

    with pytest.raises(RuntimeError, match=MIGRATION_DSN_ENV_VAR):
        command.upgrade(_config(), "head")
