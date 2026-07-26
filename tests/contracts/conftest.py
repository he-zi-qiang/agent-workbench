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

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from harness import StoreHarness
from sqlalchemy import text

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.memory import (
    InMemoryArtifactStore,
    InMemoryConversationStore,
)
from agent_workbench.adapters.persistence import (
    PostgresConversationStore,
    create_query_engine,
)
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.conversation_store import ConversationStore

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
