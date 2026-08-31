"""The pre-ADR-017 retrieval path, kept as a measurable baseline.

This is the code that used to live inside ``RetrievalService._candidates``,
moved behind ``CandidateRetrieverPort`` unchanged. Extracting it is what makes
the LlamaIndex path comparable: both implementations now answer the same
question through the same protocol, over the same index, so a difference
between two evaluation reports is a difference in retrieval rather than in
what was measured or how.

It is a **reference** adapter, and the name is the point. ADR-017 assigns
ingestion and retrieval to LlamaIndex; this path exists to answer "did the
framework change the answers?", not to be a second thing that claims to be
production. Two live paths that both call themselves production is the state
that ADR-017's migration rules exist to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.ports.embedding import EmbeddingPort
from agent_workbench.ports.sparse import SparseEncoderPort
from agent_workbench.ports.vector_index import ScoredChunk, VectorIndexPort


@dataclass(frozen=True, slots=True)
class ReferenceVectorIndexRetriever:
    """Embed the query, then ask the index -- by whichever arms exist."""

    embedder: EmbeddingPort
    index: VectorIndexPort
    # Absent when the process has no sparse runtime. Retrieval then uses the
    # dense arm alone -- which is a different retriever, not a degraded one,
    # and ``mode`` says so.
    sparse_encoder: SparseEncoderPort | None = None
    # Per-arm ceilings from `[rag.retrieval]` (ADR-097). They live here rather
    # than on `CandidateRetrieverPort` because "how much does each arm ask for"
    # is a question only a hybrid retriever has: the dense-only path has one
    # arm, and the port's `limit` means "how many after fusion" for every
    # implementation. Widening the port would have made the other two answer a
    # question they do not have.
    #
    # ``None`` keeps the historical behaviour -- both arms get the caller's
    # ``limit`` -- so every in-memory double and contract test constructs this
    # exactly as before.
    dense_top_k: int | None = None
    sparse_top_k: int | None = None

    @property
    def mode(self) -> str:
        return "hybrid" if self.sparse_encoder is not None else "dense"

    async def candidates(
        self,
        *,
        query: str,
        tenant_id: str,
        principal_id: str,
        knowledge_base_id: str,
        limit: int,
    ) -> tuple[ScoredChunk, ...]:
        vector = await self.embedder.embed_query(query)
        if self.sparse_encoder is None:
            return await self.index.search(
                vector=vector,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                authorized_principals=(principal_id,),
                limit=limit,
            )

        weights = await self.sparse_encoder.encode_query(query)
        return await self.index.search_hybrid(
            vector=vector,
            sparse_indices=weights.indices,
            sparse_values=weights.values,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            authorized_principals=(principal_id,),
            limit=limit,
            # Each arm proposes a full candidate set; RRF is what narrows them
            # to one. Halving them here would make fusion choose between two
            # already-truncated lists, which is a different retriever from the
            # one being evaluated. Configured arms keep that property: the
            # shipped `dense_top_k` and `sparse_top_k` are equal, and a
            # deployment that makes them unequal is choosing a different
            # retriever on purpose rather than by arithmetic.
            dense_limit=self.dense_top_k if self.dense_top_k is not None else limit,
            sparse_limit=(
                self.sparse_top_k if self.sparse_top_k is not None else limit
            ),
        )


__all__ = ["ReferenceVectorIndexRetriever"]
