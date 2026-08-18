"""Discovery freezes one defensive MCP directory into local bindings."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

import pytest

from agent_workbench.adapters.mcp.client import (
    ProgressSink,
    RemoteCallResult,
    RemoteTextBlock,
    RemoteToolDefinition,
    RemoteToolPage,
)
from agent_workbench.adapters.mcp.registry_source import (
    MAX_DISCOVERED_TOOLS,
    MAX_DISCOVERY_PAGES,
    discover_bindings,
)
from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

GOOD_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "additionalProperties": False,
}


def _tool(
    name: str,
    *,
    schema: object = GOOD_SCHEMA,
    description: str | None = None,
    task_support: Literal["forbidden", "optional", "required"] | None = None,
) -> RemoteToolDefinition:
    return RemoteToolDefinition(
        name=name,
        description=description or f"Run {name}.",
        input_schema=schema,
        task_support=task_support,
    )


@dataclass(slots=True)
class _DirectoryClient:
    pages: dict[str | None, RemoteToolPage]
    result: RemoteCallResult = field(
        default_factory=lambda: RemoteCallResult(
            content=(RemoteTextBlock(text="remote result"),)
        )
    )
    list_calls: list[str | None] = field(default_factory=list)
    tool_calls: list[tuple[str, JsonObject]] = field(default_factory=list)

    async def list_tools_page(self, cursor: str | None) -> RemoteToolPage:
        self.list_calls.append(cursor)
        return self.pages[cursor]

    async def call_tool(
        self,
        name: str,
        arguments: JsonObject,
        *,
        on_progress: ProgressSink | None = None,
    ) -> RemoteCallResult:
        self.tool_calls.append((name, arguments))
        return self.result


async def _discover(
    client: _DirectoryClient,
    *,
    allowed: tuple[str, ...],
    alias: str = "docs",
    server_lock: asyncio.Lock | None = None,
) -> tuple[ToolBinding, ...]:
    return await discover_bindings(
        alias=alias,
        allowed_remote_tools=allowed,
        timeout_seconds=2,
        client=client,
        artifacts=InMemoryArtifactStore(),
        artifact_threshold_bytes=65_536,
        max_result_bytes=1_048_576,
        max_artifact_bytes=1_048_576,
        server_lock=server_lock,
    )


def _invocation(tool_name: str) -> ToolInvocation:
    call = ToolCall(
        tool_call_id="toolu_registry_1",
        tool_name=tool_name,
        arguments={"value": "hello"},
    )
    context = ExecutionContext(
        principal=PrincipalContext(
            tenant_id="tenant_a",
            principal_id="user_1",
            scopes=("mcp:docs",),
        ),
        envelope=AuthorizationEnvelope(
            allowed_tools=(tool_name,),
            max_tool_risk="external",
            approval_required_risks=(),
        ),
        agent_run_id="run_registry_1",
        policy_identity="policy_1:digest",
        task_id="task_registry_1",
        lease_epoch=7,
    )
    return ToolInvocation(
        call=call,
        context=context,
        cancellation=NullCancellationToken(),
        timeout_seconds=2,
    )


def test_pagination_allowlist_and_binding_translation_form_one_snapshot() -> None:
    client = _DirectoryClient(
        pages={
            None: RemoteToolPage(
                tools=(_tool("echo"), _tool("unlisted")),
                next_cursor="next-page",
            ),
            "next-page": RemoteToolPage(tools=(_tool("render"),)),
        }
    )

    async def scenario() -> None:
        bindings = await _discover(
            client,
            allowed=("render", "echo"),
        )

        assert [binding.spec.name for binding in bindings] == [
            "mcp_docs_echo",
            "mcp_docs_render",
        ]
        for binding in bindings:
            assert binding.spec.risk == "external"
            assert binding.spec.idempotency == "safe"
            assert binding.spec.concurrency == "exclusive"
            assert binding.spec.permission_scopes == ("mcp:docs",)
            assert binding.operation_key is None

        echo = bindings[0]
        result = await echo.handler(_invocation(echo.spec.name))
        assert result.status == "ok"
        assert result.content == "remote result"

    asyncio.run(scenario())

    assert client.list_calls == [None, "next-page"]
    assert client.tool_calls == [("echo", {"value": "hello"})]


def test_a_tool_that_requires_remote_mcp_tasks_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _DirectoryClient(
        pages={
            None: RemoteToolPage(
                tools=(
                    _tool("long_job", task_support="required"),
                    _tool("sync_job", task_support="optional"),
                )
            )
        }
    )

    with caplog.at_level(
        logging.WARNING,
        logger="agent_workbench.adapters.mcp.registry_source",
    ):
        bindings = asyncio.run(_discover(client, allowed=("long_job", "sync_job")))

    assert [binding.spec.name for binding in bindings] == ["mcp_docs_sync_job"]
    assert any(
        getattr(record, "mcp_skip_reason", None)
        == "remote MCP Tasks support is required"
        for record in caplog.records
    )


def test_a_repeated_cursor_rejects_the_whole_server_snapshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _DirectoryClient(
        pages={
            None: RemoteToolPage(tools=(_tool("first"),), next_cursor="again"),
            "again": RemoteToolPage(tools=(_tool("second"),), next_cursor="again"),
        }
    )

    with caplog.at_level(
        logging.WARNING,
        logger="agent_workbench.adapters.mcp.registry_source",
    ):
        bindings = asyncio.run(_discover(client, allowed=("first", "second")))

    assert bindings == ()
    assert client.list_calls == [None, "again"]
    assert any(record.message == "mcp_discovery_failed" for record in caplog.records)


def test_allowlist_and_normalized_collision_both_fail_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _DirectoryClient(
        pages={
            None: RemoteToolPage(
                tools=(
                    _tool("foo-bar"),
                    _tool("foo.bar"),
                    _tool("safe"),
                    _tool("advertised_but_unlisted"),
                )
            )
        }
    )

    with caplog.at_level(
        logging.WARNING,
        logger="agent_workbench.adapters.mcp.registry_source",
    ):
        bindings = asyncio.run(
            _discover(
                client,
                allowed=("foo-bar", "foo.bar", "safe", "configured_missing"),
            )
        )

    assert [binding.spec.name for binding in bindings] == ["mcp_docs_safe"]
    skipped = {
        (
            record.__dict__["mcp_remote_tool"],
            record.__dict__["mcp_skip_reason"],
        )
        for record in caplog.records
        if record.message == "mcp_tool_skipped"
    }
    assert any(name == "foo-bar" and "collides" in reason for name, reason in skipped)
    assert any(name == "foo.bar" and "collides" in reason for name, reason in skipped)
    assert any(
        name == "configured_missing" and "absent" in reason for name, reason in skipped
    )
    assert all(name != "advertised_but_unlisted" for name, _ in skipped)


def test_one_bad_name_and_schema_do_not_remove_an_unrelated_tool() -> None:
    client = _DirectoryClient(
        pages={
            None: RemoteToolPage(
                tools=(
                    _tool("bad/character"),
                    _tool(
                        "bad_schema",
                        schema={
                            "type": "object",
                            "properties": {"value": {"oneOf": [{"type": "string"}]}},
                        },
                    ),
                    _tool("good"),
                )
            )
        }
    )

    bindings = asyncio.run(
        _discover(client, allowed=("bad/character", "bad_schema", "good"))
    )

    assert [binding.spec.name for binding in bindings] == ["mcp_docs_good"]


def test_a_directory_over_the_tool_bound_rejects_the_whole_snapshot() -> None:
    client = _DirectoryClient(
        pages={
            None: RemoteToolPage(
                tools=tuple(
                    _tool(f"tool_{index}") for index in range(MAX_DISCOVERED_TOOLS + 1)
                )
            )
        }
    )

    bindings = asyncio.run(_discover(client, allowed=("tool_0",)))

    assert bindings == ()


def test_a_directory_that_never_finishes_hits_the_page_bound() -> None:
    pages: dict[str | None, RemoteToolPage] = {
        None: RemoteToolPage(tools=(), next_cursor="1")
    }
    pages.update(
        {
            str(index): RemoteToolPage(tools=(), next_cursor=str(index + 1))
            for index in range(1, MAX_DISCOVERY_PAGES + 1)
        }
    )
    client = _DirectoryClient(pages=pages)

    bindings = asyncio.run(_discover(client, allowed=("never_present",)))

    assert bindings == ()
    assert len(client.list_calls) == MAX_DISCOVERY_PAGES


def test_all_bindings_from_one_server_share_the_execution_lock() -> None:
    @dataclass(slots=True)
    class _OverlapClient(_DirectoryClient):
        active: int = 0
        maximum_active: int = 0

        async def call_tool(
            self,
            name: str,
            arguments: JsonObject,
            *,
            on_progress: ProgressSink | None = None,
        ) -> RemoteCallResult:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0)
            self.tool_calls.append((name, arguments))
            self.active -= 1
            return self.result

    client = _OverlapClient(
        pages={None: RemoteToolPage(tools=(_tool("first"), _tool("second")))}
    )

    async def scenario() -> None:
        bindings = await _discover(client, allowed=("first", "second"))
        await asyncio.gather(
            *(binding.handler(_invocation(binding.spec.name)) for binding in bindings)
        )

    asyncio.run(scenario())

    assert client.maximum_active == 1


def test_bindings_from_different_servers_do_not_share_a_lock() -> None:
    @dataclass(slots=True)
    class _SharedActivity:
        active: int = 0
        maximum_active: int = 0

    @dataclass(slots=True)
    class _OverlapClient(_DirectoryClient):
        activity: _SharedActivity = field(default_factory=_SharedActivity)

        async def call_tool(
            self,
            name: str,
            arguments: JsonObject,
            *,
            on_progress: ProgressSink | None = None,
        ) -> RemoteCallResult:
            self.activity.active += 1
            self.activity.maximum_active = max(
                self.activity.maximum_active, self.activity.active
            )
            await asyncio.sleep(0)
            self.tool_calls.append((name, arguments))
            self.activity.active -= 1
            return self.result

    activity = _SharedActivity()
    first = _OverlapClient(
        pages={None: RemoteToolPage(tools=(_tool("run"),))},
        activity=activity,
    )
    second = _OverlapClient(
        pages={None: RemoteToolPage(tools=(_tool("run"),))},
        activity=activity,
    )

    async def scenario() -> None:
        first_bindings, second_bindings = await asyncio.gather(
            _discover(first, allowed=("run",), alias="first"),
            _discover(second, allowed=("run",), alias="second"),
        )
        await asyncio.gather(
            first_bindings[0].handler(_invocation(first_bindings[0].spec.name)),
            second_bindings[0].handler(_invocation(second_bindings[0].spec.name)),
        )

    asyncio.run(scenario())

    assert activity.maximum_active == 2


def test_a_server_dying_during_discovery_degrades_to_zero_bindings() -> None:
    # The 2026-08-16 mid-call shape (see test_mcp_result_mapping) applies to
    # discovery too: the CancelledError leaf makes the composite a
    # BaseExceptionGroup, which the previous `except Exception` degradation
    # path could not catch -- it would have killed the process at startup.
    class _DyingClient(_DirectoryClient):
        async def list_tools_page(self, cursor: str | None) -> RemoteToolPage:
            raise BaseExceptionGroup(
                "unhandled errors in a TaskGroup",
                [
                    ConnectionError("All connection attempts failed"),
                    RuntimeError(
                        "Attempted to exit cancel scope in a different task "
                        "than it was entered in"
                    ),
                    asyncio.CancelledError(),
                ],
            )

    bindings = asyncio.run(_discover(_DyingClient(pages={}), allowed=("echo",)))

    assert bindings == ()


def test_cancellation_during_discovery_still_propagates() -> None:
    class _CancelledClient(_DirectoryClient):
        async def list_tools_page(self, cursor: str | None) -> RemoteToolPage:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_discover(_CancelledClient(pages={}), allowed=("echo",)))
