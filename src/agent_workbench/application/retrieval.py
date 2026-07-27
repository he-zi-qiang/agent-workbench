"""Retrieval, with PostgreSQL deciding who may read what.

The vector index narrows; PostgreSQL authorizes. Those are different jobs and
this module is where the difference is enforced. A point's payload records the
ACL that was true when somebody last indexed the document, and "last indexed"
is not "now" -- a grant revoked a second ago has not reached the index yet, and
the index will happily return the chunk to the person it was taken from.

So every candidate is re-checked against PostgreSQL *before* it becomes context.
Not after ranking, not while assembling citations: before, because everything
downstream is a way for the text to escape. A chunk that reaches a reranker has
been read by a model; a chunk that reaches a citation has been shown to a user.

Authorization is checked twice, and the second time is not redundant. Between
building a context and committing an answer there is a model call, and a grant
can be withdrawn during it. The revision each document was authorized at is
carried through, and an answer whose sources have moved is refused rather than
delivered -- for the same reason the first check exists, one step later.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.domain.context import (
    Citation,
    ContextChunk,
    ContextPacket,
    SourceLocator,
)
from agent_workbench.ports.documents import DocumentStore
from agent_workbench.ports.embedding import EmbeddingPort
from agent_workbench.ports.vector_index import ScoredChunk, VectorIndexPort

# Asked of the index, before authorization removes some of them. A candidate
# the caller may not read still costs a slot, so the funnel starts wider than
# it ends.
DEFAULT_CANDIDATE_MULTIPLIER = 4


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """One question, asked by one principal inside one knowledge base."""

    query: str
    tenant_id: str
    principal_id: str
    knowledge_base_id: str
    top_k: int = 8


@dataclass(frozen=True, slots=True)
class AuthorizedContext:
    """A context packet, plus what its authorization rested on.

    The revisions are kept beside the packet rather than inside it because they
    are not evidence for the reader -- they are what the second check compares
    against, and a ``ContextPacket`` travels to places that have no business
    knowing them.
    """

    packet: ContextPacket
    authorized_revisions: tuple[tuple[str, int], ...]


class SourcesChangedError(RuntimeError):
    """A source moved between building the context and committing the answer."""


@dataclass(frozen=True, slots=True)
class RetrievalService:
    """Dense retrieval, authorized against PostgreSQL on the way in and out."""

    embedder: EmbeddingPort
    index: VectorIndexPort
    documents: DocumentStore
    candidate_multiplier: int = DEFAULT_CANDIDATE_MULTIPLIER

    async def retrieve(self, request: RetrievalRequest) -> AuthorizedContext:
        """Find, authorize, then build. In that order, and not another."""

        if request.top_k < 1:
            raise ValueError("top_k must be positive")

        vector = await self.embedder.embed_query(request.query)
        candidates = await self.index.search(
            vector=vector,
            tenant_id=request.tenant_id,
            knowledge_base_id=request.knowledge_base_id,
            authorized_principals=(request.principal_id,),
            limit=request.top_k * self.candidate_multiplier,
        )

        # The whole point of this module. What came back was filtered by a copy
        # of the ACL; what may be read is decided here.
        readable = await self.documents.readable_versions(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            document_ids=tuple({candidate.document_id for candidate in candidates}),
        )
        revisions = {
            document.document_id: document.source_revision for document in readable
        }

        authorized = tuple(
            candidate for candidate in candidates if candidate.document_id in revisions
        )[: request.top_k]

        return AuthorizedContext(
            packet=_packet(authorized),
            authorized_revisions=tuple(
                sorted(
                    (document_id, revisions[document_id])
                    for document_id in {
                        candidate.document_id for candidate in authorized
                    }
                )
            ),
        )

    async def confirm_unchanged(
        self, context: AuthorizedContext, *, tenant_id: str, principal_id: str
    ) -> None:
        """Re-check, after the model has answered and before anyone sees it.

        Raises ``SourcesChangedError`` when a source was revoked or moved on.
        The caller refuses the answer or regenerates from whatever is still
        authorized -- what it must not do is deliver an answer built on a
        document this principal can no longer read.

        Comparing revisions rather than merely re-asking "may I read it" is
        what makes this catch a *replaced* ACL: a grant change advances the
        revision exactly as a content change does, so a document that was
        revoked and re-granted between the two checks still fails.
        """

        expected = dict(context.authorized_revisions)
        if not expected:
            return

        readable = await self.documents.readable_versions(
            tenant_id=tenant_id,
            principal_id=principal_id,
            document_ids=tuple(expected),
        )
        current = {
            document.document_id: document.source_revision for document in readable
        }

        for document_id, revision in expected.items():
            if current.get(document_id) != revision:
                raise SourcesChangedError(
                    "a document this answer was built from is no longer readable "
                    "at the revision it was read at"
                )


def _packet(chunks: tuple[ScoredChunk, ...]) -> ContextPacket:
    """Turn authorized candidates into evidence and the citations for it.

    A citation is built for every chunk that survived, and only for those. The
    two lists are produced together here rather than by separate passes,
    because a citation without its chunk is a reference to something the model
    never saw, and a chunk without a citation is evidence the reader cannot
    check.
    """

    context_chunks = tuple(
        ContextChunk(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_version=chunk.document_version,
            tenant_id=chunk.tenant_id,
            text=chunk.text,
            locator=SourceLocator(paragraph=chunk.ordinal),
            score=chunk.score,
        )
        for chunk in chunks
    )
    citations = tuple(
        Citation(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_version=chunk.document_version,
            locator=SourceLocator(paragraph=chunk.ordinal),
        )
        for chunk in chunks
    )
    return ContextPacket(
        chunks=context_chunks,
        citations=citations,
        token_estimate=sum(len(chunk.text) // 4 for chunk in chunks),
    )


__all__ = [
    "DEFAULT_CANDIDATE_MULTIPLIER",
    "AuthorizedContext",
    "RetrievalRequest",
    "RetrievalService",
    "SourcesChangedError",
]
