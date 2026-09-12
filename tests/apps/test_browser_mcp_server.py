"""The six tools over the protocol, driven against a stand-in browser.

Nothing here launches Chromium. What is under test is the surface -- what the
tools accept, what they refuse before anything reaches a page, and how a
refused *destination* is told apart from a page that merely misbehaved.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from agent_workbench.apps.browser_mcp.contract import Action
from agent_workbench.apps.browser_mcp.proxy import (
    GuardedProxy,
    LocalDecisions,
    RemoteDecisions,
    parse_allowed,
)
from agent_workbench.apps.browser_mcp.server import (
    DIAGNOSTICS_TOOL,
    EVAL_TOOL,
    INTERACT_TOOL,
    OPEN_TOOL,
    SNAPSHOT_TOOL,
    TOOL_NAMES,
    create_app,
    create_server,
)
from agent_workbench.apps.browser_mcp.session import LogEntry, OpenOutcome


@dataclass
class _StubSession:
    """A browser that records what it was asked to do."""

    opened: list[str] = field(default_factory=list)
    evaluated: list[str] = field(default_factory=list)
    performed: list[Action] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    frame: bytes | None = None
    tree: str = '[ref_1] RootWebArea: "demo"'

    workspace_root: str = "/workspace"

    def workspace_url(self, relative: str) -> str:
        return f"file://{self.workspace_root}/{relative}"

    async def open(self, target: str, timeout_ms: int) -> OpenOutcome:
        del timeout_ms
        self.opened.append(target)
        return OpenOutcome(url=target, title="demo", status=200, console_errors=0)

    async def snapshot(self, max_chars: int) -> str:
        return self.tree[:max_chars]

    async def evaluate(self, expression: str, timeout_ms: int) -> Any:
        del timeout_ms
        self.evaluated.append(expression)
        return {"apexHeight": 76.5, "framesToLand": 34}

    async def interact(self, actions: tuple[Action, ...], timeout_ms: int) -> list[str]:
        del timeout_ms
        self.performed.extend(actions)
        return [f"action {index} ({a.kind}) ok" for index, a in enumerate(actions)]

    async def screenshot(self, *, full_page: bool, quality: int) -> bytes:
        del full_page, quality
        return b"\xff\xd8\xff-not-really-a-jpeg"

    def drain_logs(self, limit: int) -> tuple[tuple[LogEntry, ...], int]:
        taken = tuple(self.logs)[-limit:]
        self.logs.clear()
        return taken, 0

    def latest_frame(self) -> bytes | None:
        return self.frame

    async def aclose(self) -> None:
        return None


def _call(
    session: _StubSession,
    name: str,
    arguments: dict[str, Any],
    proxy: GuardedProxy | None = None,
) -> Any:
    async def scenario() -> Any:
        async with Client(
            create_server(session, LocalDecisions(proxy or GuardedProxy())),
            cache=None,
            raise_exceptions=True,
        ) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(scenario())


def _text(result: Any) -> str:
    return "".join(block.text for block in result.content if hasattr(block, "text"))


# -- the surface ------------------------------------------------------------


def test_the_server_declares_exactly_the_six_tools() -> None:
    async def scenario() -> Any:
        async with Client(
            create_server(_StubSession(), LocalDecisions(GuardedProxy())),
            cache=None,
            raise_exceptions=True,
        ) as client:
            return await client.list_tools()

    names = tuple(tool.name for tool in asyncio.run(scenario()).tools)
    assert names == TOOL_NAMES


def test_interaction_is_not_declared_replayable() -> None:
    """A graph node replay must not click a button a second time (ADR-0112 §3.4)."""

    async def scenario() -> Any:
        async with Client(
            create_server(_StubSession(), LocalDecisions(GuardedProxy())),
            cache=None,
            raise_exceptions=True,
        ) as client:
            return await client.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(scenario()).tools}
    assert tools[INTERACT_TOOL].annotations.idempotent_hint is False
    assert tools[EVAL_TOOL].annotations.idempotent_hint is False
    assert tools[SNAPSHOT_TOOL].annotations.read_only_hint is True


# -- opening ----------------------------------------------------------------


def test_a_workspace_path_becomes_a_file_url_under_the_mount() -> None:
    session = _StubSession()
    _call(session, OPEN_TOOL, {"workspace_path": "out/mario.html"})
    assert session.opened == ["file:///workspace/out/mario.html"]


def test_a_workspace_path_cannot_climb_out_of_the_mount() -> None:
    session = _StubSession()
    result = _call(session, OPEN_TOOL, {"workspace_path": "../../etc/passwd"})
    assert result.is_error
    assert "must be relative" in _text(result)
    assert session.opened == []


def test_naming_both_a_url_and_a_path_is_refused() -> None:
    session = _StubSession()
    result = _call(
        session, OPEN_TOOL, {"url": "https://a.example", "workspace_path": "b"}
    )
    assert result.is_error
    assert session.opened == []


def test_an_unknown_field_is_refused_before_the_browser_sees_it() -> None:
    session = _StubSession()
    result = _call(session, OPEN_TOOL, {"url": "https://a.example", "headless": False})
    assert result.is_error
    assert "unknown field" in _text(result)
    assert session.opened == []


# -- the tool that answers "is it correct" ----------------------------------


def test_eval_returns_the_value_as_json() -> None:
    session = _StubSession()
    result = _call(session, EVAL_TOOL, {"expression": "return window.__state"})
    assert '"apexHeight": 76.5' in _text(result) or '"apexHeight":76.5' in _text(result)
    assert session.evaluated == ["return window.__state"]


def test_an_empty_expression_is_refused() -> None:
    session = _StubSession()
    result = _call(session, EVAL_TOOL, {"expression": "   "})
    assert result.is_error
    assert session.evaluated == []


def test_a_batch_beyond_the_ceiling_is_refused_whole() -> None:
    session = _StubSession()
    result = _call(
        session,
        INTERACT_TOOL,
        {"actions": [{"kind": "key", "text": "Tab"} for _ in range(33)]},
    )
    assert result.is_error
    assert session.performed == []


def test_a_click_without_a_ref_is_refused_with_the_index() -> None:
    session = _StubSession()
    result = _call(
        session,
        INTERACT_TOOL,
        {"actions": [{"kind": "key", "text": "Tab"}, {"kind": "click"}]},
    )
    assert result.is_error
    assert "action 1" in _text(result)


# -- diagnostics separate two different kinds of failure --------------------


def test_a_refused_destination_is_reported_apart_from_console_noise() -> None:
    """The model must tell "this deployment said no" from "the page is buggy"."""

    session = _StubSession(logs=[LogEntry(at=0.0, level="error", text="TypeError: x")])
    proxy = GuardedProxy(allowed=parse_allowed([]))
    proxy._record("GET", "169.254.169.254", 80, False, "not publicly routable")

    body = _text(_call(session, DIAGNOSTICS_TOOL, {}, proxy))
    assert "TypeError: x" in body
    assert "Destinations refused by the guard" in body
    assert "169.254.169.254" in body


def test_a_quiet_page_says_so_rather_than_returning_nothing() -> None:
    body = _text(_call(_StubSession(), DIAGNOSTICS_TOOL, {}))
    assert "nothing reported" in body


# -- the console's frame route ---------------------------------------------


def test_the_frame_route_is_empty_until_there_is_a_frame() -> None:
    session = _StubSession(frame=None)
    with TestClient(create_app(session, LocalDecisions(GuardedProxy()))) as client:
        assert client.get("/frame").status_code == 204


def test_the_frame_route_serves_the_latest_frame_uncached() -> None:
    session = _StubSession(frame=b"\xff\xd8jpeg-bytes")
    with TestClient(create_app(session, LocalDecisions(GuardedProxy()))) as client:
        response = client.get("/frame")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b"\xff\xd8jpeg-bytes"


def test_health_names_the_tools_it_serves() -> None:
    with TestClient(
        create_app(_StubSession(), LocalDecisions(GuardedProxy()))
    ) as client:
        payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert tuple(payload["tools"]) == TOOL_NAMES


@pytest.mark.parametrize("tool", TOOL_NAMES)
def test_every_declared_tool_answers(tool: str) -> None:
    """No name in the catalogue that dispatch does not handle."""

    arguments: dict[str, dict[str, Any]] = {
        OPEN_TOOL: {"url": "https://a.example"},
        EVAL_TOOL: {"expression": "return 1"},
        INTERACT_TOOL: {"actions": [{"kind": "key", "text": "Tab"}]},
    }
    result = _call(_StubSession(), tool, arguments.get(tool, {}))
    assert not result.is_error


def test_an_unreachable_guard_is_not_reported_as_nothing_refused() -> None:
    """ADR-0112 §3.3: the two answers mean opposite things to the model.

    Under Compose the guard is another container, so this read can fail on its
    own. Flattening that into an empty list would tell a model its page is fine
    when in fact nobody checked.
    """

    async def scenario() -> Any:
        async with Client(
            create_server(
                _StubSession(),
                # A port nothing listens on: the same shape as the egress
                # container being down.
                RemoteDecisions(url="http://127.0.0.1:1/decisions"),
            ),
            cache=None,
            raise_exceptions=True,
        ) as client:
            return await client.call_tool(DIAGNOSTICS_TOOL, {})

    body = _text(asyncio.run(scenario()))
    assert "could not be reached" in body
    assert "does not say whether any request was refused" in body
