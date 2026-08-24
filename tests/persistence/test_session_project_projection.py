"""A session's project membership has to survive every projection that names it.

Three of them do, and each was written by hand: `list_sessions`, `session`, and
`set_title_if_unset`'s `RETURNING`. All three omitted `project_id` at some
point, and none of them raised when they did -- a pydantic field with a default
absorbs a missing column and answers with the default, confidently.


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

    assert _run(scenario) == (PROJECT,)


def test_a_filed_session_still_says_so_when_one_turn_reads_it() -> None:
    """The other projection, and the one that decided which tools a turn got.

    This file used to carry a sentence saying the single-session read "selects
    the whole row and was right all along". There is no such method: the read is
    `ConversationStore.session`, it projects seven columns by hand exactly the
    way `list_sessions` did, and `project_id` was not among them either. The
    sentence was the reason nobody looked.

    What it cost is larger than the sidebar label the listing bug cost.
    `code_session.py::_project_files_for` reads this method to decide which file
    language a turn speaks, so every Code turn was told its session belonged to
    no project, fell back to the flat workspace, and was handed `CODE_TOOLS`.
    The project tools of ADR-072/073/074 -- and `project_grep`/`project_run`
    from ADR-077 after them -- had never once been offered to a model through
    the console. Nothing failed: the console listed the directory, the file tree
    rendered, the turn ran, and the model answered about a versioned workspace
    it was not in. Found 2026-08-24 by reading `RunStarted.tool_names` on a
    session whose project was attached.
    """

    async def scenario(
        conversations: PostgresConversationStore, projects: PostgresProjectStore
    ) -> str | None:
        await conversations.create_session(
            session_id=SESSION, tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        await projects.create(_project())
        assert await projects.assign_session(
            tenant_id=TENANT, owner_id=OWNER, session_id=SESSION, project_id=PROJECT
        )
        session = await conversations.session(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER, mode="code"
        )
        return session.project_id

    assert _run(scenario) == PROJECT


def test_an_unfiled_session_reads_as_unfiled() -> None:
    # The control for the read, for the reason the listing has one: without it a
    # projection that hardcoded the column would pass.
    async def scenario(
        conversations: PostgresConversationStore, _projects: PostgresProjectStore
    ) -> str | None:
        await conversations.create_session(
            session_id=SESSION, tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        session = await conversations.session(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER, mode="code"
        )
        return session.project_id

    assert _run(scenario) is None


def test_renaming_a_session_does_not_unfile_it() -> None:
    """The third projection, and the one that hands its result to a caller.

    `rename_session` builds a `ConversationSession` from its own `RETURNING`
    list, and that list omitted `project_id` as well -- so renaming a coding
    session answered with a session that belonged to no project. Three
    instances of one mistake in one file, which is what a hand-written
    projection beside a model with defaults produces.
    """

    async def scenario(
        conversations: PostgresConversationStore, projects: PostgresProjectStore
    ) -> str | None:
        await conversations.create_session(
            session_id=SESSION, tenant_id=TENANT, owner_id=OWNER, mode="code"
        )
        await projects.create(_project())
        assert await projects.assign_session(
            tenant_id=TENANT, owner_id=OWNER, session_id=SESSION, project_id=PROJECT
        )
        renamed = await conversations.rename_session(
            session_id=SESSION, tenant_id=TENANT, principal_id=OWNER, title="复盘"
        )
        return renamed.project_id

    assert _run(scenario) == PROJECT


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
