"""The three workspace tools (ADR-028, stage 1 PR-1.3).

Every refusal is paired with the control that must still succeed, and the
schemas are checked against the gateway's own validator: a tool this repository
ships must pass the same gate a third-party one does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.adapters.tools.workspace import (
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceUnavailableError,
    WorkspaceWriteTool,
)
from agent_workbench.application.workspace import TaskWorkspace, WorkspaceSession
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolInvocation
from agent_workbench.runtime.schema_validation import assert_schema_supported
from agent_workbench.workflows.workspace_scope import WorkspaceScope

TENANT = "tenant_local"
OWNER = "user_local"


@contextmanager
def entered() -> Generator[WorkspaceScope]:
    """A scope with a session in it, the way a node invocation supplies one."""

    scope = WorkspaceScope()
    session = WorkspaceSession(
        workspace=TaskWorkspace(
            artifacts=InMemoryArtifactStore(),
            tenant_id=TENANT,
            principal_id=OWNER,
        )
    )
    with scope.using(session):
        yield scope


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


def version_of(scope: WorkspaceScope) -> str | None:
    session = scope.current()
    assert session is not None
    return session.version


def test_every_workspace_schema_passes_the_gateway_validator() -> None:
    # The subset this repository enforces is deliberately small. A tool it ships
    # that could not pass its own gate would be found at gateway assembly, i.e.
    # at process start, which is a bad place to learn it.
    with entered() as scope:
        for tool in (
            WorkspaceListTool(scope),
            WorkspaceReadTool(scope),
            WorkspaceWriteTool(scope),
        ):
            spec = tool.binding().spec
            assert_schema_supported(spec.input_schema, origin=f"tool {spec.name}")


def test_a_write_advances_the_session_version_so_the_node_can_commit_it() -> None:
    with entered() as scope:
        assert version_of(scope) is None

        result = invoke(WorkspaceWriteTool(scope), name="notes.md", content="hello")

        assert result.status == "ok"
        assert version_of(scope) is not None


def test_read_returns_what_write_put_there() -> None:
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="notes.md", content="hello")

        result = invoke(WorkspaceReadTool(scope), name="notes.md")

        assert result.status == "ok"
        assert "hello" in result.content


def test_reading_a_missing_name_is_a_failed_result_not_an_empty_one() -> None:
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="present.md", content="x")

        missing = invoke(WorkspaceReadTool(scope), name="absent.md")
        present = invoke(WorkspaceReadTool(scope), name="present.md")

        assert missing.status == "error"
        assert present.status == "ok"


def test_a_path_shaped_name_is_refused_and_the_version_does_not_move() -> None:
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="ok.md", content="x")
        after_valid = version_of(scope)

        refused = invoke(WorkspaceWriteTool(scope), name="../escape", content="x")

        assert refused.status == "error"
        assert version_of(scope) == after_valid

        # Control: a legal name straight after the refusal still moves it.
        invoke(WorkspaceWriteTool(scope), name="escape.md", content="x")
        assert version_of(scope) != after_valid


def test_list_reports_names_sizes_and_types() -> None:
    with entered() as scope:
        invoke(WorkspaceWriteTool(scope), name="b.md", content="two")
        invoke(WorkspaceWriteTool(scope), name="a.md", content="one")

        result = invoke(WorkspaceListTool(scope))

        assert result.status == "ok"
        # Sorted, so a listing shown to a model is the same for the same set.
        assert result.content.index("a.md") < result.content.index("b.md")
        assert "text/markdown" in result.content


def test_an_empty_workspace_lists_as_empty_rather_than_failing() -> None:
    with entered() as scope:
        assert invoke(WorkspaceListTool(scope)).status == "ok"


def test_the_write_tool_declares_the_risk_its_effects_have() -> None:
    with entered() as scope:
        binding = WorkspaceWriteTool(scope).binding()

        assert binding.spec.risk == "write"
        assert binding.spec.concurrency == "exclusive"
        assert binding.spec.permission_scopes == ("workspace:write",)
        # No operation key: the write lands in this project's own versioned
        # store, so a replay produces another version rather than a second
        # outside effect.
        assert binding.operation_key is None


def test_the_read_tools_are_safe_and_parallel() -> None:
    with entered() as scope:
        for tool in (WorkspaceListTool(scope), WorkspaceReadTool(scope)):
            spec = tool.binding().spec
            assert spec.risk == "read"
            assert spec.idempotency == "safe"
            assert spec.concurrency == "parallel"


def test_a_tool_outside_a_node_refuses_rather_than_inventing_a_workspace() -> None:
    # A workspace nothing committed is one no checkpoint names, so everything
    # written into it would be discarded at the end of the run in silence.
    with pytest.raises(WorkspaceUnavailableError):
        invoke(WorkspaceListTool(WorkspaceScope()))


def test_two_sessions_in_one_process_do_not_see_each_other() -> None:
    # Two Task lanes in one Worker process is the ordinary case since ADR-024.
    with entered() as first:
        invoke(WorkspaceWriteTool(first), name="first.md", content="1")
        first_version = version_of(first)

        with entered() as second:
            assert version_of(second) is None
            empty = invoke(WorkspaceListTool(second))
            assert empty.content == "The workspace is empty."

        assert version_of(first) == first_version
