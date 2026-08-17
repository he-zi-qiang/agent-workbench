"""The Task-side sandbox tool (ADR-029, stage 2 PR-2.2).

The container is exercised in ``tests/apps/test_sandbox_isolation.py``. What is
under test here is the gap this tool exists to bridge: workspace names in,
workspace versions out, with a server that never learns either.

The client is a stub for the same reason the executor is stubbed in the server's
own tests -- the thing being checked is what crosses the boundary, and a real
container would make every one of these a fifteen-second test that also proves
Docker works.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_workbench.adapters.mcp.client import RemoteCallResult, RemoteTextBlock
from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.adapters.tools.sandbox import (
    MAX_INLINE_STREAM_CHARS,
    MAX_OUTPUT_FILES,
    SandboxRunTool,
    SandboxUnavailableError,
)
from agent_workbench.application.workspace import Workspace, WorkspaceSession
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.apps.sandbox_mcp.contract import RUN_PYTHON_INPUT_SCHEMA
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.sandbox import (
    SANDBOX_REMOTE_TOOL,
    SANDBOX_RUN_SCOPE,
    SANDBOX_RUN_TOOL,
)
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolCall, ToolResult
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolInvocation
from agent_workbench.runtime.schema_validation import (
    assert_schema_supported,
    validate_arguments,
)

TENANT = "tenant_local"
OWNER = "user_local"


@dataclass
class _StubSandboxClient:
    """One canned answer, and a record of what it was asked."""

    outputs: tuple[tuple[str, bytes], ...] = ()
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    is_error: bool = False
    structured: object | None = None
    error_text: str = ""
    calls: list[tuple[str, JsonObject]] = field(default_factory=list)

    async def list_tools_page(self, cursor: str | None) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: JsonObject) -> RemoteCallResult:
        # Validated against the server's real schema, not merely recorded. A
        # stub that accepts anything cannot see the tool sending something the
        # server would refuse -- which is how `inputs: []` shipped, since the
        # schema declares the field optional with `minItems: 1`.
        validate_arguments(RUN_PYTHON_INPUT_SCHEMA, arguments)
        self.calls.append((name, arguments))
        if self.is_error:
            return RemoteCallResult(
                content=(RemoteTextBlock(text=self.error_text),), is_error=True
            )
        if self.structured is not None:
            return RemoteCallResult(content=(), structured_content=self.structured)
        return RemoteCallResult(
            content=(),
            structured_content={
                "exit_code": self.exit_code,
                "stdout": self.stdout,
                "stderr": self.stderr,
                "outputs": [
                    {
                        "name": output_name,
                        "content_base64": base64.b64encode(content).decode("ascii"),
                        "size_bytes": len(content),
                    }
                    for output_name, content in self.outputs
                ],
            },
        )


@contextmanager
def entered(*files: tuple[str, bytes]) -> Generator[WorkspaceScope]:
    """A scope holding a session, seeded the way an earlier node would leave it."""

    scope = WorkspaceScope()
    session = WorkspaceSession(
        workspace=Workspace(
            artifacts=InMemoryArtifactStore(),
            tenant_id=TENANT,
            principal_id=OWNER,
        )
    )
    for name, content in files:
        session.version = asyncio.run(
            session.workspace.write(
                session.version, name, content, media_type="text/plain"
            )
        )
    with scope.using(session):
        yield scope


def invoke(tool: SandboxRunTool, **arguments: object) -> ToolResult:
    call = ToolCall(
        tool_call_id="toolu_" + "0" * 20,
        tool_name=SANDBOX_RUN_TOOL,
        arguments=dict(arguments),
    )
    invocation = ToolInvocation(
        call=call,
        context=ExecutionContext(
            principal=PrincipalContext(tenant_id=TENANT, principal_id=OWNER),
            envelope=AuthorizationEnvelope(),
            agent_run_id="run_" + "0" * 28,
            policy_identity="test",
        ),
        cancellation=NullCancellationToken(),
        timeout_seconds=300,
    )
    return asyncio.run(tool.handle(invocation))


def names_in(scope: WorkspaceScope) -> tuple[str, ...]:
    session = scope.current()
    assert session is not None
    listing = asyncio.run(session.workspace.list(session.version))
    return tuple(item.name for item in listing)


def read(scope: WorkspaceScope, name: str) -> bytes:
    session = scope.current()
    assert session is not None
    return asyncio.run(session.workspace.read(session.version, name))


def test_the_declared_risk_profile_is_the_one_adr_029_wrote_down() -> None:
    """§3.5, field by field. Each of these is load-bearing somewhere else.

    `external` is what forces the Task envelope's ceiling up, `exclusive` is
    what the domain requires of anything above `read`, `safe` plus no operation
    key is §3.4's replay claim, and the scope is what the envelope allowlists.
    """

    spec = SandboxRunTool(scope=WorkspaceScope(), client=_StubSandboxClient()).binding()

    assert spec.spec.risk == "external"
    assert spec.spec.concurrency == "exclusive"
    assert spec.spec.idempotency == "safe"
    assert spec.spec.permission_scopes == (SANDBOX_RUN_SCOPE,)
    assert spec.operation_key is None
    assert_schema_supported(spec.spec.input_schema, origin="tool sandbox_run")


def test_the_model_names_workspace_files_and_never_sees_bytes() -> None:
    """The whole point of the Task-side half.

    The schema admits names; the client receives content. A schema that took
    content would be one where the model carries megabytes of base64 through
    its own context to reach a process that is one socket away.
    """

    client = _StubSandboxClient()
    with entered(("data.csv", b"a,b\n1,2\n")) as scope:
        result = invoke(
            SandboxRunTool(scope=scope, client=client),
            script="print(1)",
            inputs=["data.csv"],
        )

    assert result.status == "ok"
    name, arguments = client.calls[0]
    assert name == SANDBOX_REMOTE_TOOL
    assert arguments["inputs"] == [
        {
            "name": "data.csv",
            "content_base64": base64.b64encode(b"a,b\n1,2\n").decode("ascii"),
        }
    ]
    properties = (
        SandboxRunTool(scope=scope, client=client).spec().input_schema["properties"]
    )
    assert isinstance(properties, dict)
    assert set(properties) == {"script", "inputs"}


def test_outputs_become_workspace_versions() -> None:
    client = _StubSandboxClient(
        stdout="total 382\n",
        outputs=(("summary.txt", b"total=382\n"),),
    )
    with entered(("sales.csv", b"region,amount\n")) as scope:
        result = invoke(
            SandboxRunTool(scope=scope, client=client),
            script="pass",
            inputs=["sales.csv"],
        )

        assert result.status == "ok"
        assert "summary.txt" in (result.content or "")
        assert names_in(scope) == ("sales.csv", "summary.txt")
        assert read(scope, "summary.txt") == b"total=382\n"


def test_a_run_names_every_file_that_landed_and_only_those() -> None:
    """ADR-063, and the reason the field is a tuple rather than one name.

    One script can produce several files at once, so the sandbox is the tool
    that decided the shape. The order is the order they were written, which is
    also the order the summary sentence lists them in -- two renderings of one
    fact rather than two facts.
    """

    client = _StubSandboxClient(
        outputs=(("summary.txt", b"total=382\n"), ("plot.svg", b"<svg/>")),
    )
    with entered() as scope:
        result = invoke(SandboxRunTool(scope=scope, client=client), script="pass")

        assert result.workspace_writes == ("summary.txt", "plot.svg")


def test_a_run_that_only_computed_names_no_files() -> None:
    """The control: the field follows the writes, not the call."""

    client = _StubSandboxClient(stdout="4\n")
    with entered() as scope:
        result = invoke(
            SandboxRunTool(scope=scope, client=client), script="print(2 + 2)"
        )

        assert result.status == "ok"
        assert result.workspace_writes == ()


def test_a_partly_refused_run_reports_its_landed_files_only_in_the_message() -> None:
    """The honest limit of ADR-063, pinned so it is not mistaken for a bug.

    ``good.txt`` really is in the workspace. But this call answers with
    ``ToolResult.failed``, which the gateway records as ``ToolFailed`` -- an
    event with no ``workspace_writes`` field at all. So on this path the names
    of what landed survive only inside the error message, exactly as they did
    before this change. Extending the structured field to failures means
    extending ``ToolFailed`` too, and that is a second decision, not a detail
    of this one.
    """

    client = _StubSandboxClient(
        outputs=(("good.txt", b"kept"), ("../escape", b"refused")),
    )
    with entered() as scope:
        result = invoke(SandboxRunTool(scope=scope, client=client), script="pass")

        assert result.status == "error"
        assert result.workspace_writes == ()
        assert names_in(scope) == ("good.txt",)
        assert result.error is not None
        assert "good.txt" in result.error.message


def test_an_svg_output_is_typed_as_the_image_it_is() -> None:
    # Typed application/octet-stream (the fallback) an .svg was download-only
    # in the console. The guess costs a label, and this is the one suffix
    # where the wrong label hides the picture -- the `<img>` viewer shows it
    # rasterised, scripts and all inert.
    client = _StubSandboxClient(stdout="", outputs=(("plot.svg", b"<svg/>"),))
    with entered() as scope:
        result = invoke(SandboxRunTool(scope=scope, client=client), script="pass")

        assert result.status == "ok"
        session = scope.current()
        assert session is not None
        listing = asyncio.run(session.workspace.list(session.version))
        types = {item.name: item.media_type for item in listing}
        assert types["plot.svg"] == "image/svg+xml"


def test_a_script_with_no_inputs_omits_the_field_rather_than_sending_it_empty() -> None:
    """`inputs` is optional on the server and declares `minItems: 1`.

    Sending `[]` is a schema violation, so a script that only computes would be
    refused before it ran. The stub validates against the real schema, so this
    is the same refusal the server would produce.
    """

    client = _StubSandboxClient(stdout="4\n")
    with entered() as scope:
        result = invoke(SandboxRunTool(scope=scope, client=client), script="print(2+2)")

    assert result.status == "ok"
    assert client.calls[0][1] == {"script": "print(2+2)"}


def test_a_run_that_wrote_nothing_leaves_the_workspace_alone() -> None:
    """The control for the test above.

    Without it, an implementation that wrote a file on every call regardless
    of what came back would satisfy the first one.
    """

    client = _StubSandboxClient(stdout="4\n")
    with entered(("sales.csv", b"region,amount\n")) as scope:
        session = scope.current()
        assert session is not None
        before = session.version

        result = invoke(
            SandboxRunTool(scope=scope, client=client), script="print(2 + 2)"
        )

        assert result.status == "ok"
        assert session.version == before
        assert names_in(scope) == ("sales.csv",)


def test_a_named_input_missing_from_the_workspace_is_refused_before_the_call() -> None:
    """Named, rather than silently omitted.

    A script handed one fewer file than it asked for fails inside itself, and
    the traceback that comes back says nothing about the real cause.
    """

    client = _StubSandboxClient()
    with entered(("present.txt", b"x")) as scope:
        result = invoke(
            SandboxRunTool(scope=scope, client=client),
            script="pass",
            inputs=["present.txt", "absent.txt"],
        )

    assert result.status == "error"
    assert client.calls == []

    # Control: the same call without the missing name reaches the sandbox.
    client = _StubSandboxClient()
    with entered(("present.txt", b"x")) as scope:
        ok = invoke(
            SandboxRunTool(scope=scope, client=client),
            script="pass",
            inputs=["present.txt"],
        )
    assert ok.status == "ok"
    assert len(client.calls) == 1


def test_a_failing_script_is_a_successful_tool_call() -> None:
    """The traceback is the answer, not an error to swallow.

    A tool failure would cost the model the stderr it needs to fix the script.
    """

    client = _StubSandboxClient(exit_code=1, stderr="ValueError: boom\n")
    with entered() as scope:
        result = invoke(SandboxRunTool(scope=scope, client=client), script="pass")

    assert result.status == "ok"
    assert "exit_code: 1" in (result.content or "")
    assert "ValueError: boom" in (result.content or "")


def test_a_refused_run_is_a_tool_failure_and_carries_the_reason() -> None:
    client = _StubSandboxClient(is_error=True, error_text="timeout: 60 seconds")
    with entered() as scope:
        result = invoke(SandboxRunTool(scope=scope, client=client), script="pass")

    assert result.status == "error"
    assert result.error is not None
    assert "timeout" in result.error.message


def test_a_malformed_result_is_refused_rather_than_half_applied() -> None:
    client = _StubSandboxClient(structured={"exit_code": 0, "outputs": "not a list"})
    with entered(("keep.txt", b"x")) as scope:
        result = invoke(SandboxRunTool(scope=scope, client=client), script="pass")

        assert result.status == "error"
        assert names_in(scope) == ("keep.txt",)


def test_a_very_long_stream_is_marked_rather_than_silently_cut() -> None:
    """A model shown a truncated stream with no marker reads it as the whole one.

    ``workspace_read`` answers an oversized read the same way, and the reason
    carries: the size is the part that tells the model to go get the rest.
    """

    client = _StubSandboxClient(stdout="x" * (MAX_INLINE_STREAM_CHARS + 500))
    with entered() as scope:
        result = invoke(SandboxRunTool(scope=scope, client=client), script="pass")

    content = result.content or ""
    assert f"{MAX_INLINE_STREAM_CHARS + 500} characters" in content

    # Control: a stream inside the ceiling arrives whole and unannotated.
    client = _StubSandboxClient(stdout="y" * MAX_INLINE_STREAM_CHARS)
    with entered() as scope:
        within = invoke(SandboxRunTool(scope=scope, client=client), script="pass")
    assert "characters; first" not in (within.content or "")
    assert "y" * MAX_INLINE_STREAM_CHARS in (within.content or "")


def test_more_output_files_than_the_ceiling_is_refused_and_the_ceiling_is_not() -> None:
    over = _StubSandboxClient(
        outputs=tuple((f"f{index}.txt", b"x") for index in range(MAX_OUTPUT_FILES + 1))
    )
    with entered() as scope:
        refused = invoke(SandboxRunTool(scope=scope, client=over), script="pass")

        assert refused.status == "error"
        assert names_in(scope) == ()

    at_limit = _StubSandboxClient(
        outputs=tuple((f"f{index}.txt", b"x") for index in range(MAX_OUTPUT_FILES))
    )
    with entered() as scope:
        accepted = invoke(SandboxRunTool(scope=scope, client=at_limit), script="pass")

        assert accepted.status == "ok"
        assert len(names_in(scope)) == MAX_OUTPUT_FILES


def test_an_output_name_the_workspace_refuses_names_what_did_land() -> None:
    """Partial by construction; the report says which part.

    The versions committed before the refusal are real and stay. A caller told
    only "failed" would either redo work that landed or abandon work that did.
    """

    client = _StubSandboxClient(
        outputs=(("good.txt", b"kept"), ("../escape", b"refused")),
    )
    with entered() as scope:
        result = invoke(SandboxRunTool(scope=scope, client=client), script="pass")

        assert result.status == "error"
        assert result.error is not None
        assert "good.txt" in result.error.message
        assert names_in(scope) == ("good.txt",)


def test_running_outside_an_entered_session_refuses_rather_than_inventing_one() -> None:
    """A workspace no node committed is one no checkpoint names.

    Everything written into it would be discarded at the end of the run with
    nothing saying so, which is the same reasoning the workspace tools refuse
    on (ADR-028 §3.2).
    """

    tool = SandboxRunTool(scope=WorkspaceScope(), client=_StubSandboxClient())

    with pytest.raises(SandboxUnavailableError):
        invoke(tool, script="pass")
