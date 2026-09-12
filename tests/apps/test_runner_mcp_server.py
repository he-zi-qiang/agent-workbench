"""The MCP surface of the command runner (ADR-0115), and what it refuses.

The shell itself is exercised where `project_run` always exercised it
(``tests/adapters/test_project_tools.py``, on POSIX). What is under test here
is the protocol boundary: what the one tool declares, that `cwd` is judged
against the root this process was started with and not against the caller's
word, and that a run's outcome crosses the wire whole.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from agent_workbench.apps.runner_mcp.contract import (
    RunnerInputError,
    parse_run_request,
)
from agent_workbench.apps.runner_mcp.server import (
    HEALTH_PATH,
    TOOL_NAME,
    create_app,
    create_server,
)
from agent_workbench.ports.commands import CommandOutcome


@dataclass
class _StubRunner:
    """Stands in for the shell so the protocol can be tested in memory."""

    outcome: CommandOutcome
    seen: list[tuple[str, str, float]] = field(default_factory=list)

    async def run(
        self, command: str, *, cwd: str, timeout_seconds: float
    ) -> CommandOutcome:
        self.seen.append((command, cwd, timeout_seconds))
        return self.outcome


def _outcome(**overrides: Any) -> CommandOutcome:
    fields: dict[str, Any] = {
        "exit_code": 0,
        "output": "",
        "timed_out": False,
        "overflowed": False,
    }
    return CommandOutcome(**{**fields, **overrides})


def _call(root: Path, runner: _StubRunner, arguments: dict[str, Any]) -> Any:
    async def scenario() -> Any:
        async with Client(
            create_server(projects_root=root, runner=runner),
            cache=None,
            raise_exceptions=True,
        ) as client:
            return await client.call_tool(TOOL_NAME, arguments)

    return asyncio.run(scenario())


def test_the_server_declares_exactly_one_tool_and_says_it_is_not_safe() -> None:
    async def scenario() -> Any:
        async with Client(
            create_server(projects_root=Path("."), runner=_StubRunner(_outcome())),
            cache=None,
            raise_exceptions=True,
        ) as client:
            return await client.list_tools()

    tools = asyncio.run(scenario()).tools
    assert [tool.name for tool in tools] == [TOOL_NAME]
    annotations = tools[0].annotations
    assert annotations is not None
    # It runs arbitrary commands over the user's real files. `project_run` in
    # front of it says the same with `risk="destructive"`; a generic client
    # reading hints must not conclude otherwise.
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is True
    assert annotations.idempotent_hint is False


def test_a_command_under_the_root_runs_and_comes_back_whole(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    runner = _StubRunner(_outcome(exit_code=3, output="one\nnope\n"))

    result = _call(
        tmp_path,
        runner,
        {"command": "pytest -q", "cwd": str(project), "timeout_seconds": 30},
    )

    assert result.is_error is False
    assert runner.seen == [("pytest -q", str(project.resolve()), 30.0)]
    structured = result.structured_content
    assert structured == {
        "exit_code": 3,
        "output": "one\nnope\n",
        "timed_out": False,
        "overflowed": False,
    }
    assert "exit code: 3" in result.content[0].text


def test_a_cwd_outside_the_root_is_refused_before_anything_runs(
    tmp_path: Path,
) -> None:
    """The one check that makes a mounted folder a boundary.

    The caller is the API process naming a directory it believes both sides
    share; the only place that belief can be checked is against the directory
    this process actually mounted.
    """

    root = tmp_path / "projects"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    runner = _StubRunner(_outcome())

    result = _call(root, runner, {"command": "ls", "cwd": str(elsewhere)})

    assert result.is_error is True
    assert "outside the projects root" in result.content[0].text
    assert runner.seen == []


def test_a_cwd_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    runner = _StubRunner(_outcome())

    result = _call(tmp_path, runner, {"command": "ls", "cwd": str(tmp_path / "gone")})

    assert result.is_error is True
    assert "does not exist" in result.content[0].text
    assert runner.seen == []


def test_the_contract_refuses_a_relative_cwd_and_a_long_clock(tmp_path: Path) -> None:
    with pytest.raises(RunnerInputError, match="absolute"):
        parse_run_request({"command": "ls", "cwd": "demo"}, projects_root=tmp_path)
    with pytest.raises(RunnerInputError, match="timeout_seconds"):
        parse_run_request(
            {"command": "ls", "cwd": str(tmp_path), "timeout_seconds": 10_000},
            projects_root=tmp_path,
        )
    # The root itself is a legal directory: the launcher's smoke probe and a
    # session whose project *is* the root both run there.
    request = parse_run_request(
        {"command": "ls", "cwd": str(tmp_path)}, projects_root=tmp_path
    )
    assert request.cwd == tmp_path.resolve()


def test_a_command_the_clock_killed_says_so_on_both_channels(tmp_path: Path) -> None:
    runner = _StubRunner(_outcome(exit_code=None, output="partial", timed_out=True))

    result = _call(tmp_path, runner, {"command": "sleep 999", "cwd": str(tmp_path)})

    assert result.is_error is False
    assert result.structured_content["timed_out"] is True
    assert result.structured_content["exit_code"] is None
    assert "wall clock" in result.content[0].text


def test_health_names_the_tool_and_refuses_while_the_mount_is_missing(
    tmp_path: Path,
) -> None:
    """503 until the folder is there, so a start whose bind mount did not
    arrive is not turned into a `project_run` every call of which fails."""

    with TestClient(
        create_app(projects_root=tmp_path / "missing", runner=_StubRunner(_outcome()))
    ) as client:
        degraded = client.get(HEALTH_PATH)
    assert degraded.status_code == 503
    assert degraded.json()["projects_root_mounted"] is False

    with TestClient(
        create_app(projects_root=tmp_path, runner=_StubRunner(_outcome()))
    ) as client:
        healthy = client.get(HEALTH_PATH)
    assert healthy.status_code == 200
    assert healthy.json()["tools"] == [TOOL_NAME]


@pytest.mark.skipif(
    sys.platform == "win32", reason="the runner is /bin/sh and process groups"
)
def test_the_default_runner_really_runs_a_shell(tmp_path: Path) -> None:
    """One real command through the real `LocalCommandRunner`, so the server
    is known to be wired to a shell and not only to a stub."""

    async def scenario() -> Any:
        async with Client(
            create_server(projects_root=tmp_path), cache=None, raise_exceptions=True
        ) as client:
            return await client.call_tool(
                TOOL_NAME, {"command": "echo hi; pwd", "cwd": str(tmp_path)}
            )

    result = asyncio.run(scenario())
    assert result.is_error is False
    assert result.structured_content["exit_code"] == 0
    assert "hi" in result.structured_content["output"]
    assert str(tmp_path.resolve()) in result.structured_content["output"]
