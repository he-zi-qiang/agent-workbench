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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_workbench.adapters.persistence import create_query_engine, metadata
from agent_workbench.adapters.persistence.conversation_store import (
    PostgresConversationStore,
)
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


def test_knowledge_base_entities_backfill_existing_document_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy document labels become durable, tenant-scoped knowledge bases."""

    dsn = _dsn()
    monkeypatch.setenv(MIGRATION_DSN_ENV_VAR, dsn)
    config = _config()
    command.upgrade(config, "head")
    command.downgrade(config, "0019_tool_executions")

    async def seed_legacy_documents() -> None:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO documents (
                            document_id,
                            tenant_id,
                            owner_id,
                            knowledge_base_id,
                            source_revision,
                            last_applied_revision,
                            deleted,
                            created_at
                        ) VALUES
                            (
                                'doc_kb_backfill_first',
                                'tenant_kb_backfill_a',
                                'user_kb_backfill_first',
                                'kb_legacy_shared',
                                1,
                                0,
                                false,
                                TIMESTAMPTZ '2026-01-01 12:00:00+00'
                            ),
                            (
                                'doc_kb_backfill_second',
                                'tenant_kb_backfill_a',
                                'user_kb_backfill_second',
                                'kb_legacy_shared',
                                1,
                                0,
                                false,
                                TIMESTAMPTZ '2026-01-02 12:00:00+00'
                            ),
                            (
                                'doc_kb_backfill_other_tenant',
                                'tenant_kb_backfill_b',
                                'user_kb_backfill_other',
                                'kb_legacy_shared',
                                1,
                                0,
                                false,
                                TIMESTAMPTZ '2026-01-03 12:00:00+00'
                            )
                        """
                    )
                )
        finally:
            await engine.dispose()

    async def inspect_backfill() -> list[tuple[object, ...]]:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            """
                            SELECT
                                tenant_id,
                                owner_id,
                                name,
                                description,
                                created_at = updated_at AS timestamps_match
                            FROM knowledge_bases
                            WHERE tenant_id LIKE 'tenant_kb_backfill_%'
                              AND knowledge_base_id = 'kb_legacy_shared'
                            ORDER BY tenant_id
                            """
                        )
                    )
                ).all()
                return [tuple(row) for row in rows]
        finally:
            await engine.dispose()

    async def remove_fixtures() -> None:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM documents "
                        "WHERE document_id LIKE 'doc_kb_backfill_%'"
                    )
                )
                await connection.execute(
                    text(
                        "DELETE FROM knowledge_bases "
                        "WHERE tenant_id LIKE 'tenant_kb_backfill_%'"
                    )
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed_legacy_documents())
        command.upgrade(config, "head")
        observed = asyncio.run(inspect_backfill())
    finally:
        # Keep the shared test database usable after a failing migration test.
        command.upgrade(config, "head")
        asyncio.run(remove_fixtures())

    assert observed == [
        (
            "tenant_kb_backfill_a",
            "user_kb_backfill_first",
            "kb_legacy_shared",
            None,
            True,
        ),
        (
            "tenant_kb_backfill_b",
            "user_kb_backfill_other",
            "kb_legacy_shared",
            None,
            True,
        ),
    ]


def test_sessions_that_predate_the_mode_column_become_chat_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every session written before 0026 was a chat session, so it says so.

    The second half is the constraint, and it is the half that makes the
    column a gate: a writer that skips the repository must not be able to
    invent a third mode that every reader's ``mode=`` predicate then misses.
    """

    dsn = _dsn()
    monkeypatch.setenv(MIGRATION_DSN_ENV_VAR, dsn)
    config = _config()
    command.upgrade(config, "head")
    command.downgrade(config, "0025_agent_invocation_count")

    async def seed_legacy_session() -> None:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO conversation_sessions "
                        "(session_id, tenant_id, owner_id, title) VALUES "
                        "('ses_before_mode', 'tenant_before_mode', "
                        "'user_before_mode', NULL)"
                    )
                )
        finally:
            await engine.dispose()

    async def inspect_upgraded_session() -> tuple[str, str, bool, bool]:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.connect() as connection:
                mode = (
                    await connection.execute(
                        text(
                            "SELECT mode FROM conversation_sessions "
                            "WHERE session_id = 'ses_before_mode'"
                        )
                    )
                ).scalar_one()
                nullable = (
                    await connection.execute(
                        text(
                            "SELECT is_nullable FROM information_schema.columns "
                            "WHERE table_schema = current_schema() "
                            "AND table_name = 'conversation_sessions' "
                            "AND column_name = 'mode'"
                        )
                    )
                ).scalar_one()
                return (
                    str(mode),
                    str(nullable),
                    await _mode_is_storable(connection, "code"),
                    await _mode_is_storable(connection, "shell"),
                )
        finally:
            await engine.dispose()

    async def remove_fixtures() -> None:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM conversation_sessions "
                        "WHERE session_id LIKE 'ses_%_mode'"
                    )
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed_legacy_session())
        command.upgrade(config, "head")
        observed = asyncio.run(inspect_upgraded_session())
    finally:
        command.upgrade(config, "head")
        asyncio.run(remove_fixtures())

    assert observed == ("chat", "NO", True, False)


async def _mode_is_storable(connection: AsyncConnection, mode: str) -> bool:
    """Whether the CHECK admits this mode, without poisoning the transaction."""

    # The savepoint is what makes the rejected insert survivable: a violated
    # CHECK aborts the whole transaction, so a second probe on the same
    # connection would fail for a reason that has nothing to do with its mode.
    try:
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO conversation_sessions "
                    "(session_id, tenant_id, owner_id, mode) VALUES "
                    "(:session_id, 'tenant_before_mode', 'user_before_mode', :mode)"
                ),
                {"session_id": f"ses_{mode}_mode", "mode": mode},
            )
    except IntegrityError:
        return False
    return True


def test_a_session_that_predates_the_pointer_is_at_no_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NULL, and the compare-and-set can still move it from there.

    The second half is what needs the database. "No version yet" is the state
    every session's first write compares against, and for a row that predates
    the column it is a state the migration created rather than the store. A
    schema whose default made that row unaddressable -- or a comparison that
    could not name it -- would leave every session that existed before this
    migration unable to write its first file, and nothing in the store's own
    tests would notice, because they create every session they use.
    """

    dsn = _dsn()
    monkeypatch.setenv(MIGRATION_DSN_ENV_VAR, dsn)
    config = _config()
    command.upgrade(config, "head")
    command.downgrade(config, "0026_session_mode")

    async def seed_legacy_session() -> None:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO conversation_sessions "
                        "(session_id, tenant_id, owner_id, mode) VALUES "
                        "('ses_before_pointer', 'tenant_before_pointer', "
                        "'user_before_pointer', 'chat')"
                    )
                )
        finally:
            await engine.dispose()

    async def inspect_upgraded_session() -> tuple[str | None, str, str | None]:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.connect() as connection:
                before = (
                    await connection.execute(
                        text(
                            "SELECT workspace_version FROM conversation_sessions "
                            "WHERE session_id = 'ses_before_pointer'"
                        )
                    )
                ).scalar_one()
                nullable = (
                    await connection.execute(
                        text(
                            "SELECT is_nullable FROM information_schema.columns "
                            "WHERE table_schema = current_schema() "
                            "AND table_name = 'conversation_sessions' "
                            "AND column_name = 'workspace_version'"
                        )
                    )
                ).scalar_one()

            # The real store, against the row the migration produced.
            store = PostgresConversationStore(engine)
            await store.advance_workspace_version(
                session_id="ses_before_pointer",
                tenant_id="tenant_before_pointer",
                principal_id="user_before_pointer",
                expected=None,
                next_version="art_first_write",
            )
            session = await store.session(
                session_id="ses_before_pointer",
                tenant_id="tenant_before_pointer",
                principal_id="user_before_pointer",
            )
            return (before, str(nullable), session.workspace_version)
        finally:
            await engine.dispose()

    async def remove_fixtures() -> None:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM conversation_sessions "
                        "WHERE session_id = 'ses_before_pointer'"
                    )
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed_legacy_session())
        command.upgrade(config, "head")
        observed = asyncio.run(inspect_upgraded_session())
    finally:
        command.upgrade(config, "head")
        asyncio.run(remove_fixtures())

    assert observed == (None, "YES", "art_first_write")


def test_migrations_refuse_to_run_without_a_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration against an unset DSN would pick whatever the driver defaults to."""

    _dsn()
    monkeypatch.delenv(MIGRATION_DSN_ENV_VAR, raising=False)

    with pytest.raises(RuntimeError, match=MIGRATION_DSN_ENV_VAR):
        command.upgrade(_config(), "head")
