"""Searching the knowledge base, without asking a model to talk about it.

Retrieval had no product surface. It ran inside a chat turn or inside the
evaluation script, and nowhere else -- so a deployment with no model provider
could upload a document, watch the index grow, and have no way to ask what was
in it. The vectors were there and unreachable.

The authorization story here is different from Task and Approval, and simpler.
There is no id to probe: a caller searches as whoever the request resolved to,
and the passages that come back are the ones PostgreSQL says that principal may
read. ``knowledge_base_id`` narrows where to look and grants nothing -- looking
somewhere you may not read returns nothing rather than an error, because "no
readable passages matched" is the honest answer to both "there is nothing there"
and "there is nothing there *for you*", and those two must not be
distinguishable.

What comes back is the retrieval packet, which is what the fixed chat path would
have put in front of a model. That is the point: this endpoint answers "what
would the model have been given", so a corpus can be inspected and a retrieval
quality problem told apart from a generation one.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.application.retrieval import RetrievalRequest
from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.context import Citation
from agent_workbench.domain.identifiers import Identifier

SEARCH_PREFIX = "/v1/search"

MAX_QUERY_LENGTH = 4096
MAX_TOP_K = 50


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    knowledge_base_id: Identifier
    top_k: int = Field(default=8, ge=1, le=MAX_TOP_K)


class SearchHit(BaseModel):
    """One passage, and enough to find it again.

    The tenant is absent even though the chunk carries one: the caller's own
    tenant is the only one that can appear here, so echoing it back adds nothing
    and makes every response a place identity could leak from.
    """

    chunk_id: Identifier
    document_id: Identifier
    document_version: str
    text: str


class SearchResponse(BaseModel):
    """What the model would have been shown, had one been asked."""

    hits: tuple[SearchHit, ...]
    citations: tuple[Citation, ...]
    #: Which retriever answered -- "dense", "hybrid", "hybrid+rerank". Reported
    #: because a result set means something different under each, and an
    #: evaluation that could not tell them apart would credit the wrong one.
    retriever: str


class SearchUnavailableError(RuntimeError):
    """This process assembled no retrieval stack, so it cannot search."""


router = APIRouter(prefix=SEARCH_PREFIX, tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request) -> SearchResponse:
    dependencies = dependencies_of(request)
    retrieval = dependencies.retrieval
    if retrieval is None:  # pragma: no cover - the router is not mounted then
        raise SearchUnavailableError("this process assembled no retrieval stack")

    principal = dependencies.principals.resolve(request)
    context = await retrieval.retrieve(
        RetrievalRequest(
            query=body.query,
            # From the resolved principal, never from the body. A caller that
            # could name whose documents to search would be choosing its own
            # permissions, which is the same rule the knowledge_search tool
            # follows and for the same reason.
            #
            # This and `extra="forbid"` above are a redundant pair, and a
            # sabotage round established which one carries the weight: the
            # schema does. Reading these from the body instead is unobservable,
            # because the body cannot have them -- so removing this alone fails
            # nothing, while removing the schema guard fails a test. Kept as the
            # direct statement of intent, and it becomes load-bearing the day
            # the request model grows a field somebody thought was harmless.
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            knowledge_base_id=body.knowledge_base_id,
            top_k=body.top_k,
        )
    )
    return SearchResponse(
        hits=tuple(
            SearchHit(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_version=chunk.document_version,
                text=chunk.text,
            )
            for chunk in context.packet.chunks
        ),
        citations=context.packet.citations,
        retriever=retrieval.mode,
    )


__all__ = ["SEARCH_PREFIX", "SearchUnavailableError", "router"]
