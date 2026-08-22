"""A session's project membership has to survive the *list*, not only the row.

Written after the console showed 不属于任何项目 for a coding session PostgreSQL
had filed correctly. `list_sessions` projects its columns by hand, and
`project_id` was not among the seven -- so `ConversationSession.model_validate`
filled the field from its default. Nothing raised: the list answered "no
project" with exactly the confidence it answers everything else, and the sidebar
believed it.

Here rather than in `tests/contracts` because the state needs two stores over
one engine: `ProjectStore.assign_session` files it and `ConversationStore.list_sessions`
reads it back. The in-memory conversation store copies a frozen model and cannot
reproduce a hand-written SQL projection, so a contract test parameterised over
both would have passed against the bug in the implementation that had it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence.conversation_store import (
    PostgresConversationStore,
)
from agent_workbench.adapters.persistence.engine import create_query_engine
from agent_workbench.adapters.persistence.projects import PostgresProjectStore
from agent_workbench.ports.projects import ProjectRecord

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TENANT = "tenant_a"
OWNER = "user_1"
PROJECT = "prj_review"
SESSION = "ses_code_1"


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(
    scenario: Callable[
        [PostgresConversationStore, PostgresProjectStore], Awaitable[Any]
    ],
) -> Any:
    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("TRUNCATE projects, conversation_sessions CASCADE")
                )
            return await scenario(
                PostgresConversationStore(engine), PostgresProjectStore(engine)
            )
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _project() -> ProjectRecord:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    return ProjectRecord(
        project_id=PROJECT,
        tenant_id=TENANT,
        owner_id=OWNER,
        name="季度复盘",
        created_at=now,
        updated_at=now,
    )


def test_a_filed_session_still_says_so_when_it_is_listed() -> None:
    async def scenario(
        conversations: PostgresConversationStore, projects: PostgresProjectStore
    ) -> tuple[str | None, ...]:
        await conversations.create_session(
            session_id=SESSION, tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        await projects.create(_project())
        assert await projects.assign_session(
            tenant_id=TENANT, owner_id=OWNER, session_id=SESSION, project_id=PROJECT
        )
        listed = await conversations.list_sessions(
            tenant_id=TENANT, principal_id=OWNER, mode="code"
        )
        return tuple(session.project_id for session in listed)

    # Through the *listing*. `get_session` selects the whole row and was right
    # all along, so a test that read the row back would have passed against the
    # bug this exists for.
    assert _run(scenario) == (PROJECT,)


def test_an_unfiled_session_lists_as_unfiled() -> None:
    async def scenario(
        conversations: PostgresConversationStore, _projects: PostgresProjectStore
    ) -> tuple[str | None, ...]:
        await conversations.create_session(
            session_id=SESSION, tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        listed = await conversations.list_sessions(
            tenant_id=TENANT, principal_id=OWNER, mode="code"
        )
        return tuple(session.project_id for session in listed)

    # The control. Without it the fix above would also pass for a projection
    # that hardcoded the column to a constant -- and NULL is the normal state
    # here (ADR-071 §5.3), so it has to be reported as itself.
    assert _run(scenario) == (None,)
