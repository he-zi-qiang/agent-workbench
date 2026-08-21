"""Store harnesses: one contract, every implementation.

A port with two implementations and two test suites has two contracts. These
fixtures parameterize the suites instead, so the in-memory store and the real
one answer the same questions and any difference between them shows up as a
failure rather than as a surprise in production.

The PostgreSQL harness truncates between scenarios, which is why it refuses any
database whose name does not end in ``_test``. A developer who exports the
wrong DSN should get a skipped suite, not an emptied database.

The variable deliberately sits outside the ``AW_`` namespace: settings reject
any unknown ``AW_*`` variable, and that guard is worth more than the symmetry
of a matching prefix.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from harness import StoreHarness
from sqlalchemy import text

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.memory import (
    InMemoryArtifactStore,
    InMemoryConversationStore,
    InMemoryEventLog,
    InMemoryProjectStore,
)
from agent_workbench.adapters.persistence import (
    PostgresConversationStore,
    PostgresEventLog,
    PostgresProjectStore,
    create_query_engine,
)
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.conversation_store import ChatTurnStore, ConversationStore

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
REQUIRED_TEST_DATABASE_SUFFIX = "_test"


def _test_dsn() -> str | None:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        return None
    database = dsn.rsplit("/", maxsplit=1)[-1].split("?", maxsplit=1)[0]
    if not database.endswith(REQUIRED_TEST_DATABASE_SUFFIX):
        raise AssertionError(
            f"{TEST_DSN_ENV_VAR} must name a database ending in "
            f"{REQUIRED_TEST_DATABASE_SUFFIX!r}; these suites truncate it"
        )
    return dsn


@asynccontextmanager
async def _memory_conversations() -> AsyncIterator[ConversationStore]:
    yield InMemoryConversationStore()


def _postgres_conversations(dsn: str) -> Callable[[], Any]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[ConversationStore]:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("TRUNCATE conversation_sessions, messages CASCADE")
                )
            yield PostgresConversationStore(engine)
        finally:
            await engine.dispose()

    return factory


@pytest.fixture(params=["memory", "postgres"])
def conversations(request: pytest.FixtureRequest) -> StoreHarness:
    if request.param == "memory":
        return StoreHarness(name="memory", factory=_memory_conversations)

    dsn = _test_dsn()
    if dsn is None:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return StoreHarness(name="postgres", factory=_postgres_conversations(dsn))


@asynccontextmanager
async def _memory_chat_turns() -> AsyncIterator[ChatTurnStore]:
    yield _LeaseControlledMemoryConversationStore()


class _LeaseControlledMemoryConversationStore(InMemoryConversationStore):
    """Contract-only clock control without weakening the production port."""

    def __init__(self) -> None:
        self._test_now = datetime(2026, 7, 28, tzinfo=UTC)
        super().__init__(clock=lambda: self._test_now)

    async def expire_turn_for_test(self, turn_id: str) -> None:
        async with self._lock:
            turn = self._turns[turn_id]
            assert turn.lease_until is not None
            self._test_now = turn.lease_until + timedelta(microseconds=1)


class _LeaseControlledPostgresConversationStore(PostgresConversationStore):
    """Contract-only SQL seams for deterministic deadline and lock tests."""

    async def expire_turn_for_test(self, turn_id: str) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE chat_turns "
                    "SET lease_until = statement_timestamp() - INTERVAL '1 second' "
                    "WHERE turn_id = :turn_id AND status = 'running'"
                ),
                {"turn_id": turn_id},
            )

    async def hold_turn_lock_for_test(
        self,
        turn_id: str,
        *,
        locked: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "SELECT turn_id FROM chat_turns WHERE turn_id = :turn_id FOR UPDATE"
                ),
                {"turn_id": turn_id},
            )
            locked.set()
            await release.wait()


def _postgres_chat_turns(dsn: str) -> Callable[[], Any]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[ChatTurnStore]:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("TRUNCATE chat_turns, messages, conversation_sessions CASCADE")
                )
            yield _LeaseControlledPostgresConversationStore(engine)
        finally:
            await engine.dispose()

    return factory


@pytest.fixture(params=["memory", "postgres"])
def chat_turn_conversations(request: pytest.FixtureRequest) -> StoreHarness:
    """One lifecycle contract exercised against both turn-ledger stores."""

    if request.param == "memory":
        return StoreHarness(name="memory", factory=_memory_chat_turns)

    dsn = _test_dsn()
    if dsn is None:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return StoreHarness(name="postgres", factory=_postgres_chat_turns(dsn))


@asynccontextmanager
async def _memory_events() -> AsyncIterator[Any]:
    yield InMemoryEventLog()


def _postgres_events(dsn: str) -> Callable[[], Any]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text("TRUNCATE events, event_streams CASCADE"))
            yield PostgresEventLog(engine)
        finally:
            await engine.dispose()

    return factory


@pytest.fixture(params=["memory", "postgres"])
def event_logs(request: pytest.FixtureRequest) -> StoreHarness:
    """Both event logs, so the contract is pinned per implementation.

    The in-memory one made the contract executable; the PostgreSQL one has to
    keep it under concurrency, and gap-free sequencing is exactly the property
    that only shows up there.
    """

    if request.param == "memory":
        return StoreHarness(name="memory", factory=_memory_events)

    dsn = _test_dsn()
    if dsn is None:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return StoreHarness(name="postgres", factory=_postgres_events(dsn))


@asynccontextmanager
async def _memory_artifacts() -> AsyncIterator[ArtifactStore]:
    yield InMemoryArtifactStore()


def _local_artifacts(root: Path) -> Callable[[], Any]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[ArtifactStore]:
        yield LocalArtifactStore(root)

    return factory


@pytest.fixture(params=["memory", "local"])
def artifacts(request: pytest.FixtureRequest, tmp_path: Path) -> StoreHarness:
    if request.param == "memory":
        return StoreHarness(name="memory", factory=_memory_artifacts)
    return StoreHarness(name="local", factory=_local_artifacts(tmp_path / "artifacts"))


# --- projects -------------------------------------------------------------
#
# Both implementations get the same three seams, because the contract needs rows
# this store does not own: a conversation, a Task and a knowledge base. Over
# PostgreSQL they are real inserts, so ON DELETE SET NULL and the composite
# foreign keys are the things actually under test; in memory they are the
# double's ``remember_*`` calls. Named ``_for_test`` for the reason the chat-turn
# seams above are: the production port must not grow a way to invent a Task.


class _SeededMemoryProjectStore(InMemoryProjectStore):
    """The double, plus the seams the contract seeds through."""

    async def seed_session_for_test(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        session_id: str,
        mode: str = "chat",
        title: str | None = None,
        last_activity_at: datetime | None = None,
    ) -> None:
        self.remember_session(
            tenant_id=tenant_id,
            owner_id=owner_id,
            session_id=session_id,
            mode=mode,  # type: ignore[arg-type]
            title=title,
            last_activity_at=last_activity_at,
        )

    async def seed_task_for_test(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        objective_preview: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.remember_task(
            tenant_id=tenant_id,
            owner_id=owner_id,
            task_id=task_id,
            objective_preview=objective_preview,
            created_at=created_at,
        )

    async def seed_knowledge_base_for_test(
        self, *, tenant_id: str, owner_id: str, knowledge_base_id: str, name: str
    ) -> None:
        self.remember_knowledge_base(
            tenant_id=tenant_id,
            owner_id=owner_id,
            knowledge_base_id=knowledge_base_id,
            name=name,
        )

    async def session_ids_for_test(self, *, tenant_id: str) -> list[str]:
        return sorted(
            item_id
            for (held_tenant, item_id), member in self._members.items()
            if held_tenant == tenant_id and member.kind in {"chat", "code"}
        )

    async def knowledge_base_exists_for_test(
        self, *, tenant_id: str, knowledge_base_id: str
    ) -> bool:
        return (tenant_id, knowledge_base_id) in self._bases


class _SeededPostgresProjectStore(PostgresProjectStore):
    """The real store, plus inserts for the rows it does not own."""

    def __init__(self, engine: Any) -> None:
        super().__init__(engine)
        self._loop_engine = engine

    async def _execute_for_test(
        self, statement: Any, parameters: dict[str, Any]
    ) -> None:
        async with self._loop_engine.begin() as connection:
            await connection.execute(statement, parameters)

    async def _fetch_for_test(
        self, statement: Any, parameters: dict[str, Any]
    ) -> list[Any]:
        async with self._loop_engine.connect() as connection:
            return list((await connection.execute(statement, parameters)).all())

    async def seed_session_for_test(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        session_id: str,
        mode: str = "chat",
        title: str | None = None,
        last_activity_at: datetime | None = None,
    ) -> None:
        await self._execute_for_test(
            text(
                "INSERT INTO conversation_sessions "
                "(session_id, tenant_id, owner_id, mode, title, last_activity_at) "
                "VALUES (:session_id, :tenant_id, :owner_id, :mode, :title, "
                "COALESCE(:last_activity_at, now()))"
            ),
            {
                "session_id": session_id,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "mode": mode,
                "title": title,
                "last_activity_at": last_activity_at,
            },
        )

    async def seed_task_for_test(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        objective_preview: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        await self._execute_for_test(
            text(
                "INSERT INTO task_runs (task_id, tenant_id, owner_id, thread_id, "
                "graph_version, input_ref, input_fingerprint, "
                "submission_dedup_key, run_semantics_snapshot, "
                "run_semantics_revision, submitted_policy_revision, "
                "submitted_policy_fingerprint, submitted_authorization_envelope, "
                "submitted_principal_scopes, status, created_at) "
                "VALUES (:task_id, :tenant_id, :owner_id, :thread_id, 'v1', "
                "'art_1', 'fp', :task_id, '{}'::jsonb, 'rev', 'rev', 'fp', "
                "'{}'::jsonb, '[]'::jsonb, 'queued', COALESCE(:created_at, now()))"
            ),
            {
                "task_id": task_id,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "thread_id": f"thread_{task_id}",
                "created_at": created_at,
            },
        )
        # `objective_preview` separately, so a NULL one is expressible.
        if objective_preview is not None:
            await self._execute_for_test(
                text(
                    "UPDATE task_runs SET objective_preview = :preview "
                    "WHERE task_id = :task_id"
                ),
                {"preview": objective_preview, "task_id": task_id},
            )

    async def seed_knowledge_base_for_test(
        self, *, tenant_id: str, owner_id: str, knowledge_base_id: str, name: str
    ) -> None:
        await self._execute_for_test(
            text(
                "INSERT INTO knowledge_bases "
                "(knowledge_base_id, tenant_id, owner_id, name) "
                "VALUES (:knowledge_base_id, :tenant_id, :owner_id, :name)"
            ),
            {
                "knowledge_base_id": knowledge_base_id,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "name": name,
            },
        )

    async def session_ids_for_test(self, *, tenant_id: str) -> list[str]:
        rows = await self._fetch_for_test(
            text(
                "SELECT session_id FROM conversation_sessions "
                "WHERE tenant_id = :tenant_id ORDER BY session_id"
            ),
            {"tenant_id": tenant_id},
        )
        return [row[0] for row in rows]

    async def knowledge_base_exists_for_test(
        self, *, tenant_id: str, knowledge_base_id: str
    ) -> bool:
        rows = await self._fetch_for_test(
            text(
                "SELECT 1 FROM knowledge_bases WHERE tenant_id = :tenant_id "
                "AND knowledge_base_id = :knowledge_base_id"
            ),
            {"tenant_id": tenant_id, "knowledge_base_id": knowledge_base_id},
        )
        return bool(rows)


@asynccontextmanager
async def _memory_projects() -> AsyncIterator[Any]:
    yield _SeededMemoryProjectStore()


def _postgres_projects(dsn: str) -> Callable[[], Any]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE projects, project_knowledge_bases, task_runs, "
                        "knowledge_bases, conversation_sessions CASCADE"
                    )
                )
            yield _SeededPostgresProjectStore(engine)
        finally:
            await engine.dispose()

    return factory


@pytest.fixture(params=["memory", "postgres"])
def projects(request: pytest.FixtureRequest) -> StoreHarness:
    """One membership contract, both stores."""

    if request.param == "memory":
        return StoreHarness(name="memory", factory=_memory_projects)

    dsn = _test_dsn()
    if dsn is None:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return StoreHarness(name="postgres", factory=_postgres_projects(dsn))
