"""The MCP surface of the sandbox process, and the knobs it deliberately lacks.

The container itself is exercised in ``test_sandbox_isolation.py``. What is
under test here is the protocol boundary: what the tool declares, what a
refusal looks like on the wire, and the fact that nothing outside
``executor.ISOLATION_FLAGS`` can reach the isolation.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from agent_workbench.apps.sandbox_mcp import executor as executor_module
from agent_workbench.apps.sandbox_mcp import main as main_module
from agent_workbench.apps.sandbox_mcp.contract import SandboxFile, SandboxRequest
from agent_workbench.apps.sandbox_mcp.executor import (
    ISOLATION_FLAGS,
    OutputSink,
    SandboxExecutionError,
    SandboxExecutor,
    SandboxOutcome,
)
from agent_workbench.apps.sandbox_mcp.server import (
    HEALTH_PATH,
    MCP_PATH,
    STDERR_PREFIX,
    TOOL_NAME,
    create_app,
    create_server,
)


@dataclass(frozen=True, slots=True)
class _StubExecutor(SandboxExecutor):
    """Stands in for the container so the protocol can be tested in memory."""

    outcome: SandboxOutcome | None = None
    failure: SandboxExecutionError | None = None
    available: bool = True
    #: What the "script" prints while it "runs", as `(channel, text)`.
    streams: tuple[tuple[str, str], ...] = ()

    async def probe(self) -> bool:
        return self.available

    async def run(
        self,
        request: SandboxRequest,
        *,
        on_output: OutputSink | None = None,
    ) -> SandboxOutcome:
        _seen.append(request)
        if self.failure is not None:
            raise self.failure
        if on_output is not None:
            for channel, text in self.streams:
                await on_output(channel, text)
        assert self.outcome is not None
        return self.outcome


_seen: list[SandboxRequest] = []


def _outcome(**overrides: Any) -> SandboxOutcome:
    fields: dict[str, Any] = {
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "outputs": (),
    }
    return SandboxOutcome(**{**fields, **overrides})


def _call(executor: SandboxExecutor, arguments: dict[str, Any]) -> Any:
    async def scenario() -> Any:
        async with Client(
            create_server(executor), cache=None, raise_exceptions=True
        ) as client:
            return await client.call_tool(TOOL_NAME, arguments)

    return asyncio.run(scenario())


def test_the_server_declares_exactly_one_tool() -> None:
    async def scenario() -> Any:
        async with Client(
            create_server(_StubExecutor(outcome=_outcome())),
            cache=None,
            raise_exceptions=True,
        ) as client:
            return await client.list_tools()

    tools = asyncio.run(scenario()).tools
    assert [tool.name for tool in tools] == [TOOL_NAME]

    tool = tools[0]
    annotations = tool.annotations
    assert annotations is not None
    # It executes code, so it is not read-only. It is still repeatable and
    # closed: ADR-029 §3.4 is what lets `idempotent` and a shut world both be
    # true here, and the network switch is what lets §3.4 hold.
    assert annotations.read_only_hint is False
    assert annotations.destructive_hint is False
    assert annotations.idempotent_hint is True
    assert annotations.open_world_hint is False


def test_a_successful_run_returns_structured_content_and_a_summary() -> None:
    outcome = _outcome(
        exit_code=0,
        stdout="total 382\n",
        outputs=(SandboxFile(name="summary.txt", content=b"total=382\n"),),
    )

    result = _call(
        _StubExecutor(outcome=outcome),
        {"script": "print('total 382')"},
    )

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    assert structured["exit_code"] == 0
    assert structured["stdout"] == "total 382\n"
    assert structured["outputs"] == [
        {
            "name": "summary.txt",
            "content_base64": base64.b64encode(b"total=382\n").decode("ascii"),
            "size_bytes": 10,
        }
    ]


def test_the_model_facing_text_names_the_outputs_without_carrying_them() -> None:
    """Output bytes belong in the structured channel.

    A four-megabyte file base64-encoded into the model's context is the whole
    context spent on something the caller was going to write to a workspace
    anyway.
    """

    payload = b"x" * 4096
    result = _call(
        _StubExecutor(
            outcome=_outcome(outputs=(SandboxFile(name="big.bin", content=payload),))
        ),
        {"script": "print(1)"},
    )

    text = "".join(
        block.text for block in result.content if getattr(block, "text", None)
    )
    assert "big.bin" in text
    assert base64.b64encode(payload).decode("ascii") not in text


def test_a_non_zero_exit_is_a_result_and_not_a_protocol_error() -> None:
    """A script that raised is the answer, and its traceback is the useful part."""

    result = _call(
        _StubExecutor(
            outcome=_outcome(exit_code=1, stderr="ValueError: boom\n"),
        ),
        {"script": "raise ValueError('boom')"},
    )

    assert result.is_error is False
    structured = result.structured_content
    assert structured is not None
    assert structured["exit_code"] == 1
    assert "ValueError: boom" in structured["stderr"]


@pytest.mark.parametrize(
    "code",
    ["timeout", "stdout_too_large", "too_many_outputs", "sandbox_unavailable"],
)
def test_a_refused_run_is_an_error_result_carrying_its_code(code: str) -> None:
    """The caller has to tell a ceiling from a broken sandbox, and only the
    code says which."""

    result = _call(
        _StubExecutor(failure=SandboxExecutionError(code, "refused")),
        {"script": "print(1)"},
    )

    assert result.is_error is True
    assert code in "".join(block.text for block in result.content)


def test_an_invalid_request_never_reaches_the_container() -> None:
    """The control is the accepted form of the same call, one test above."""

    _seen.clear()
    result = _call(
        _StubExecutor(outcome=_outcome()),
        {"script": "print(1)", "inputs": [{"name": "../escape", "content_base64": ""}]},
    )

    assert result.is_error is True
    assert _seen == []


def test_a_refusal_does_not_echo_the_script() -> None:
    """These strings travel into events, operator logs and the model's context."""

    secret = "print('SENSITIVE-MARKER')"
    result = _call(
        _StubExecutor(outcome=_outcome()),
        {"script": secret, "inputs": [{"name": "ok.txt", "content_base64": "!!!"}]},
    )

    assert result.is_error is True
    assert "SENSITIVE-MARKER" not in "".join(block.text for block in result.content)


def test_an_unknown_tool_is_refused() -> None:
    async def scenario() -> Any:
        async with Client(
            create_server(_StubExecutor(outcome=_outcome())),
            cache=None,
            raise_exceptions=True,
        ) as client:
            return await client.call_tool("render_document", {})

    result = asyncio.run(scenario())
    assert result.is_error is True


def test_health_reports_the_runtime_rather_than_a_flat_ok() -> None:
    """A liveness check that says ok without a runtime is how a deployment
    with no sandbox looks healthy until the first call (ADR-029 §3.6)."""

    with TestClient(create_app(executor=_StubExecutor(available=True))) as client:  # pyright: ignore[reportArgumentType]
        healthy = client.get(HEALTH_PATH)
    assert healthy.status_code == 200
    assert healthy.json()["container_runtime_available"] is True

    with TestClient(create_app(executor=_StubExecutor(available=False))) as client:  # pyright: ignore[reportArgumentType]
        degraded = client.get(HEALTH_PATH)
    assert degraded.status_code == 503
    assert degraded.json()["container_runtime_available"] is False


def test_every_isolation_flag_is_passed_to_the_runtime() -> None:
    """A regression guard, not the proof.

    ``test_sandbox_isolation.py`` is where the network, the read-only root and
    the wall clock are actually tried. This one covers the flags whose effect
    no test can observe from inside a script -- the memory and CPU ceilings --
    and catches a reordering that drops one on the floor.
    """

    captured: list[tuple[str, ...]] = []

    async def fake_exec(*args: str, **kwargs: object) -> object:
        captured.append(args)
        raise OSError("not started")

    async def scenario() -> None:
        executor = SandboxExecutor()
        with pytest.raises(SandboxExecutionError):
            await executor.run(SandboxRequest(script="print(1)", inputs=()))

    original = executor_module.asyncio.create_subprocess_exec
    executor_module.asyncio.create_subprocess_exec = fake_exec  # type: ignore[assignment]
    try:
        asyncio.run(scenario())
    finally:
        executor_module.asyncio.create_subprocess_exec = original  # type: ignore[assignment]

    assert len(captured) == 1
    argv = captured[0]
    for flag in ISOLATION_FLAGS:
        assert flag in argv
    assert "--rm" in argv
    # No bind mount of any shape, however spelled.
    assert not any(
        argument.startswith(("--volume", "-v", "--mount")) for argument in argv
    )


def test_the_command_line_exposes_no_way_to_weaken_the_sandbox() -> None:
    """ADR-029 §3.2: the isolation is not a configuration surface.

    The runtime and the image are deployment facts, like --host and --port.
    Anything that would relax the container is absent, and this is the test
    that fails when somebody adds one.
    """

    parser_flags: set[str] = set()

    class _Recorder:
        def add_argument(self, *names: str, **kwargs: object) -> None:
            parser_flags.update(name for name in names if name.startswith("--"))

        def parse_args(self, argv: object) -> None:
            raise SystemExit(0)

    original = main_module.argparse.ArgumentParser
    main_module.argparse.ArgumentParser = lambda **kwargs: _Recorder()  # type: ignore[assignment]
    try:
        with pytest.raises(SystemExit):
            main_module.main([])
    finally:
        main_module.argparse.ArgumentParser = original  # type: ignore[assignment]

    assert parser_flags == {"--host", "--port", "--container-runtime", "--image"}


# --- The preview crossing the protocol (ADR-069) ---------------------------


def test_the_scripts_output_reaches_the_caller_as_progress() -> None:
    """`notifications/progress`, one per slice the executor reports.

    The value carried in `progress` counts characters streamed rather than a
    fraction of the work, and `total` stays `None`: this server cannot know how
    much a script will print, and the protocol's way of saying so is to leave
    the end unstated rather than to invent one.
    """

    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(
        progress: float, total: float | None, message: str | None
    ) -> None:
        seen.append((progress, total, message))

    async def scenario() -> Any:
        executor = _StubExecutor(
            outcome=_outcome(),
            streams=(("stdout", "line one\n"), ("stderr", "a warning\n")),
        )
        async with Client(
            create_server(executor), cache=None, raise_exceptions=True
        ) as client:
            return await client.call_tool(
                TOOL_NAME, {"script": "print(1)"}, progress_callback=on_progress
            )

    asyncio.run(scenario())

    assert [message for _, _, message in seen] == [
        "line one\n",
        f"{STDERR_PREFIX}a warning\n",
    ]
    # Monotonic, which the protocol requires of the field.
    assert [progress for progress, _, _ in seen] == [9.0, 19.0]
    assert all(total is None for _, total, _ in seen)


def test_a_caller_that_did_not_ask_for_progress_still_gets_its_result() -> None:
    """`report_progress` is a no-op without a progress token.

    Worth pinning rather than assuming: the server reports unconditionally, so
    if that were *not* a no-op every call from a client with no callback would
    fail on a notification it never asked for.
    """

    result = _call(
        _StubExecutor(
            outcome=_outcome(),
            streams=(("stdout", "line one\n"),),
        ),
        {"script": "print(1)"},
    )

    assert result.is_error is False
    assert result.structured_content is not None


def test_the_transport_answers_with_a_stream_not_one_json_body() -> None:
    """The line that makes the streaming above real rather than theoretical.

    Under `json_response=True` a call is answered by a single JSON document and
    a notification raised while the tool is still running has nowhere to go --
    measured: the client's progress callback fires zero times, with no error
    anywhere to say so. This asserts the app is built the other way, because
    nothing else in this file would notice if it were flipped back.
    """

    app = create_app(host="testserver", executor=_StubExecutor(outcome=_outcome()))
    with TestClient(app) as client:  # pyright: ignore[reportArgumentType]
        response = client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )

    assert response.status_code == 200
    # The observable difference, and the whole assertion: a stream can carry a
    # notification raised mid-call, and a single JSON document cannot.
    assert response.headers["content-type"].startswith("text/event-stream")
