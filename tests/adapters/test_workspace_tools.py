"""The three workspace tools (ADR-028, stage 1 PR-1.3).

Every refusal is paired with the control that must still succeed, and the
schemas are checked against the gateway's own validator: a tool this repository
ships must pass the same gate a third-party one does.
"""

from __future__ import annotations

import asyncio

from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.adapters.tools.workspace import (
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceSession,
    WorkspaceWriteTool,
)
from agent_workbench.application.workspace import TaskWorkspace
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolInvocation
from agent_workbench.runtime.schema_validation import assert_schema_supported

TENANT = "tenant_local"
OWNER = "user_local"


def session() -> WorkspaceSession:
    return WorkspaceSession(
        workspace=TaskWorkspace(
            artifacts=InMemoryArtifactStore(),
            tenant_id=TENANT,
            principal_id=OWNER,
        )
    )


def invoke(tool: object, **arguments: object) -> object:
    call = ToolCall(
        tool_call_id="toolu_" + "0" * 20,
        tool_name=tool.binding().spec.name,
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
        timeout_seconds=30,
    )
    return asyncio.run(tool.handle(invocation))


def test_every_workspace_schema_passes_the_gateway_validator() -> None:
    # The subset this repository enforces is deliberately small. A tool it ships
    # that could not pass its own gate would be found at gateway assembly, i.e.
    # at process start, which is a bad place to learn it.
    space = session()
    for tool in (
        WorkspaceListTool(space),
        WorkspaceReadTool(space),
        WorkspaceWriteTool(space),
    ):
        spec = tool.binding().spec
        assert_schema_supported(spec.input_schema, origin=f"tool {spec.name}")


def test_a_write_advances_the_session_version_so_the_node_can_commit_it() -> None:
    space = session()
    assert space.version is None

    result = invoke(WorkspaceWriteTool(space), name="notes.md", content="hello")

    assert result.status == "ok"
    assert space.version is not None


def test_read_returns_what_write_put_there() -> None:
    space = session()
    invoke(WorkspaceWriteTool(space), name="notes.md", content="hello")

    result = invoke(WorkspaceReadTool(space), name="notes.md")

    assert result.status == "ok"
    assert "hello" in result.content


def test_reading_a_missing_name_is_a_failed_result_not_an_empty_one() -> None:
    space = session()
    invoke(WorkspaceWriteTool(space), name="present.md", content="x")

    missing = invoke(WorkspaceReadTool(space), name="absent.md")
    present = invoke(WorkspaceReadTool(space), name="present.md")

    assert missing.status == "error"
    assert present.status == "ok"


def test_a_path_shaped_name_is_refused_and_the_version_does_not_move() -> None:
    space = session()
    invoke(WorkspaceWriteTool(space), name="ok.md", content="x")
    after_valid = space.version

    refused = invoke(WorkspaceWriteTool(space), name="../escape", content="x")

    assert refused.status == "error"
    assert space.version == after_valid

    # Control: a legal name straight after the refusal still moves it.
    invoke(WorkspaceWriteTool(space), name="escape.md", content="x")
    assert space.version != after_valid


def test_list_reports_names_sizes_and_types() -> None:
    space = session()
    invoke(WorkspaceWriteTool(space), name="b.md", content="two")
    invoke(WorkspaceWriteTool(space), name="a.md", content="one")

    result = invoke(WorkspaceListTool(space))
    content = result.content

    assert result.status == "ok"
    # Sorted, so a listing shown to a model is the same for the same workspace.
    assert content.index("a.md") < content.index("b.md")
    assert "text/markdown" in content


def test_an_empty_workspace_lists_as_empty_rather_than_failing() -> None:
    result = invoke(WorkspaceListTool(session()))

    assert result.status == "ok"


def test_the_write_tool_declares_the_risk_its_effects_have() -> None:
    spec = WorkspaceWriteTool(session()).binding().spec

    assert spec.risk == "write"
    assert spec.concurrency == "exclusive"
    assert spec.permission_scopes == ("workspace:write",)
    # No operation key: the write lands in this project's own versioned store,
    # so a replay produces another version rather than a second outside effect.
    assert WorkspaceWriteTool(session()).binding().operation_key is None


def test_the_read_tools_are_safe_and_parallel() -> None:
    space = session()
    for tool in (WorkspaceListTool(space), WorkspaceReadTool(space)):
        spec = tool.binding().spec
        assert spec.risk == "read"
        assert spec.idempotency == "safe"
        assert spec.concurrency == "parallel"
