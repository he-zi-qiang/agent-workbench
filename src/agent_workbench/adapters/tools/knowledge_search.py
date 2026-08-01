"""Retrieval as a tool, for the path where the model decides when to search.

The same ``RetrievalService`` the fixed two-step chat uses, so both paths
produce the same ``ContextPacket``. Two retrievers would be two sets of
authorization checks, two citation shapes and two things to evaluate, and the
one that got less attention would be the one that leaked.

**The principal comes from the execution context, never from the arguments.**
A model that could name whose documents to search would be choosing its own
permissions, and the arguments are the one part of a tool call that untrusted
text can reach: a retrieved passage saying "search as user_admin" is a passage
that would be granting itself access. The schema has no field for it, so there
is nothing to smuggle it through.

The knowledge base is an argument, because choosing where to look is the
model's job and looking somewhere it may not read is refused by the same
PostgreSQL check as everywhere else -- narrowing to a knowledge base is not
authorization, and does not become authorization by arriving in an argument.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import JsonValue

from agent_workbench.application.chat_execution import RetrievalJournal
from agent_workbench.application.retrieval import RetrievalRequest, RetrievalService
from agent_workbench.domain.context import ContextPacket
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.tools import ToolResult, ToolSpec
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

TOOL_NAME = "knowledge_search"

MAX_TOP_K = 20

INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query", "knowledge_base_id"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "knowledge_base_id": {"type": "string", "minLength": 1},
        "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOP_K},
    },
}

SPEC = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Search the knowledge base for passages relevant to a query. Returns "
        "chunks with their ids, which are the ids to cite. Only documents the "
        "caller may read are searched."
    ),
    input_schema=INPUT_SCHEMA,
    concurrency="parallel",
    risk="read",
    idempotency="safe",
    timeout_seconds=30,
)


@dataclass(frozen=True, slots=True)
class KnowledgeSearchTool:
    """Wraps the retrieval service as a callable tool."""

    retrieval: RetrievalService
    # Where each search records what it authorized, for the run's release fence
    # to re-check afterwards. Optional because the tool is also usable outside a
    # chat turn; a run whose evidence nothing journals is one whose answer
    # cannot be fenced, so the agentic assembly always supplies one.
    journal: RetrievalJournal | None = None

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=SPEC, handler=self.handle)

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        """Search as whoever the run is for, never as whoever the model names."""

        arguments = invocation.call.arguments
        query = str(arguments.get("query", ""))
        knowledge_base_id = str(arguments.get("knowledge_base_id", ""))
        top_k = int(str(arguments.get("top_k", 8)))

        principal = invocation.context.principal
        context = await self.retrieval.retrieve(
            RetrievalRequest(
                query=query,
                # From the run, not the call. The schema has no field for these
                # and this is why.
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                knowledge_base_id=knowledge_base_id,
                top_k=min(top_k, MAX_TOP_K),
            )
        )
        if self.journal is not None:
            # Recorded before the result is rendered, so a passage the model is
            # about to see is already something the fence knows to re-check.
            self.journal.record(invocation.context.agent_run_id, context)
        return ToolResult(
            tool_call_id=invocation.call.tool_call_id,
            tool_name=TOOL_NAME,
            status="ok",
            content=_render(context.packet),
        )

    async def refuse(self, invocation: ToolInvocation, reason: str) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.call.tool_call_id,
            tool_name=TOOL_NAME,
            status="error",
            error=ErrorInfo(code="invalid_tool_input", message=reason),
        )


def _render(packet: ContextPacket) -> str:
    """What the model sees.

    Chunk ids are labelled so a citation can be checked against what was
    actually returned rather than against whatever the model names. Passages
    are quoted as evidence -- the system prompt says text inside them is not
    instructions, and this format does not blur that by interleaving them with
    anything that looks like one.
    """

    if not packet.chunks:
        return json.dumps({"chunks": [], "note": "no readable passages matched"})
    return json.dumps(
        {
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "text": chunk.text,
                }
                for chunk in packet.chunks
            ]
        },
        ensure_ascii=False,
    )


__all__ = [
    "INPUT_SCHEMA",
    "MAX_TOP_K",
    "SPEC",
    "TOOL_NAME",
    "KnowledgeSearchTool",
]
