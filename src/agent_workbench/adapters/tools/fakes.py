"""Two side-effect-free tools.

The walking skeleton needs tools that prove the protocol without proving
anything about the outside world, so both are pure: one reads from an in-memory
corpus supplied at construction, the other computes statistics over its own
argument. Neither touches the filesystem, the network or a clock, which is what
makes a run reproducible byte for byte.

They also exercise the two failure paths a handler owns: a lookup that finds
nothing, and an argument that is not the type the schema promised.
"""

from __future__ import annotations

from collections.abc import Mapping

from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.tools import ToolResult, ToolSpec
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

READ_DOCUMENT_SPEC = ToolSpec(
    name="read_document",
    description="Return the full text of one document from the local corpus.",
    input_schema={
        "type": "object",
        "properties": {"document_id": {"type": "string"}},
        "required": ["document_id"],
        "additionalProperties": False,
    },
    concurrency="parallel",
    risk="read",
    idempotency="safe",
    timeout_seconds=5,
)

TEXT_STATISTICS_SPEC = ToolSpec(
    name="text_statistics",
    description="Count characters, words and lines in the supplied text.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    concurrency="parallel",
    risk="read",
    idempotency="safe",
    timeout_seconds=5,
)


def read_document_tool(corpus: Mapping[str, str]) -> ToolBinding:
    """Bind the read tool to a fixed corpus."""

    documents = dict(corpus)

    async def handler(invocation: ToolInvocation) -> ToolResult:
        invocation.cancellation.raise_if_cancelled()
        document_id = invocation.call.arguments.get("document_id")
        if not isinstance(document_id, str):
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message="document_id must be a string",
                ),
            )
        text = documents.get(document_id)
        if text is None:
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(code="not_found", message=f"no document {document_id}"),
            )
        return ToolResult.succeeded(invocation.call, content=text)

    return ToolBinding(spec=READ_DOCUMENT_SPEC, handler=handler)


def text_statistics_tool() -> ToolBinding:
    """Bind the deterministic transform tool."""

    async def handler(invocation: ToolInvocation) -> ToolResult:
        invocation.cancellation.raise_if_cancelled()
        text = invocation.call.arguments.get("text")
        if not isinstance(text, str):
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(code="invalid_tool_input", message="text must be a string"),
            )
        characters = len(text)
        words = len(text.split())
        lines = len(text.splitlines()) or (1 if text else 0)
        return ToolResult.succeeded(
            invocation.call,
            content=f"characters={characters} words={words} lines={lines}",
        )

    return ToolBinding(spec=TEXT_STATISTICS_SPEC, handler=handler)


__all__ = [
    "READ_DOCUMENT_SPEC",
    "TEXT_STATISTICS_SPEC",
    "read_document_tool",
    "text_statistics_tool",
]
