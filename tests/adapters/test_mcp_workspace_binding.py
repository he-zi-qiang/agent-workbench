"""An MCP tool's file has to reach the working set (ADR-026 + ADR-028).

The failure this closes is not a crash. `task_fe2c66b7...` rendered a 37 KB Word
package through `mcp_word_render_document`, the gateway allowed every call, the
artifact was stored under the Task's own tenant and owner -- and the `review`
node, whose only tools read the workspace, answered:

    decision: revise
    summary: The workspace is empty -- no Word document or any file exists to
             review.

Two revisions later the Task failed. Everything worked except the one thing that
made the work visible to the node judging it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager

from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.adapters.tools.mcp_workspace import bind_results_into_workspace
from agent_workbench.adapters.tools.workspace import WorkspaceListTool
from agent_workbench.application.workspace import Workspace, WorkspaceSession
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

TENANT = "tenant_local"
OWNER = "user_local"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

SPEC = ToolSpec(
    name="mcp_word_render_document",
    description="Render a Word document.",
    input_schema={"type": "object", "additionalProperties": False},
    concurrency="exclusive",
    risk="external",
    idempotency="safe",
    timeout_seconds=30,
    permission_scopes=("mcp:word",),
)


@contextmanager
def entered() -> Generator[tuple[WorkspaceScope, InMemoryArtifactStore]]:
    """A scope with a session in it, the way a node invocation supplies one."""

    artifacts = InMemoryArtifactStore()
    scope = WorkspaceScope()
    session = WorkspaceSession(
        workspace=Workspace(artifacts=artifacts, tenant_id=TENANT, principal_id=OWNER)
    )
    with scope.using(session):
        yield scope, artifacts


def _invocation(call: ToolCall) -> ToolInvocation:
    return ToolInvocation(
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


def _rendering(
    artifacts: InMemoryArtifactStore,
    *,
    filename: str | None = "mcp-result.docx",
    content: str = "Rendered one document.",
) -> ToolBinding:
    """A binding that stores bytes and returns them the way the MCP mapping does."""

    async def handle(invocation: ToolInvocation) -> ToolResult:
        ref = await artifacts.put(
            tenant_id=TENANT,
            owner_id=OWNER,
            kind="tool_result",
            media_type=DOCX_MEDIA_TYPE,
            content=b"PK\x03\x04 pretend this is a docx",
            filename=filename,
        )
        return ToolResult.succeeded(invocation.call, content=content, artifact=ref)

    return ToolBinding(spec=SPEC, handler=handle)


def _call() -> ToolCall:
    return ToolCall(tool_call_id="call_render_1", tool_name=SPEC.name, arguments={})


def test_a_rendered_file_becomes_something_the_reviewer_can_list() -> None:
    with entered() as (scope, artifacts):
        bound = bind_results_into_workspace(_rendering(artifacts), scope)

        result = asyncio.run(bound.handler(_invocation(_call())))

        assert result.status == "ok"
        listing = asyncio.run(
            WorkspaceListTool(scope).handle(
                _invocation(
                    ToolCall(tool_call_id="call_list_1", tool_name="workspace_list")
                )
            )
        )
        assert "mcp-result.docx" in listing.content
        # The type travels with the reference, so the listing says what the file
        # is rather than what a name suggests.
        assert DOCX_MEDIA_TYPE in listing.content


def test_the_model_is_told_the_name_it_can_read_the_file_by() -> None:
    # Without this the model knows a document exists and has no way to name it
    # to the reviewer, which is one more round trip at best.
    with entered() as (scope, artifacts):
        bound = bind_results_into_workspace(_rendering(artifacts), scope)

        result = asyncio.run(bound.handler(_invocation(_call())))

        assert "Rendered one document." in result.content
        assert "mcp-result.docx" in result.content


def test_the_bytes_are_not_stored_a_second_time() -> None:
    # `write_ref`, not a copy: the artifact is already under this Task's tenant
    # and owner. Duplicating a 37 KB package per render is the thing the
    # manifest's reference shape exists to avoid.
    with entered() as (scope, artifacts):
        bound = bind_results_into_workspace(_rendering(artifacts), scope)

        result = asyncio.run(bound.handler(_invocation(_call())))

        assert result.artifact is not None
        session = scope.current()
        assert session is not None
        manifest = asyncio.run(session.workspace.load(session.version))
        assert (
            manifest.entries["mcp-result.docx"].artifact_id
            == result.artifact.artifact_id
        )


def test_a_second_render_replaces_the_first_under_the_same_name() -> None:
    # "The current document" is what a reviewer reads the working set for. Two
    # entries differing by a counter would make it guess which one is the work.
    with entered() as (scope, artifacts):
        bound = bind_results_into_workspace(_rendering(artifacts), scope)

        asyncio.run(bound.handler(_invocation(_call())))
        asyncio.run(bound.handler(_invocation(_call())))

        session = scope.current()
        assert session is not None
        manifest = asyncio.run(session.workspace.load(session.version))
        assert list(manifest.entries) == ["mcp-result.docx"]


def test_a_name_the_workspace_cannot_take_falls_back_to_the_tool_name() -> None:
    # A server names its own file and this one is not a flat ASCII name. Refusing
    # the bind over it would lose the document to protect a label.
    with entered() as (scope, artifacts):
        bound = bind_results_into_workspace(
            _rendering(artifacts, filename="季度总结.docx"), scope
        )

        asyncio.run(bound.handler(_invocation(_call())))

        session = scope.current()
        assert session is not None
        manifest = asyncio.run(session.workspace.load(session.version))
        assert list(manifest.entries) == ["mcp_word_render_document.bin"]


def test_a_result_with_no_file_is_returned_untouched() -> None:
    # The control. Most MCP results are text, and a wrapper that touched those
    # would be changing what every tool says.
    with entered() as (scope, _artifacts):

        async def handle(invocation: ToolInvocation) -> ToolResult:
            return ToolResult.succeeded(invocation.call, content="just text")

        bound = bind_results_into_workspace(
            ToolBinding(spec=SPEC, handler=handle), scope
        )

        result = asyncio.run(bound.handler(_invocation(_call())))

        assert result.content == "just text"
        session = scope.current()
        assert session is not None
        assert session.version is None


def test_a_node_that_entered_no_session_still_gets_its_result() -> None:
    # The researcher holds MCP tools and never enters a working set. Binding is
    # what it cannot do, not a reason to fail its download.
    artifacts = InMemoryArtifactStore()
    scope = WorkspaceScope()
    bound = bind_results_into_workspace(_rendering(artifacts), scope)

    result = asyncio.run(bound.handler(_invocation(_call())))

    assert result.status == "ok"
    assert result.artifact is not None
    assert "workspace" not in result.content


def test_the_spec_the_model_sees_is_unchanged() -> None:
    # The envelope names tools by spec, and the gateway refuses one it did not
    # advertise. A wrapper that altered the spec would change what a Task is
    # authorized to call.
    artifacts = InMemoryArtifactStore()
    original = _rendering(artifacts)

    bound = bind_results_into_workspace(original, WorkspaceScope())

    assert bound.spec == original.spec
    assert bound.operation_key is original.operation_key


def test_a_server_that_names_nothing_still_lands_in_the_workspace() -> None:
    # `ArtifactRef` refuses an empty filename outright, so "unnamed" reaches
    # here as `None` and only as `None`.
    with entered() as (scope, artifacts):
        bound = bind_results_into_workspace(_rendering(artifacts, filename=None), scope)

        asyncio.run(bound.handler(_invocation(_call())))

        session = scope.current()
        assert session is not None
        manifest = asyncio.run(session.workspace.load(session.version))
        assert list(manifest.entries) == ["mcp_word_render_document.bin"]
