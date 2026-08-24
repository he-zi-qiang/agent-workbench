"""The Worker-side half of the non-retryable refusal (ADR-075 §1).

The refusal has two ends and they fail differently. The envelope end is pinned
by ``tests/config/test_local_computer_profile.py``: a name that never enters an
authorization envelope cannot be authorized by anything downstream. This file
pins the other one, which nothing covered until now -- ``_build_mcp_bindings``
skipping a server whose effects were not declared retryable.

Asserting that the result is empty would prove nothing here: a server the
Worker *tried* to reach and could not also yields nothing, and that is the
ordinary state of this endpoint in a test process. So the assertion is that the
connector is never called at all. Deleting the skip turns that from a decision
into a connection attempt, and this file goes red rather than staying green on
a coincidence.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any, cast

import pytest

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.apps.task_worker import composition
from agent_workbench.bootstrap.projections import (
    MCPConfig,
    MCPServerConfig,
    TaskWorkerRuntimeConfig,
)
from agent_workbench.ports.tools import ToolBinding

#: Deliberately a port nothing serves. If the skip below ever stops happening,
#: the failure should be "it tried to connect", not "it connected".
UNSERVED = "http://127.0.0.1:8768/mcp"


def _mcp(*, retryable: bool) -> MCPConfig:
    return MCPConfig(
        servers=(
            MCPServerConfig(
                alias="computer",
                endpoint=UNSERVED,
                retryable_effects=retryable,
                timeout_seconds=5,
                remote_tools=("screenshot", "left_click"),
                audience="synthesis",
            ),
        ),
        artifact_threshold_bytes=1024,
        max_result_bytes=65_536,
        max_artifact_bytes=1_048_576,
    )


async def _discover(
    config: MCPConfig, tmp_path: Any
) -> tuple[tuple[ToolBinding, str], ...]:
    async with AsyncExitStack() as resources:
        return cast(
            "tuple[tuple[ToolBinding, str], ...]",
            await composition._build_mcp_bindings(
                cast("TaskWorkerRuntimeConfig", _Config(mcp=config)),
                artifacts=LocalArtifactStore(root=tmp_path),
                resources=resources,
                workspace_scope=cast("Any", object()),
            ),
        )
    raise AssertionError("unreachable")


class _Config:
    """Only the attribute the traversal reads.

    A real ``TaskWorkerRuntimeConfig`` would drag in a database DSN and a
    provider key to test one ``if``.
    """

    def __init__(self, *, mcp: MCPConfig) -> None:
        self.mcp = mcp


def test_a_non_retryable_server_is_never_even_connected_to(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def _refuse_to_connect(endpoint: str, **_: Any) -> Any:
        attempts.append(endpoint)
        raise AssertionError(
            "the Worker opened a connection to a server it must not bind"
        )

    monkeypatch.setattr(composition, "connect_mcp_client", _refuse_to_connect)

    bindings = asyncio.run(_discover(_mcp(retryable=False), tmp_path))

    assert bindings == ()
    assert attempts == []


def test_the_same_server_declared_retryable_is_connected_to(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control group, and the reason the test above means anything.

    Without it, a traversal that skipped *every* server -- or one that had
    stopped being called at all -- would satisfy the assertion above and prove
    nothing about which declaration did the skipping.
    """

    attempts: list[str] = []

    def _record_then_fail(endpoint: str, **_: Any) -> Any:
        attempts.append(endpoint)
        raise ConnectionRefusedError(endpoint)

    monkeypatch.setattr(composition, "connect_mcp_client", _record_then_fail)

    bindings = asyncio.run(_discover(_mcp(retryable=True), tmp_path))

    # Still nothing bound -- nothing is serving that port -- but the Worker got
    # as far as trying, which is exactly what the declaration asks it to do.
    assert bindings == ()
    assert attempts == [UNSERVED]
