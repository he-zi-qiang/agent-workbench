"""The official MCP SDK behind a small project-owned client contract.

No SDK model crosses this module.  Discovery and result mapping consume the
frozen dataclasses below, which keeps ``mcp`` and ``mcp_types`` at the outer
adapter boundary and makes malformed remote payloads independently testable.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol, cast, runtime_checkable

from mcp import Client, types

from agent_workbench.domain.schema import JsonObject


@dataclass(frozen=True, slots=True)
class RemoteToolDefinition:
    name: str
    description: str | None
    input_schema: object
    task_support: Literal["forbidden", "optional", "required"] | None = None


@dataclass(frozen=True, slots=True)
class RemoteToolPage:
    tools: tuple[RemoteToolDefinition, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteTextBlock:
    text: str
    media_type: str | None = None
    embedded_resource: bool = False


@dataclass(frozen=True, slots=True)
class RemoteBinaryBlock:
    data: bytes
    media_type: str | None
    kind: str


@dataclass(frozen=True, slots=True)
class RemoteResourceLink:
    name: str
    uri: str
    media_type: str | None = None


RemoteContentBlock = RemoteTextBlock | RemoteBinaryBlock | RemoteResourceLink


@dataclass(frozen=True, slots=True)
class RemoteCallResult:
    content: tuple[RemoteContentBlock, ...]
    structured_content: object | None = None
    is_error: bool = False


@runtime_checkable
class ProgressSink(Protocol):
    """What a caller is told while a remote tool is still running.

    The MCP `notifications/progress` triple, narrowed to what this project
    needs: ``progress`` is a value the server promises will increase, ``total``
    is the end when the server knows it and ``None`` when it does not, and
    ``message`` is the line to show. A server that never notifies simply never
    calls it (ADR-069).

    A Protocol rather than a `Callable[...]` alias, and the parameter *names*
    are the reason: the SDK calls its progress callback with keywords, so a
    positional alias type-checks here and fails at the call site.
    """

    async def __call__(
        self, progress: float, total: float | None, message: str | None
    ) -> None: ...


@runtime_checkable
class MCPClientPort(Protocol):
    """The two MCP operations this adapter needs after connection."""

    async def list_tools_page(self, cursor: str | None) -> RemoteToolPage: ...

    async def call_tool(
        self,
        name: str,
        arguments: JsonObject,
        *,
        on_progress: ProgressSink | None = None,
    ) -> RemoteCallResult: ...


@dataclass(slots=True)
class _SDKMCPClient:
    client: Client

    async def list_tools_page(self, cursor: str | None) -> RemoteToolPage:
        page = await self.client.list_tools(cursor=cursor)
        return RemoteToolPage(
            tools=tuple(_remote_tool_definition(tool) for tool in page.tools),
            next_cursor=page.next_cursor,
        )

    async def call_tool(
        self,
        name: str,
        arguments: JsonObject,
        *,
        on_progress: ProgressSink | None = None,
    ) -> RemoteCallResult:
        # Passing the callback is also what makes the client *send* a progress
        # token: without one the SDK omits `_meta.progressToken`, and a server
        # that would have notified has nothing to notify against. So this
        # argument does not merely receive progress, it asks for it.
        result = await self.client.call_tool(
            name, arguments, progress_callback=on_progress
        )
        return RemoteCallResult(
            content=tuple(_content_block(block) for block in result.content),
            structured_content=result.structured_content,
            is_error=result.is_error,
        )


def _remote_tool_definition(tool: types.Tool) -> RemoteToolDefinition:
    """Project one SDK tool, including legacy task-support metadata."""

    return RemoteToolDefinition(
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema,
        task_support=(
            tool.execution.task_support if tool.execution is not None else None
        ),
    )


def _content_block(block: types.ContentBlock) -> RemoteContentBlock:
    if isinstance(block, types.TextContent):
        return RemoteTextBlock(text=block.text)
    if isinstance(block, types.ImageContent):
        return RemoteBinaryBlock(
            data=base64.b64decode(block.data, validate=True),
            media_type=block.mime_type,
            kind="image",
        )
    if isinstance(block, types.AudioContent):
        return RemoteBinaryBlock(
            data=base64.b64decode(block.data, validate=True),
            media_type=block.mime_type,
            kind="audio",
        )
    if isinstance(block, types.ResourceLink):
        return RemoteResourceLink(
            name=block.name,
            uri=str(block.uri),
            media_type=block.mime_type,
        )
    resource = block.resource
    if isinstance(resource, types.TextResourceContents):
        return RemoteTextBlock(
            text=resource.text,
            media_type=resource.mime_type,
            embedded_resource=True,
        )
    return RemoteBinaryBlock(
        data=base64.b64decode(resource.blob, validate=True),
        media_type=resource.mime_type,
        kind="resource",
    )


def is_client_fault(exc: BaseException) -> bool:
    """Whether *exc* is an SDK-boundary fault the caller may absorb.

    The SDK's Streamable HTTP transport runs inside anyio task groups, and a
    server process dying mid-call does not surface as one tidy exception.
    Measured on 2026-08-16 (demo profile, Task Worker): the connect failure
    (httpcore ``ConnectError``) arrived *together with* the transport's broken
    cleanup -- ``RuntimeError: Attempted to exit cancel scope in a different
    task than it was entered in`` -- and the scope's own ``CancelledError``,
    composed into a ``BaseExceptionGroup``.  A group holding a
    ``CancelledError`` leaf is not an ``Exception``, so the tool executor's
    handler-fault catch (``except Exception``) let it pass, and it killed the
    whole Worker process.  With the only Worker gone, the running Task's lease
    expired with no surviving process to run ``reclaim_expired`` -- the task
    showed ``running`` indefinitely.

    Absorbable, then: every plain ``Exception`` raised by the SDK call, and
    any group whose leaves mix exceptions with cancel-scope ``CancelledError``
    shrapnel.  Not absorbable: a bare ``CancelledError`` or a group of nothing
    else -- that is cooperative cancellation (run cancel, tool timeout) and
    must keep propagating -- and anything carrying ``KeyboardInterrupt`` or
    ``SystemExit``, which belong to the process, not to one tool call.
    """

    return _leaves_absorbable(exc) and not _pure_cancellation(exc)


def client_fault_types(exc: BaseException) -> tuple[str, ...]:
    """Sorted, deduplicated leaf type names of an absorbed fault.

    Only the type names cross the boundary -- same rule as
    ``ErrorInfo.from_exception``: third-party exception text is untrusted
    content of unknown provenance and must not reach events or the model.
    """

    leaves = _group_leaves(exc)
    if leaves is not None:
        names: set[str] = set()
        for leaf in leaves:
            names.update(client_fault_types(leaf))
        return tuple(sorted(names))
    return (type(exc).__name__,)


def _group_leaves(exc: BaseException) -> tuple[BaseException, ...] | None:
    # isinstance can only narrow to BaseExceptionGroup[Unknown]; the cast
    # re-parameterizes it once so the recursive walkers above stay fully
    # typed under strict pyright.
    if isinstance(exc, BaseExceptionGroup):
        return cast("BaseExceptionGroup[BaseException]", exc).exceptions
    return None


def _leaves_absorbable(exc: BaseException) -> bool:
    leaves = _group_leaves(exc)
    if leaves is not None:
        return all(_leaves_absorbable(leaf) for leaf in leaves)
    return isinstance(exc, Exception | asyncio.CancelledError)


def _pure_cancellation(exc: BaseException) -> bool:
    leaves = _group_leaves(exc)
    if leaves is not None:
        return all(_pure_cancellation(leaf) for leaf in leaves)
    return isinstance(exc, asyncio.CancelledError)


@asynccontextmanager
async def connect_mcp_client(
    endpoint: str, *, timeout_seconds: int
) -> AsyncGenerator[MCPClientPort]:
    """Open one process-owned SDK client with caching disabled.

    The official v2 Client negotiates the 2026 protocol and falls back to older
    revisions.  A URL selects Streamable HTTP; stdio is intentionally absent
    from this adapter's configuration contract.
    """

    async with Client(
        endpoint,
        read_timeout_seconds=float(timeout_seconds),
        cache=None,
    ) as client:
        yield _SDKMCPClient(client)


__all__ = [
    "MCPClientPort",
    "ProgressSink",
    "RemoteBinaryBlock",
    "RemoteCallResult",
    "RemoteContentBlock",
    "RemoteResourceLink",
    "RemoteTextBlock",
    "RemoteToolDefinition",
    "RemoteToolPage",
    "client_fault_types",
    "connect_mcp_client",
    "is_client_fault",
]
