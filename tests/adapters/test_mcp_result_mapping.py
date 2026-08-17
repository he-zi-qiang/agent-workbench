"""MCP responses stay bounded, complete and owned by the caller."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest

from agent_workbench.adapters.mcp.client import (
    RemoteBinaryBlock,
    RemoteCallResult,
    RemoteResourceLink,
    RemoteTextBlock,
    RemoteToolPage,
)
from agent_workbench.adapters.mcp.result_mapping import (
    MCPToolHandler,
    map_remote_result,
)
from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.domain.errors import NotFoundError, OperationCancelledError
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolCall, ToolResult
from agent_workbench.ports.cancellation import (
    CancellationSource,
    CancellationToken,
    NullCancellationToken,
)
from agent_workbench.ports.tools import ToolInvocation

TENANT = "tenant_real"
OWNER = "owner_real"


def _invocation(
    *,
    arguments: dict[str, object] | None = None,
    cancellation: CancellationToken | None = None,
) -> ToolInvocation:
    call = ToolCall(
        tool_call_id="toolu_mapping_1",
        tool_name="mcp_docs_render",
        arguments=arguments or {},
    )
    return ToolInvocation(
        call=call,
        context=ExecutionContext(
            principal=PrincipalContext(
                tenant_id=TENANT,
                principal_id=OWNER,
                scopes=("mcp:docs",),
            ),
            envelope=AuthorizationEnvelope(
                allowed_tools=("mcp_docs_render",),
                max_tool_risk="external",
                approval_required_risks=(),
            ),
            agent_run_id="run_mapping_1",
            policy_identity="policy_1:digest",
            task_id="task_mapping_1",
            lease_epoch=4,
        ),
        cancellation=(
            cancellation if cancellation is not None else NullCancellationToken()
        ),
        timeout_seconds=10,
    )


async def _map(
    remote: RemoteCallResult,
    store: InMemoryArtifactStore,
    *,
    invocation: ToolInvocation | None = None,
    threshold: int = 65_536,
    max_result: int = 1_048_576,
    max_artifact: int = 1_048_576,
):
    return await map_remote_result(
        invocation or _invocation(),
        remote,
        artifacts=store,
        artifact_threshold_bytes=threshold,
        max_result_bytes=max_result,
        max_artifact_bytes=max_artifact,
    )


def test_small_text_stays_inline_and_structured_content_is_not_duplicated() -> None:
    store = InMemoryArtifactStore()
    remote = RemoteCallResult(
        content=(RemoteTextBlock(text="plain answer"),),
        structured_content={"answer": "plain answer"},
    )

    result = asyncio.run(_map(remote, store))

    assert result.status == "ok"
    assert result.content == "plain answer"
    assert result.artifact is None


def test_structured_content_is_a_deterministic_fallback_for_empty_content() -> None:
    store = InMemoryArtifactStore()
    remote = RemoteCallResult(
        content=(),
        structured_content={"z": 1, "a": [True, None]},
    )

    result = asyncio.run(_map(remote, store))

    assert result.status == "ok"
    assert result.content == '{"a":[true,null],"z":1}'
    assert result.artifact is None


def test_resource_links_are_rendered_but_never_fetched() -> None:
    store = InMemoryArtifactStore()
    remote = RemoteCallResult(
        content=(
            RemoteResourceLink(
                name="manual",
                uri="https://example.test/private/manual",
                media_type="text/html",
            ),
        )
    )

    result = asyncio.run(_map(remote, store))

    assert result.status == "ok"
    assert result.content == "Resource manual: https://example.test/private/manual"
    assert result.artifact is None


def test_multiple_artifact_blocks_are_complete_and_byte_deterministic() -> None:
    remote = RemoteCallResult(
        content=(
            RemoteBinaryBlock(data=b"first", media_type="image/png", kind="image"),
            RemoteBinaryBlock(
                data=b"second",
                media_type="audio/wav",
                kind="audio",
            ),
        )
    )

    async def scenario() -> tuple[bytes, bytes]:
        first_store = InMemoryArtifactStore()
        second_store = InMemoryArtifactStore()
        first = await _map(remote, first_store)
        second = await _map(remote, second_store)
        assert first.artifact is not None
        assert second.artifact is not None
        assert first.artifact.media_type == "application/zip"
        assert first.artifact.filename == "mcp-result.zip"
        first_bytes = await first_store.get(
            tenant_id=TENANT,
            artifact_id=first.artifact.artifact_id,
            principal_id=OWNER,
        )
        second_bytes = await second_store.get(
            tenant_id=TENANT,
            artifact_id=second.artifact.artifact_id,
            principal_id=OWNER,
        )
        return first_bytes, second_bytes

    first_bytes, second_bytes = asyncio.run(scenario())

    assert first_bytes == second_bytes
    with zipfile.ZipFile(io.BytesIO(first_bytes)) as bundle:
        assert bundle.namelist() == [
            "manifest.json",
            "part-001.bin",
            "part-002.bin",
        ]
        assert json.loads(bundle.read("manifest.json")) == [
            {
                "file": "part-001.bin",
                "kind": "image",
                "media_type": "image/png",
                "size_bytes": 5,
            },
            {
                "file": "part-002.bin",
                "kind": "audio",
                "media_type": "audio/wav",
                "size_bytes": 6,
            },
        ]
        assert bundle.read("part-001.bin") == b"first"
        assert bundle.read("part-002.bin") == b"second"


@pytest.mark.parametrize(
    ("reported", "expected_media_type", "expected_filename"),
    [
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "mcp-result.docx",
        ),
        ("not a media type", "application/octet-stream", "mcp-result.bin"),
    ],
)
def test_a_single_artifact_preserves_a_valid_media_type_and_known_extension(
    reported: str,
    expected_media_type: str,
    expected_filename: str,
) -> None:
    store = InMemoryArtifactStore()
    remote = RemoteCallResult(
        content=(RemoteBinaryBlock(data=b"document", media_type=reported, kind="blob"),)
    )

    result = asyncio.run(_map(remote, store))

    assert result.status == "ok"
    assert result.artifact is not None
    assert result.artifact.media_type == expected_media_type
    assert result.artifact.filename == expected_filename


def test_artifact_tenant_and_owner_come_only_from_the_execution_context() -> None:
    store = InMemoryArtifactStore()
    invocation = _invocation(
        arguments={
            "tenant_id": "tenant_attacker",
            "owner_id": "owner_attacker",
        }
    )
    remote = RemoteCallResult(
        content=(
            RemoteBinaryBlock(
                data=b"owned bytes",
                media_type="application/octet-stream",
                kind="blob",
            ),
        ),
        structured_content={
            "tenant_id": "tenant_attacker",
            "owner_id": "owner_attacker",
        },
    )

    async def scenario() -> None:
        result = await _map(remote, store, invocation=invocation)
        assert result.artifact is not None
        assert result.artifact.tenant_id == TENANT
        assert (
            await store.get(
                tenant_id=TENANT,
                artifact_id=result.artifact.artifact_id,
                principal_id=OWNER,
            )
            == b"owned bytes"
        )
        with pytest.raises(NotFoundError, match="artifact not found"):
            await store.get(
                tenant_id="tenant_attacker",
                artifact_id=result.artifact.artifact_id,
                principal_id="owner_attacker",
            )
        with pytest.raises(NotFoundError, match="artifact not found"):
            await store.get(
                tenant_id=TENANT,
                artifact_id=result.artifact.artifact_id,
                principal_id="owner_attacker",
            )

    asyncio.run(scenario())


def test_remote_and_normalized_size_ceilings_fail_safely() -> None:
    async def scenario() -> None:
        store = InMemoryArtifactStore()
        remote_too_large = await _map(
            RemoteCallResult(content=(RemoteTextBlock(text="12345"),)),
            store,
            max_result=4,
        )
        artifact_too_large = await _map(
            RemoteCallResult(
                content=(
                    RemoteBinaryBlock(
                        data=b"12345",
                        media_type="application/octet-stream",
                        kind="blob",
                    ),
                )
            ),
            store,
            max_result=100,
            max_artifact=4,
        )

        assert remote_too_large.status == "error"
        assert remote_too_large.error is not None
        assert remote_too_large.error.code == "output_too_large"
        assert remote_too_large.artifact is None
        assert artifact_too_large.status == "error"
        assert artifact_too_large.error is not None
        assert artifact_too_large.error.code == "output_too_large"
        assert artifact_too_large.artifact is None

    asyncio.run(scenario())


def test_text_block_separators_count_toward_the_remote_result_ceiling() -> None:
    store = InMemoryArtifactStore()

    within = asyncio.run(
        _map(
            RemoteCallResult(content=tuple(RemoteTextBlock(text="") for _ in range(5))),
            store,
            max_result=4,
        )
    )
    over = asyncio.run(
        _map(
            RemoteCallResult(content=tuple(RemoteTextBlock(text="") for _ in range(6))),
            store,
            max_result=4,
        )
    )

    assert within.status == "ok"
    assert within.content == "\n\n\n\n"
    assert over.status == "error"
    assert over.error is not None
    assert over.error.code == "output_too_large"


def test_remote_error_text_is_not_exposed_or_persisted() -> None:
    store = InMemoryArtifactStore()
    secret = "api_key=never-copy-this"
    remote = RemoteCallResult(
        content=(RemoteTextBlock(text=secret),),
        is_error=True,
    )

    result = asyncio.run(_map(remote, store))

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_failed"
    assert secret not in result.error.message
    assert secret not in result.content
    assert result.artifact is None


def test_inline_ceiling_reserves_space_for_an_artifact_summary() -> None:
    store = InMemoryArtifactStore()
    remote = RemoteCallResult(
        content=(
            RemoteTextBlock(text="x" * 65_536),
            RemoteBinaryBlock(
                data=b"binary",
                media_type="application/octet-stream",
                kind="blob",
            ),
        )
    )

    async def scenario() -> tuple[str, bytes]:
        result = await _map(
            remote,
            store,
            threshold=65_536,
            max_result=100_000,
            max_artifact=100_000,
        )
        assert result.status == "ok"
        assert result.artifact is not None
        content = await store.get(
            tenant_id=TENANT,
            artifact_id=result.artifact.artifact_id,
            principal_id=OWNER,
        )
        return result.content, content

    inline, artifact = asyncio.run(scenario())

    assert len(inline) < 65_536
    with zipfile.ZipFile(io.BytesIO(artifact)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert [part["kind"] for part in manifest] == ["text", "blob"]
        assert bundle.read("part-001.bin") == b"x" * 65_536
        assert bundle.read("part-002.bin") == b"binary"


def test_non_json_structured_content_fails_without_exposing_repr() -> None:
    class _SecretValue:
        def __repr__(self) -> str:
            return "SECRET_REPR"

    store = InMemoryArtifactStore()
    remote = RemoteCallResult(content=(), structured_content=_SecretValue())

    result = asyncio.run(_map(remote, store))

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_failed"
    assert "SECRET_REPR" not in result.error.message


# --- A server process dying mid-call stays this node's failure --------------


def _dead_server_group() -> BaseExceptionGroup[BaseException]:
    """The exception shape measured on 2026-08-16 when a demo MCP server died.

    The connect failure (httpcore ``ConnectError`` in production, a stand-in
    here -- the classifier never reads types beyond Exception/CancelledError)
    surfaced inside the SDK transport's anyio task group, whose cleanup added
    ``RuntimeError: Attempted to exit cancel scope in a different task than it
    was entered in`` plus the scope's own ``CancelledError``.  That last leaf
    is the point: it keeps the composite a ``BaseExceptionGroup`` rather than
    an ``ExceptionGroup``, which is exactly why ``except Exception`` in the
    tool executor could not catch it and the Worker process died.
    """

    return BaseExceptionGroup(
        "unhandled errors in a TaskGroup",
        [
            ConnectionError("All connection attempts failed"),
            RuntimeError(
                "Attempted to exit cancel scope in a different task than "
                "it was entered in"
            ),
            asyncio.CancelledError(),
        ],
    )


class _DeadServerClient:
    """Every call fails the way a dead server process fails."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def list_tools_page(self, cursor: str | None) -> RemoteToolPage:
        raise self.error

    async def call_tool(self, name: str, arguments: JsonObject) -> RemoteCallResult:
        raise self.error


def _handler(client: _DeadServerClient, lock: asyncio.Lock) -> MCPToolHandler:
    return MCPToolHandler(
        client=client,
        remote_name="render",
        artifacts=InMemoryArtifactStore(),
        artifact_threshold_bytes=65_536,
        max_result_bytes=1_048_576,
        max_artifact_bytes=1_048_576,
        server_lock=lock,
    )


def test_a_server_dying_mid_call_becomes_this_nodes_retryable_failure() -> None:
    group = _dead_server_group()
    # The incident's premise, pinned so a future CPython cannot silently
    # invalidate this test: the composite is not an Exception.
    assert not isinstance(group, Exception)

    async def scenario() -> ToolResult:
        return await _handler(_DeadServerClient(group), asyncio.Lock())(_invocation())

    result = asyncio.run(scenario())

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_failed"
    assert result.error.retryable is True
    # Leaf type names may cross the boundary; third-party exception text no.
    assert "ConnectionError" in result.error.message
    assert "RuntimeError" in result.error.message
    assert "All connection attempts failed" not in result.error.message


def test_a_plain_transport_exception_is_absorbed_as_retryable() -> None:
    async def scenario() -> ToolResult:
        client = _DeadServerClient(ConnectionError("refused"))
        return await _handler(client, asyncio.Lock())(_invocation())

    result = asyncio.run(scenario())

    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "tool_failed"
    assert result.error.retryable is True


def test_pure_cancellation_still_propagates_through_the_call_boundary() -> None:
    async def cancelled_bare() -> None:
        await _handler(_DeadServerClient(asyncio.CancelledError()), asyncio.Lock())(
            _invocation()
        )

    async def cancelled_group() -> None:
        error = BaseExceptionGroup("cancelled", [asyncio.CancelledError()])
        await _handler(_DeadServerClient(error), asyncio.Lock())(_invocation())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled_bare())
    with pytest.raises(BaseExceptionGroup):
        asyncio.run(cancelled_group())


def test_a_group_carrying_a_process_signal_is_not_absorbed() -> None:
    error = BaseExceptionGroup(
        "shutdown", [ConnectionError("refused"), KeyboardInterrupt()]
    )

    async def scenario() -> None:
        await _handler(_DeadServerClient(error), asyncio.Lock())(_invocation())

    with pytest.raises(BaseExceptionGroup):
        asyncio.run(scenario())


def test_a_run_cancelled_while_the_server_died_reports_cancellation() -> None:
    source = CancellationSource()

    class _CancelledMidCall(_DeadServerClient):
        async def call_tool(self, name: str, arguments: JsonObject) -> RemoteCallResult:
            source.cancel("user_cancelled")
            raise self.error

    async def scenario() -> None:
        client = _CancelledMidCall(_dead_server_group())
        await _handler(client, asyncio.Lock())(_invocation(cancellation=source))

    with pytest.raises(OperationCancelledError):
        asyncio.run(scenario())
