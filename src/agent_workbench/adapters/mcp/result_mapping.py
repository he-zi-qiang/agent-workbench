"""MCP content blocks normalized into one bounded local ``ToolResult``."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from agent_workbench.adapters.mcp.client import (
    MCPClientPort,
    RemoteBinaryBlock,
    RemoteCallResult,
    RemoteTextBlock,
)
from agent_workbench.domain.artifacts import MediaType
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.identifiers import ID_MAX_LENGTH
from agent_workbench.domain.tools import ToolResult
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.tools import ToolInvocation

_MEDIA_TYPE: TypeAdapter[str] = TypeAdapter(MediaType)
_INLINE_TEXT_LIMIT_BYTES = 65_536
_WORD_DOCUMENT_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@dataclass(frozen=True, slots=True)
class _Payload:
    content: bytes
    media_type: str
    kind: str


@dataclass(frozen=True, slots=True)
class MCPToolHandler:
    """Call one remote tool and map its result under local size/owner rules."""

    client: MCPClientPort
    remote_name: str
    artifacts: ArtifactStore
    artifact_threshold_bytes: int
    max_result_bytes: int
    max_artifact_bytes: int
    # ToolSpec.exclusive is a barrier only inside one Agent run.  This lock is
    # shared by every binding from the server so concurrent Task lanes do not
    # drive one MCP session at the same time.
    server_lock: asyncio.Lock

    async def __call__(self, invocation: ToolInvocation) -> ToolResult:
        invocation.cancellation.raise_if_cancelled()
        async with self.server_lock:
            remote = await self.client.call_tool(
                self.remote_name, invocation.call.arguments
            )
        invocation.cancellation.raise_if_cancelled()
        return await map_remote_result(
            invocation,
            remote,
            artifacts=self.artifacts,
            artifact_threshold_bytes=self.artifact_threshold_bytes,
            max_result_bytes=self.max_result_bytes,
            max_artifact_bytes=self.max_artifact_bytes,
        )


async def map_remote_result(
    invocation: ToolInvocation,
    remote: RemoteCallResult,
    *,
    artifacts: ArtifactStore,
    artifact_threshold_bytes: int,
    max_result_bytes: int,
    max_artifact_bytes: int,
) -> ToolResult:
    """Preserve text and resources without exceeding the one-artifact contract.

    Text blocks stay in model context until the configured inline threshold.
    Embedded binary/image/audio blocks become artifacts.  If a result contains
    several artifact-shaped blocks, they are placed in one deterministic ZIP;
    the domain contract still carries exactly one ``ArtifactRef`` and no block
    is silently discarded.  Resource links are rendered as text and are never
    fetched by the adapter.
    """

    text_parts: list[str] = []
    payloads: list[_Payload] = []
    total_bytes = 0

    for block in remote.content:
        if isinstance(block, RemoteTextBlock):
            encoded = block.text.encode("utf-8")
            total_bytes += len(encoded)
            if block.embedded_resource and len(encoded) > artifact_threshold_bytes:
                payloads.append(
                    _Payload(
                        content=encoded,
                        media_type=_safe_media_type(block.media_type, "text/plain"),
                        kind="text_resource",
                    )
                )
            else:
                text_parts.append(block.text)
        elif isinstance(block, RemoteBinaryBlock):
            total_bytes += len(block.data)
            payloads.append(
                _Payload(
                    content=block.data,
                    media_type=_safe_media_type(
                        block.media_type, "application/octet-stream"
                    ),
                    kind=block.kind,
                )
            )
        else:
            rendered = f"Resource {block.name}: {block.uri}"
            total_bytes += len(rendered.encode("utf-8"))
            text_parts.append(rendered)

    # The model-facing projection joins each text-shaped block with one byte
    # that did not arrive from the server. Count those separators before the
    # untrusted-result ceiling; otherwise a directory of empty blocks could
    # grow output while contributing zero to the limit.
    total_bytes += max(0, len(text_parts) - 1)

    try:
        structured = _structured_bytes(remote.structured_content)
    except (TypeError, ValueError):
        # Streamable HTTP can only carry JSON, but the project-owned port also
        # makes malformed in-memory adapters testable.  Keep that malformed
        # value (and its repr) out of the ToolResult/error surface.
        return ToolResult.failed(
            invocation.call,
            ErrorInfo(
                code="tool_failed",
                message="the MCP server returned invalid structured content",
            ),
        )
    total_bytes += len(structured)
    # Modern servers often provide the same value in text and structured form.
    # Use structured content as a fallback rather than duplicating it into the
    # model context, while still counting it against the untrusted response cap.
    if structured and not remote.content:
        text_parts.append(structured.decode("utf-8"))

    if total_bytes > max_result_bytes:
        return ToolResult.failed(
            invocation.call,
            ErrorInfo(
                code="output_too_large",
                message="the MCP tool result exceeds the configured size ceiling",
            ),
        )
    if remote.is_error:
        return ToolResult.failed(
            invocation.call,
            ErrorInfo(
                code="tool_failed",
                message="the MCP server reported that the tool call failed",
            ),
        )

    inline = "\n".join(text_parts)
    inline_bytes = inline.encode("utf-8")
    inline_limit = min(artifact_threshold_bytes, _INLINE_TEXT_LIMIT_BYTES)
    # A result that already needs an artifact also appends a summary to inline
    # content.  Reserve the largest possible identifier plus that fixed text
    # before writing anything, otherwise a 65,536-byte text block followed by
    # one binary block would create an orphan artifact and then fail Pydantic's
    # 65,536-character ToolResult.content bound.
    summary_reserve = _artifact_summary_reserve(len(payloads) + 1)
    if len(inline_bytes) > inline_limit or (
        payloads and inline_bytes and len(inline_bytes) + summary_reserve > inline_limit
    ):
        payloads.insert(
            0,
            _Payload(
                content=inline_bytes,
                media_type="text/plain",
                kind="text",
            ),
        )
        inline = ""

    reference = None
    if payloads:
        content, media_type, filename = _artifact_bytes(payloads)
        if len(content) > max_artifact_bytes:
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="output_too_large",
                    message="the normalized MCP artifact exceeds the storage ceiling",
                ),
            )
        invocation.cancellation.raise_if_cancelled()
        principal = invocation.context.principal
        reference = await artifacts.put(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            kind="tool_result",
            media_type=media_type,
            content=content,
            filename=filename,
        )
        summary = (
            f"MCP result stored as {reference.artifact_id} "
            f"({len(payloads)} content block(s))"
        )
        inline = f"{inline}\n{summary}".strip()

    if not inline:
        inline = "MCP tool completed without inline content"
    return ToolResult.succeeded(
        invocation.call,
        content=inline,
        artifact=reference,
    )


def _safe_media_type(value: str | None, fallback: str) -> str:
    if value is None:
        return fallback
    try:
        return _MEDIA_TYPE.validate_python(value)
    except ValidationError:
        return fallback


def _structured_bytes(value: object | None) -> bytes:
    if value is None:
        return b""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _artifact_summary_reserve(payload_count: int) -> int:
    placeholder = "x" * ID_MAX_LENGTH
    return len(
        f"\nMCP result stored as {placeholder} "
        f"({payload_count} content block(s))".encode()
    )


def _artifact_bytes(payloads: list[_Payload]) -> tuple[bytes, str, str]:
    if len(payloads) == 1:
        payload = payloads[0]
        if payload.media_type == _WORD_DOCUMENT_MEDIA_TYPE:
            suffix = ".docx"
        else:
            suffix = ".txt" if payload.media_type.startswith("text/") else ".bin"
        return payload.content, payload.media_type, f"mcp-result{suffix}"

    stream = io.BytesIO()
    manifest = [
        {
            "file": f"part-{index:03d}.bin",
            "kind": payload.kind,
            "media_type": payload.media_type,
            "size_bytes": len(payload.content),
        }
        for index, payload in enumerate(payloads, start=1)
    ]
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
        _zip_write(
            bundle,
            "manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        for index, payload in enumerate(payloads, start=1):
            _zip_write(bundle, f"part-{index:03d}.bin", payload.content)
    return stream.getvalue(), "application/zip", "mcp-result.zip"


def _zip_write(bundle: zipfile.ZipFile, name: str, content: bytes) -> None:
    # ZipInfo defaults to 1980-01-01. Set every other metadata bit explicitly so
    # the same remote result produces byte-identical artifact content on retry.
    info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    bundle.writestr(info, content)


__all__ = ["MCPToolHandler", "map_remote_result"]
