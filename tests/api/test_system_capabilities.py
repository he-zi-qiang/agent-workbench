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
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from agent_workbench.application.provider_key import ProviderKeyStore
from agent_workbench.application.switches import SWITCHES, SwitchStore
from agent_workbench.apps.api.routes import system as system_route
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.bootstrap.projections import (
    SwitchState,
    task_authorization_envelope,
)
from agent_workbench.domain.policies import PrincipalContext

HEADERS = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
PRINCIPAL = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")


class _StubPrincipals:
    def resolve(self, request: object) -> object:
        del request
        return PRINCIPAL


def _switch_states(**overrides: SwitchState) -> tuple[SwitchState, ...]:
    """Every switch off and undecided, unless a test says otherwise."""

    return tuple(
        overrides.get(
            spec.path,
            SwitchState(path=spec.path, active=False, stored_at_start=None, held=""),
        )
        for spec in SWITCHES
    )


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
    switches: tuple[SwitchState, ...] | None = None,
    switches_path: Path | None = None,
    active_key: str | None = None,
    key_file: Path | None = None,
) -> SimpleNamespace:
    """A deployment, shaped like the one that prompted this route.

    The defaults are the Compose stack a Windows user gets by double-clicking
    `scripts\\stack.cmd`: Direct Chat once a key is saved, nothing else -- no
    embedding runtime, no research provider, no MCP server, no sandbox, and
    nothing stored on any switch.
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
            model=SimpleNamespace(
                api_key=SecretStr(active_key) if active_key is not None else None
            ),
            switches=switches if switches is not None else _switch_states(),
        ),
        serves_chat=serves_chat,
        chat_unavailable=chat_unavailable,
        rag_unavailable=rag_unavailable,
        serves_search=serves_search,
        serves_code=serves_code,
        triage=triage,
        principals=_StubPrincipals(),
        switches=SwitchStore(path=switches_path, checkout_root=None),
        provider_keys=ProviderKeyStore(key_file=key_file, checkout_root=None),
    )


def _request(
    dependencies: SimpleNamespace,
    method: str = "GET",
    path: str = "/v1/system/capabilities",
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    app = FastAPI()
    app.include_router(system_route.router)
    setattr(app.state, STATE_ATTRIBUTE, dependencies)

    async def execute() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test"
        ) as client:
            return await client.request(method, path, headers=HEADERS, json=json_body)

    return asyncio.run(execute())


def _rows(response: httpx.Response) -> dict[str, dict[str, Any]]:
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()["capabilities"]}


def _report(dependencies: SimpleNamespace) -> dict[str, dict[str, Any]]:
    return _rows(_request(dependencies))


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


# --- ADR-103: switches -----------------------------------------------------


def test_every_row_says_how_it_is_provided() -> None:
    """Switch-shaped parts get a switch; the rest say what they need instead.

    The distinction is the whole design: a switch on a part that needs a
    server or another image would be a promise nothing on this page can keep.
    """

    rows = _report(_dependencies())

    provision = {row_id: row["provision"] for row_id, row in rows.items()}
    assert provision == {
        "chat.direct": "key",
        "chat.knowledge_base": "install",
        "knowledge.search": "install",
        "task.submit": "none",
        "task.worker": "none",
        "chat.web_search": "switch",
        "code.sessions": "switch",
        "task.external_search": "switch",
        "task.mcp_tools": "install",
        "task.sandbox": "install",
        "task.delegation": "switch",
        "task.triage": "switch",
    }
    # One setting, two rows: the switch object is the same on both.
    assert rows["chat.web_search"]["switch"]["id"] == "research.enabled"
    assert rows["task.external_search"]["switch"] == rows["chat.web_search"]["switch"]
    assert rows["code.sessions"]["switch"]["id"] == "code.enabled"
    assert rows["task.delegation"]["switch"]["id"] == "multi_agent.delegation_enabled"
    assert rows["task.triage"]["switch"]["id"] == "triage.enabled"
    assert all(rows[r]["switch"] is None for r, p in provision.items() if p != "switch")


def test_a_fresh_stack_has_every_switch_undecided_and_nothing_owed() -> None:
    rows = _report(_dependencies())

    for row_id in (
        "chat.web_search",
        "code.sessions",
        "task.delegation",
        "task.triage",
    ):
        switch = rows[row_id]["switch"]
        assert switch["stored"] is None
        assert switch["active"] is False
        assert switch["restart_required"] is False
        assert switch["restart_hint"] == ""
        assert switch["overridden"] is False
        assert switch["held"] == ""
    # No key anywhere, so the three that need a model say so up front; the
    # one that does not, does not.
    assert "模型密钥" in rows["chat.web_search"]["switch"]["blocked"]
    assert "模型密钥" in rows["task.triage"]["switch"]["blocked"]
    assert rows["task.delegation"]["switch"]["blocked"] == ""


def test_storing_a_switch_writes_the_file_and_says_a_restart_is_owed(
    tmp_path: Path,
) -> None:
    """The two halves this route exists to keep apart, in one response."""

    target = tmp_path / "switches.json"
    response = _request(
        _dependencies(switches_path=target),
        "PUT",
        "/v1/system/switches/multi_agent.delegation_enabled",
        {"enabled": True},
    )
    rows = _rows(response)

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "multi_agent.delegation_enabled": True
    }
    switch = rows["task.delegation"]["switch"]
    assert switch["stored"] is True
    # Nothing in this process changed, and the page has to say so rather than
    # report a success that changes nothing visible.
    assert switch["active"] is False
    assert switch["restart_required"] is True
    assert "重启" in switch["restart_hint"]
    assert rows["task.delegation"]["state"] == "absent"


def test_withdrawing_a_switch_returns_it_to_undecided(tmp_path: Path) -> None:
    target = tmp_path / "switches.json"
    deps = _dependencies(switches_path=target)
    _request(deps, "PUT", "/v1/system/switches/triage.enabled", {"enabled": False})
    rows = _rows(_request(deps, "DELETE", "/v1/system/switches/triage.enabled"))

    assert rows["task.triage"]["switch"]["stored"] is None
    assert rows["task.triage"]["switch"]["restart_required"] is False
    assert json.loads(target.read_text(encoding="utf-8")) == {}


def test_a_switch_the_process_started_with_is_not_owed_again(tmp_path: Path) -> None:
    """Stored equals what was read at start: no restart is owed for that."""

    target = tmp_path / "switches.json"
    target.write_text(json.dumps({"triage.enabled": True}), encoding="utf-8")
    deps = _dependencies(
        switches_path=target,
        switches=_switch_states(
            **{
                "triage.enabled": SwitchState(
                    path="triage.enabled", active=True, stored_at_start=True, held=""
                )
            }
        ),
        triage=object(),
    )
    rows = _report(deps)

    switch = rows["task.triage"]["switch"]
    assert switch["stored"] is True
    assert switch["active"] is True
    assert switch["restart_required"] is False
    assert rows["task.triage"]["state"] == "available"


def test_a_held_switch_is_the_rows_reason(tmp_path: Path) -> None:
    """Research stored on, no key at start: the row says that, not "no [research]"."""

    target = tmp_path / "switches.json"
    target.write_text(json.dumps({"research.enabled": True}), encoding="utf-8")
    held = "这次启动没有可用的 Provider Key，所以这个开关被搁置"
    deps = _dependencies(
        serves_chat=True,
        chat_unavailable=None,
        switches_path=target,
        switches=_switch_states(
            **{
                "research.enabled": SwitchState(
                    path="research.enabled",
                    active=False,
                    stored_at_start=True,
                    held=held,
                )
            }
        ),
    )
    rows = _report(deps)

    assert rows["chat.web_search"]["state"] == "absent"
    assert rows["chat.web_search"]["reason"] == held
    assert rows["task.external_search"]["reason"] == held
    switch = rows["chat.web_search"]["switch"]
    assert switch["held"] == held
    assert switch["overridden"] is False
    assert switch["restart_required"] is False


def test_an_environment_that_beat_the_file_reads_as_overridden(tmp_path: Path) -> None:
    target = tmp_path / "switches.json"
    target.write_text(
        json.dumps({"multi_agent.delegation_enabled": True}), encoding="utf-8"
    )
    deps = _dependencies(
        switches_path=target,
        switches=_switch_states(
            **{
                "multi_agent.delegation_enabled": SwitchState(
                    path="multi_agent.delegation_enabled",
                    active=False,
                    stored_at_start=True,
                    held="",
                )
            }
        ),
    )
    switch = _report(deps)["task.delegation"]["switch"]

    assert switch["overridden"] is True
    assert switch["restart_required"] is False


def test_a_key_in_either_place_clears_the_needs_model_note(tmp_path: Path) -> None:
    active = _report(_dependencies(active_key="example-not-a-credential-0001"))
    assert active["task.triage"]["switch"]["blocked"] == ""

    key_file = tmp_path / "key"
    key_file.write_text("example-not-a-credential-0002\n", encoding="utf-8")
    stored = _report(_dependencies(key_file=key_file))
    assert stored["task.triage"]["switch"]["blocked"] == ""


def test_an_unknown_switch_is_404_and_a_refusal_is_400(tmp_path: Path) -> None:
    deps = _dependencies(switches_path=tmp_path / "switches.json")
    missing = _request(
        deps, "PUT", "/v1/system/switches/policy.shell_tools_enabled", {"enabled": True}
    )
    assert missing.status_code == 404
    assert "没有叫" in missing.json()["detail"]

    nowhere = _request(
        _dependencies(switches_path=None),
        "PUT",
        "/v1/system/switches/triage.enabled",
        {"enabled": True},
    )
    assert nowhere.status_code == 400
    assert "AW_KEY_FILE" in nowhere.json()["detail"]

    extra = _request(
        deps, "PUT", "/v1/system/switches/triage.enabled", {"enabled": True, "x": 1}
    )
    assert extra.status_code == 422


def test_a_file_the_store_cannot_read_does_not_take_the_page_down(
    tmp_path: Path,
) -> None:
    """The page exists to explain this deployment; it must survive its own file."""

    target = tmp_path / "switches.json"
    target.write_text("{not json", encoding="utf-8")
    rows = _report(_dependencies(switches_path=target))

    switch = rows["task.delegation"]["switch"]
    assert switch["stored"] is None
    assert switch["restart_required"] is False
    assert "读不到已存的开关" in switch["blocked"]
