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

**What is journalled is what was rendered.** A search can retrieve more than
one tool result may carry, and the passages over the budget are dropped. The
journal is the run's account of the evidence its answer may rest on, so it
records the passages that reached the model and not the ones retrieval
proposed. Recording the wider set made the citation fence accept a citation to
a passage nobody was shown -- an id the model produced rather than read,
returned to the asker as a source with this system's authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import JsonValue

from agent_workbench.application.chat_execution import RetrievalJournal
from agent_workbench.application.retrieval import (
    AuthorizedContext,
    RetrievalRequest,
    RetrievalService,
)
from agent_workbench.domain.context import ContextChunk, ContextPacket
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.tools import ToolResult, ToolSpec
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

TOOL_NAME = "knowledge_search"

MAX_TOP_K = 20

#: How much evidence one search may hand the model.
#:
#: Below ``ToolOutputText``'s ceiling on purpose: that one is the backstop for a
#: tool with no budget of its own, and this is the budget. Sized for what the
#: tool actually returns -- ``MAX_TOP_K`` passages of this project's 512-token
#: chunks -- so an ordinary result fits and a pathological one is cut rather
#: than refused.
#:
#: Deliberately above ``ChunkText``'s own 32,768 ceiling, which is what makes
#: "one passage on its own does not fit" unreachable: a single chunk cannot be
#: larger than that bound, so at least one always survives. A test pins the
#: relationship, because it is the reason the empty branch below is dead and
#: lowering this constant would quietly bring it back to life.
MAX_CONTENT_CHARS = 48_000

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
        rendered = _render(context.packet)
        if self.journal is not None:
            # What was rendered, not what was retrieved. The journal is the
            # run's account of the evidence its answer may rest on, and a
            # passage this result dropped to fit its budget is one the model
            # never saw -- recording it would let a citation to it verify, and
            # would fence a document the answer could not have been built from.
            self.journal.record(
                invocation.context.agent_run_id, _as_shown(context, rendered.shown)
            )
        return ToolResult(
            tool_call_id=invocation.call.tool_call_id,
            tool_name=TOOL_NAME,
            status="ok",
            content=rendered.text,
        )

    async def refuse(self, invocation: ToolInvocation, reason: str) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.call.tool_call_id,
            tool_name=TOOL_NAME,
            status="error",
            error=ErrorInfo(code="invalid_tool_input", message=reason),
        )


@dataclass(frozen=True, slots=True)
class _Rendered:
    """One search result, and which passages it actually consists of.

    The two are returned together rather than recomputed by the caller, because
    the whole point is that they cannot disagree: ``shown`` is what ``text``
    contains, so nothing downstream has to reason about the budget a second
    time to know what the model saw.
    """

    text: str
    shown: tuple[ContextChunk, ...]


def _render(packet: ContextPacket) -> _Rendered:
    """What the model sees, bounded by what a tool result may carry.

    Chunk ids are labelled so a citation can be checked against what was
    actually returned rather than against whatever the model names. Passages
    are quoted as evidence -- the system prompt says text inside them is not
    instructions, and this format does not blur that by interleaving them with
    anything that looks like one.

    Passages are dropped whole, highest-ranked first, and never clipped. A
    half-passage is evidence that says something the document does not, and the
    model would cite it under the id of the whole thing -- the citation fence
    checks that a cited chunk was shown, not that the sentence relied on
    survived the cut. Dropping loses evidence; clipping invents it.

    What was dropped is reported rather than left to be inferred from a count
    the model never saw. A model that knows its evidence is partial can search
    again with a narrower query; one that does not will answer as if it had
    everything.

    A dropped passage leaves this function in ``shown`` as well as out of
    ``text``. It is the only place that knows which passages survived the
    budget, and the fence downstream is only worth anything if it is checking
    that list rather than the one retrieval proposed.
    """

    if not packet.chunks:
        return _Rendered(
            text=json.dumps({"chunks": [], "note": "no readable passages matched"}),
            shown=(),
        )

    kept: list[dict[str, str]] = []
    shown: list[ContextChunk] = []
    for chunk in packet.chunks:
        entry = {
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "text": chunk.text,
        }
        if len(_encode({"chunks": [*kept, entry]})) > MAX_CONTENT_CHARS:
            break
        kept.append(entry)
        shown.append(chunk)

    omitted = len(packet.chunks) - len(kept)
    if not omitted:
        return _Rendered(text=_encode({"chunks": kept}), shown=tuple(shown))
    if not kept:  # pragma: no cover - MAX_CONTENT_CHARS exceeds ChunkText's own
        # ceiling, so the first passage always fits. Kept as the answer if
        # either bound moves: there would be nothing to return that is not a
        # fragment, and saying so beats returning one.
        return _Rendered(
            text=_encode(
                {
                    "chunks": [],
                    "note": (
                        "the highest-ranked passage alone exceeds this tool's "
                        "result budget; narrow the query or lower top_k"
                    ),
                }
            ),
            shown=(),
        )
    return _Rendered(
        text=_encode(
            {
                "chunks": kept,
                "note": (
                    f"{omitted} lower-ranked passage(s) omitted to fit this "
                    "tool's result budget; narrow the query or lower top_k to "
                    "see them"
                ),
            }
        ),
        shown=tuple(shown),
    )


def _as_shown(
    context: AuthorizedContext, shown: tuple[ContextChunk, ...]
) -> AuthorizedContext:
    """The search, narrowed to the passages that reached the model.

    Both halves of the journal entry narrow, and for the same reason. The
    citations narrow because a cited chunk the model was never shown is a chunk
    id it produced rather than read, and the fence's whole job is to refuse
    those -- an id that survived only because retrieval proposed it would be
    presented to the asker as a source with this system's authority.

    The authorized revisions narrow because they are what the release fence
    re-checks, and it asks whether the asker may still read what the answer was
    *built from*. A passage that never entered the prompt was not built from,
    so keeping it would refuse good answers whenever an unshown document's
    permissions moved -- fencing something this run never exposed.
    """

    if len(shown) == len(context.packet.chunks):
        return context

    shown_ids = {chunk.chunk_id for chunk in shown}
    shown_documents = {chunk.document_id for chunk in shown}
    return AuthorizedContext(
        packet=ContextPacket(
            chunks=shown,
            citations=tuple(
                citation
                for citation in context.packet.citations
                if citation.chunk_id in shown_ids
            ),
            retrieval_trace_id=context.packet.retrieval_trace_id,
            token_estimate=sum(len(chunk.text) // 4 for chunk in shown),
        ),
        authorized_revisions=tuple(
            (document_id, revision)
            for document_id, revision in context.authorized_revisions
            if document_id in shown_documents
        ),
        reranked=context.reranked,
    )


def _encode(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "INPUT_SCHEMA",
    "MAX_CONTENT_CHARS",
    "MAX_TOP_K",
    "SPEC",
    "TOOL_NAME",
    "KnowledgeSearchTool",
]
