"""Workspace to container and back, with nothing stubbed in between.

The tool's own tests stub the client and the server's own tests stub the
executor, which is right for both -- but between them sits the claim this whole
stage exists to make: a name in the workspace becomes a file in a container, and
what the script writes becomes the next workspace version. Every piece of that
is real here. The MCP protocol runs over the official SDK's in-memory transport,
so there is no listening port to make flaky; the container is a real one, so
there is nothing to fake about the part that could be faked.

Skipped, loudly, without a container runtime or the interpreter image.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import Any, cast

import pytest

from agent_workbench.adapters.mcp.client import connect_mcp_client
from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.adapters.tools.sandbox import SandboxRunTool
from agent_workbench.application.workspace import Workspace, WorkspaceSession
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.apps.sandbox_mcp.executor import (
    DEFAULT_CONTAINER_RUNTIME,
    DEFAULT_SANDBOX_IMAGE,
)
from agent_workbench.apps.sandbox_mcp.server import create_server
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.sandbox import SANDBOX_RUN_TOOL
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolInvocation

TENANT = "tenant_e2e"
OWNER = "user_e2e"


def _require_runtime() -> None:
    if shutil.which(DEFAULT_CONTAINER_RUNTIME) is None:
        pytest.skip(f"{DEFAULT_CONTAINER_RUNTIME} is not installed")
    probe = subprocess.run(
        [DEFAULT_CONTAINER_RUNTIME, "image", "inspect", DEFAULT_SANDBOX_IMAGE],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if probe.returncode != 0:
        pytest.skip(
            f"{DEFAULT_SANDBOX_IMAGE} is not present locally; "
            f"run `{DEFAULT_CONTAINER_RUNTIME} pull {DEFAULT_SANDBOX_IMAGE}`"
        )


def test_a_workspace_file_is_computed_over_and_the_result_comes_back() -> None:
    _require_runtime()

    async def scenario() -> tuple[str, tuple[str, ...], bytes]:
        artifacts = InMemoryArtifactStore()
        session = WorkspaceSession(
            workspace=Workspace(
                artifacts=artifacts, tenant_id=TENANT, principal_id=OWNER
            )
        )
        session.version = await session.workspace.write(
            session.version,
            "sales.csv",
            b"region,amount\nnorth,100\nsouth,250\neast,25\nwest,7\n",
            media_type="text/csv",
        )
        entry_version = session.version

        scope = WorkspaceScope()
        # The production adapter receives a URL; the official v2 Client also
        # accepts an in-memory server. Same constructor, real protocol, no port.
        async with connect_mcp_client(
            cast(Any, create_server()), timeout_seconds=300
        ) as client:
            tool = SandboxRunTool(scope=scope, client=client)
            with scope.using(session):
                result = await tool.handle(
                    ToolInvocation(
                        call=ToolCall(
                            tool_call_id="toolu_" + "0" * 20,
                            tool_name=SANDBOX_RUN_TOOL,
                            arguments={
                                "script": (
                                    "import csv, pathlib\n"
                                    "rows = list(csv.DictReader(open('sales.csv')))\n"
                                    "total = sum(int(r['amount']) for r in rows)\n"
                                    "print('total', total)\n"
                                    "pathlib.Path('summary.md').write_text(\n"
                                    "    f'# Sales\\n\\nTotal: {total}\\n'\n"
                                    ")\n"
                                ),
                                "inputs": ["sales.csv"],
                            },
                        ),
                        context=ExecutionContext(
                            principal=PrincipalContext(
                                tenant_id=TENANT, principal_id=OWNER
                            ),
                            envelope=AuthorizationEnvelope(),
                            agent_run_id="run_" + "0" * 28,
                            policy_identity="e2e",
                        ),
                        cancellation=NullCancellationToken(),
                        timeout_seconds=300,
                    )
                )

        assert result.status == "ok", result.error
        assert session.version != entry_version
        listing = await session.workspace.list(session.version)
        summary = await session.workspace.read(session.version, "summary.md")
        # The entry version still resolves: writing produced a new manifest and
        # replaced nothing (ADR-028).
        entry_listing = await session.workspace.list(entry_version)
        assert tuple(item.name for item in entry_listing) == ("sales.csv",)
        return (
            result.content or "",
            tuple(item.name for item in listing),
            summary,
        )

    content, names, summary = asyncio.run(scenario())

    assert "total 382" in content
    assert "summary.md" in content
    assert names == ("sales.csv", "summary.md")
    assert summary == b"# Sales\n\nTotal: 382\n"


def test_a_script_that_reaches_for_the_network_fails_through_the_whole_path() -> None:
    """The premise, asserted where an agent would actually meet it.

    ``test_sandbox_isolation.py`` proves the container has no network. This
    proves nothing between the workspace and the container quietly restores it,
    and that the failure arrives as a readable result rather than as a tool
    error the model cannot act on.
    """

    _require_runtime()

    async def scenario() -> tuple[str, tuple[str, ...]]:
        session = WorkspaceSession(
            workspace=Workspace(
                artifacts=InMemoryArtifactStore(),
                tenant_id=TENANT,
                principal_id=OWNER,
            )
        )
        scope = WorkspaceScope()
        async with connect_mcp_client(
            cast(Any, create_server()), timeout_seconds=300
        ) as client:
            tool = SandboxRunTool(scope=scope, client=client)
            with scope.using(session):
                result = await tool.handle(
                    ToolInvocation(
                        call=ToolCall(
                            tool_call_id="toolu_" + "0" * 20,
                            tool_name=SANDBOX_RUN_TOOL,
                            arguments={
                                "script": (
                                    "import socket, pathlib\n"
                                    "socket.create_connection(('1.1.1.1', 80), 5)\n"
                                    "pathlib.Path('leak.txt').write_text('exfil')\n"
                                )
                            },
                        ),
                        context=ExecutionContext(
                            principal=PrincipalContext(
                                tenant_id=TENANT, principal_id=OWNER
                            ),
                            envelope=AuthorizationEnvelope(),
                            agent_run_id="run_" + "0" * 28,
                            policy_identity="e2e",
                        ),
                        cancellation=NullCancellationToken(),
                        timeout_seconds=300,
                    )
                )
            listing = await session.workspace.list(session.version)
        return result.content or "", tuple(item.name for item in listing)

    content, names = asyncio.run(scenario())

    assert "Network is unreachable" in content
    # The script died before its write, so nothing was bound. This is the
    # control that says a failed run does not leave a half-made workspace.
    assert names == ()
