"""`browser_open` for a project turn resolves `workspace_path` at home (ADR-0115 §3.3).

A fake store rather than the filesystem one, deliberately: what is under test
is the rewrite and the two refusals, and the filesystem store's own path
judgement is pinned in its own file. The fake answers the two questions the
wrapper asks -- where is the root, is this path there -- and refuses a `..`
the way the real one does.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_workbench.adapters.tools.browser import (
    OPEN_TOOL_LOCAL_NAME,
    open_within_project,
)
from agent_workbench.application.project_file_scope import ProjectFileScope
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.project_files import ProjectPathError
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolBinding, ToolInvocation


@dataclass
class _FakeStore:
    """`working_directory` is a real absolute path so `as_uri()` has a drive
    to name on Windows; `/projects/demo` is absolute only on POSIX."""

    root: Path
    present: frozenset[str]

    @property
    def working_directory(self) -> Path:
        return self.root

    async def exists(self, path: str) -> bool:
        if path.startswith("/") or ".." in path.split("/"):
            raise ProjectPathError(
                f"a project path may not climb out of the root: {path!r}"
            )
        return path in self.present


@dataclass
class _Recording:
    """The discovered handler, standing in for the browser server."""

    seen: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, invocation: ToolInvocation) -> ToolResult:
        self.seen.append(dict(invocation.call.arguments))
        return ToolResult.succeeded(invocation.call, content="opened")


def _binding(name: str, handler: Any) -> ToolBinding:
    return ToolBinding(
        spec=ToolSpec(
            name=name,
            description="a discovered browser tool",
            input_schema={"type": "object"},
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=5,
        ),
        handler=handler,
    )


def _invocation(name: str, **arguments: object) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(
            tool_call_id="toolu_" + "0" * 20,
            tool_name=name,
            arguments=dict(arguments),
        ),
        context=ExecutionContext(
            principal=PrincipalContext(tenant_id="tenant_a", principal_id="user_owner"),
            envelope=AuthorizationEnvelope(),
            agent_run_id="run_" + "0" * 28,
            policy_identity="test",
        ),
        cancellation=NullCancellationToken(),
        timeout_seconds=5,
    )


def _open(
    wrapped: ToolBinding, scope: ProjectFileScope, store: Any, **arguments: object
) -> ToolResult:
    async def scenario() -> ToolResult:
        if store is None:
            return await wrapped.handler(_invocation(OPEN_TOOL_LOCAL_NAME, **arguments))
        with scope.using(store):
            return await wrapped.handler(_invocation(OPEN_TOOL_LOCAL_NAME, **arguments))

    return asyncio.run(scenario())


def test_only_the_open_tool_is_wrapped() -> None:
    other = _binding("mcp_browser_browser_snapshot", _Recording())

    assert open_within_project(other, ProjectFileScope()) is other


def test_a_project_turns_relative_path_becomes_a_file_url_under_its_root(
    tmp_path: Path,
) -> None:
    recording = _Recording()
    scope = ProjectFileScope()
    wrapped = open_within_project(_binding(OPEN_TOOL_LOCAL_NAME, recording), scope)

    result = _open(
        wrapped,
        scope,
        _FakeStore(tmp_path, frozenset({"mario.html"})),
        workspace_path="mario.html",
        timeout_ms=5000,
    )

    assert result.error is None
    assert recording.seen == [
        {"url": (tmp_path / "mario.html").as_uri(), "timeout_ms": 5000}
    ]


def test_a_flat_workspace_turn_passes_the_path_through_untouched() -> None:
    """No project entered: the server resolves it against its own root, the
    way it did before this wrapper existed."""

    recording = _Recording()
    scope = ProjectFileScope()
    wrapped = open_within_project(_binding(OPEN_TOOL_LOCAL_NAME, recording), scope)

    _open(wrapped, scope, None, workspace_path="page.html")

    assert recording.seen == [{"workspace_path": "page.html"}]


def test_a_url_is_never_rewritten() -> None:
    recording = _Recording()
    scope = ProjectFileScope()
    wrapped = open_within_project(_binding(OPEN_TOOL_LOCAL_NAME, recording), scope)

    _open(
        wrapped, scope, _FakeStore(Path.cwd(), frozenset()), url="https://example.com"
    )

    assert recording.seen == [{"url": "https://example.com"}]


def test_a_path_that_climbs_out_is_refused_by_the_stores_own_rule() -> None:
    recording = _Recording()
    scope = ProjectFileScope()
    wrapped = open_within_project(_binding(OPEN_TOOL_LOCAL_NAME, recording), scope)

    result = _open(
        wrapped,
        scope,
        _FakeStore(Path.cwd(), frozenset()),
        workspace_path="../secret.html",
    )

    assert result.error is not None
    assert result.error.code == "invalid_tool_input"
    assert recording.seen == []


def test_a_file_that_is_not_there_is_said_in_the_project_tools_words() -> None:
    recording = _Recording()
    scope = ProjectFileScope()
    wrapped = open_within_project(_binding(OPEN_TOOL_LOCAL_NAME, recording), scope)

    result = _open(
        wrapped,
        scope,
        _FakeStore(Path.cwd(), frozenset()),
        workspace_path="missing.html",
    )

    assert result.error is not None
    assert result.error.code == "not_found"
    assert result.error.message == "missing.html is not in the project directory"
    assert recording.seen == []
