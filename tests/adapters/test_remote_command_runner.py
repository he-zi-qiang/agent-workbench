"""The client half of ADR-0115: what crosses the wire, and what comes back.

Driven with a fake `MCPClientPort` rather than a server, because the server
has its own file (``tests/apps/test_runner_mcp_server.py``) and what is
under test here is the translation -- the arguments the runner is asked with,
and the three ways an answer is not an outcome.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_workbench.adapters.mcp.client import (
    RemoteCallResult,
    RemoteTextBlock,
    RemoteToolPage,
)
from agent_workbench.adapters.tools.runner import (
    RemoteCommandRunner,
    RunnerRefusedError,
)
from agent_workbench.domain.runner import RUNNER_REMOTE_TOOL


@dataclass
class _FakeClient:
    result: RemoteCallResult
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def list_tools_page(self, cursor: str | None) -> RemoteToolPage:
        raise AssertionError("the runner never lists tools")

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, on_progress: Any = None
    ) -> RemoteCallResult:
        self.calls.append((name, dict(arguments)))
        return self.result


def _run(client: _FakeClient, *, timeout: float = 120.0) -> Any:
    return asyncio.run(
        RemoteCommandRunner(client=client).run(
            "pytest -q", cwd="/projects/demo", timeout_seconds=timeout
        )
    )


def test_the_command_the_directory_and_a_whole_second_clock_are_sent() -> None:
    client = _FakeClient(
        RemoteCallResult(
            content=(RemoteTextBlock(text="exit code: 0"),),
            structured_content={
                "exit_code": 0,
                "output": "ok\n",
                "timed_out": False,
                "overflowed": False,
            },
        )
    )

    outcome = _run(client, timeout=120.0)

    assert client.calls == [
        (
            RUNNER_REMOTE_TOOL,
            {"command": "pytest -q", "cwd": "/projects/demo", "timeout_seconds": 120},
        )
    ]
    assert outcome.exit_code == 0
    assert outcome.output == "ok\n"
    assert outcome.timed_out is False
    assert outcome.overflowed is False


def test_a_killed_command_comes_back_with_no_exit_code() -> None:
    client = _FakeClient(
        RemoteCallResult(
            content=(),
            structured_content={
                "exit_code": None,
                "output": "partial",
                "timed_out": True,
                "overflowed": False,
            },
        )
    )

    outcome = _run(client)

    assert outcome.exit_code is None
    assert outcome.timed_out is True
    assert outcome.output == "partial"


def test_a_refusal_on_the_wire_is_raised_with_the_runners_own_sentence() -> None:
    client = _FakeClient(
        RemoteCallResult(
            content=(RemoteTextBlock(text="invalid run request: cwd is outside"),),
            is_error=True,
        )
    )

    with pytest.raises(RunnerRefusedError, match="cwd is outside") as caught:
        _run(client)
    assert caught.value.code == "tool_failed"


def test_an_answer_without_a_body_is_refused_not_guessed() -> None:
    client = _FakeClient(RemoteCallResult(content=(RemoteTextBlock(text="ok"),)))

    with pytest.raises(RunnerRefusedError, match="no structured result"):
        _run(client)


def test_a_body_that_does_not_parse_is_refused() -> None:
    client = _FakeClient(
        RemoteCallResult(
            content=(),
            structured_content={"exit_code": "three", "output": ""},
        )
    )

    with pytest.raises(RunnerRefusedError, match="malformed"):
        _run(client)
