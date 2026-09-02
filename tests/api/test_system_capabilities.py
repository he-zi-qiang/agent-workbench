"""``GET /v1/system/capabilities``: the report a broken deployment is read from.

No database and no model. Every fact this route returns was already decided at
assembly, so the harness is the router plus a stand-in dependency object -- which
is also why these run in CI while most of ``tests/api`` skips itself.

The envelope rows are built from the *real* `task_authorization_envelope`
rather than a hand-written tuple. That is the whole point of the second test:
the MCP row is defined as "whatever is in the envelope that is not a built-in",
so it stays exact only while the built-in set is maintained, and a test written
against a hand-made envelope would pass on the day somebody adds a built-in and
the console starts calling it an MCP tool.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agent_workbench.apps.api.routes import system as system_route
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.bootstrap.projections import task_authorization_envelope
from agent_workbench.domain.policies import PrincipalContext

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
PRINCIPAL = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")


class _StubPrincipals:
    def resolve(self, request: object) -> object:
        del request
        return PRINCIPAL


def _dependencies(
    *,
    serves_chat: bool = False,
    chat_unavailable: str | None = "no provider key is configured",
    rag_unavailable: str | None = "no embedding runtime is installed",
    serves_search: bool = False,
    serves_code: bool = False,
    triage: object | None = None,
    research: object | None = None,
    envelope: Any = None,
) -> SimpleNamespace:
    """A deployment, shaped like the one that prompted this route.

    The defaults are the Compose stack a Windows user gets by double-clicking
    `scripts\\stack.cmd`: Direct Chat once a key is saved, nothing else -- no
    embedding runtime, no research provider, no MCP server, no sandbox.
    """

    return SimpleNamespace(
        config=SimpleNamespace(
            task=SimpleNamespace(
                default_authorization_envelope=(
                    envelope
                    if envelope is not None
                    else task_authorization_envelope(external_search=False)
                )
            ),
            research=research,
            code=SimpleNamespace(enabled=False),
        ),
        serves_chat=serves_chat,
        chat_unavailable=chat_unavailable,
        rag_unavailable=rag_unavailable,
        serves_search=serves_search,
        serves_code=serves_code,
        triage=triage,
        principals=_StubPrincipals(),
    )


def _report(dependencies: SimpleNamespace) -> dict[str, dict[str, Any]]:
    app = FastAPI()
    app.include_router(system_route.router)
    setattr(app.state, STATE_ATTRIBUTE, dependencies)

    async def execute() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await client.get("/v1/system/capabilities", headers=HEADERS)

    response = asyncio.run(execute())
    assert response.status_code == 200, response.text
    rows = response.json()["capabilities"]
    return {row["id"]: row for row in rows}


def test_the_default_compose_stack_names_every_thing_it_cannot_do() -> None:
    """The four absences that made somebody suspect their provider key."""

    rows = _report(_dependencies())

    assert rows["chat.direct"]["state"] == "absent"
    # The deployment's own recorded sentence, not one this route invented.
    assert rows["chat.direct"]["reason"] == "no provider key is configured"
    assert rows["chat.web_search"]["state"] == "absent"
    # Names the half a provider key cannot buy -- see the test below for why
    # this is not simply `rag_unavailable` forwarded.
    assert "embedding extra" in rows["chat.knowledge_base"]["reason"]
    assert rows["task.mcp_tools"]["state"] == "absent"
    assert rows["task.mcp_tools"]["detail"] == []
    assert rows["task.external_search"]["state"] == "absent"
    assert rows["task.sandbox"]["state"] == "absent"
    # Submission is not the same question as execution, and the report keeps
    # them apart: this stack accepts Tasks it cannot promise anybody will run.
    assert rows["task.submit"]["state"] == "available"


def test_the_worker_is_unknown_rather_than_absent_or_present() -> None:
    """The API has no channel a Worker reports itself through, so it says so.

    `--demo` and real are the same to this process, which is exactly the
    confusion this row exists to stop somebody having: a console cannot see
    that its two Workers are synthetic.
    """

    rows = _report(_dependencies())

    assert rows["task.worker"]["state"] == "unknown"
    assert "--demo" in rows["task.worker"]["reason"]
    assert rows["task.worker"]["remedy"] != ""


def test_a_keyless_stack_is_not_told_its_index_is_missing_because_of_the_key() -> None:
    """The wrong cause is worse than no cause, and this one was measured.

    With no provider key the whole of Chat fails to assemble, and assembly
    records that *model* error into `rag_unavailable` as well. Forwarding it
    verbatim made a real Compose stack report "no API key" for a row whose true
    answer is "this image has no embedding runtime, and a key will not change
    it" -- which sends somebody to buy credit for a feature the image cannot
    run. Retrieval is asked about first because it is the half money cannot fix.
    """

    rows = _report(
        _dependencies(
            serves_chat=False,
            chat_unavailable="secrets.deepseek_api_key is not configured",
            rag_unavailable="secrets.deepseek_api_key is not configured",
            serves_search=False,
        )
    )

    reason = rows["chat.knowledge_base"]["reason"]
    assert "embedding extra" in reason
    assert "deepseek_api_key" not in reason


def test_a_stack_with_an_index_and_no_key_says_so_the_other_way_round() -> None:
    """The same row, the other half missing: retrieval is there, Chat is not."""

    rows = _report(
        _dependencies(
            serves_chat=False,
            chat_unavailable="secrets.deepseek_api_key is not configured",
            rag_unavailable="chat was not requested for this process",
            serves_search=True,
        )
    )

    reason = rows["chat.knowledge_base"]["reason"]
    assert "没有 Chat" in reason
    assert "deepseek_api_key" in reason, "the recorded sentence is still carried"


def test_the_mcp_row_is_exactly_the_names_that_are_not_built_in() -> None:
    """Pinned against a real envelope, so a new built-in fails here first."""

    envelope = task_authorization_envelope(
        external_search=True,
        mcp_tools=("word_render_document", "web_fetch_page"),
        sandbox=True,
        delegation=True,
    )
    rows = _report(_dependencies(envelope=envelope))

    assert rows["task.mcp_tools"]["state"] == "available"
    assert sorted(rows["task.mcp_tools"]["detail"]) == [
        "web_fetch_page",
        "word_render_document",
    ]
    assert rows["task.external_search"]["state"] == "available"
    assert rows["task.sandbox"]["state"] == "available"
    assert rows["task.delegation"]["state"] == "available"


def test_an_assembled_deployment_reports_no_reason_where_nothing_is_missing() -> None:
    """A row that is available says nothing, because there is nothing to say."""

    rows = _report(
        _dependencies(
            serves_chat=True,
            chat_unavailable=None,
            rag_unavailable=None,
            serves_search=True,
            serves_code=True,
            triage=object(),
            research=object(),
        )
    )

    for row_id in (
        "chat.direct",
        "chat.knowledge_base",
        "chat.web_search",
        "knowledge.search",
        "code.sessions",
        "task.triage",
    ):
        assert rows[row_id]["state"] == "available", row_id
        assert rows[row_id]["reason"] == "", row_id
        assert rows[row_id]["remedy"] == "", row_id


@pytest.mark.parametrize("row_id", ["chat.direct", "chat.web_search", "task.mcp_tools"])
def test_every_absence_carries_something_to_do_about_it(row_id: str) -> None:
    """A report that says "missing" and stops is the thing this replaces."""

    rows = _report(_dependencies())

    assert rows[row_id]["state"] == "absent"
    assert rows[row_id]["remedy"] != ""


def test_the_rows_are_a_stable_set_with_a_tier_each() -> None:
    """Ids are what a console branches on; tiers are what a reader sorts by."""

    rows = _report(_dependencies())

    assert set(rows) == {
        "chat.direct",
        "chat.knowledge_base",
        "chat.web_search",
        "knowledge.search",
        "code.sessions",
        "task.submit",
        "task.worker",
        "task.external_search",
        "task.mcp_tools",
        "task.sandbox",
        "task.delegation",
        "task.triage",
    }
    assert {row["tier"] for row in rows.values()} == {"core", "optional"}
    # The five the product claims to be, separated from the seven it can also
    # be asked to do. A row moving between these two is a product decision and
    # should have to edit this list to make it.
    assert {row_id for row_id, row in rows.items() if row["tier"] == "core"} == {
        "chat.direct",
        "chat.knowledge_base",
        "knowledge.search",
        "task.submit",
        "task.worker",
    }
