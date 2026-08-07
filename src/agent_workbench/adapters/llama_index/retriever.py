"""The LlamaIndex retrieval path, as a ``CandidateRetrieverPort``.

What LlamaIndex owns here is real: it embeds the query through the embedding
adapter, builds the ``VectorStoreQuery`` -- top_k, mode, filters -- runs the
retriever, and hands back ``NodeWithScore``. What it does not own is everything
after that. The nodes are converted straight back into this project's own
``ScoredChunk`` and leave through a framework-neutral port, so authorization,
reranking, the cut to top_k and citation building continue to run on types that
have never heard of a retrieval framework. ADR-017 draws the line exactly here.

A retriever is built per call rather than once per process, and that is a
safety property rather than an oversight. The filters carry the asking
principal, so a retriever cached across requests would hold one principal's
narrowing and apply it to the next request that came along. The object is
cheap; a leaked narrowing is not.

The final answer is never generated here. There is no ``as_query_engine``, no
response synthesizer and no agent: those are the LlamaIndex roles
``rag.llama_index`` turns off, and the architecture guard enforces their
absence by refusing the imports outright, which is stronger than a flag this
module could read and forget to check.
"""

from __future__ import annotations

from llama_index.core import VectorStoreIndex
from llama_index.core.schema import QueryBundle
from llama_index.core.vector_stores.types import VectorStoreQueryMode

from agent_workbench.adapters.llama_index.embedding import PortBackedEmbedding
from agent_workbench.adapters.llama_index.nodes import from_node
from agent_workbench.adapters.llama_index.vector_store import (
    PortBackedVectorStore,
    build_filters,
)
from agent_workbench.ports.embedding import EmbeddingPort
from agent_workbench.ports.sparse import SparseEncoderPort
from agent_workbench.ports.vector_index import ScoredChunk, VectorIndexPort


class LlamaIndexCandidateRetriever:
    """Candidates proposed by LlamaIndex, over this project's own index."""

    def __init__(
        self,
        *,
        embedder: EmbeddingPort,
        index: VectorIndexPort,
        sparse_encoder: SparseEncoderPort | None = None,
    ) -> None:
        self._sparse_encoder = sparse_encoder
        self._store = PortBackedVectorStore(index, sparse_encoder=sparse_encoder)
        self._index = VectorStoreIndex.from_vector_store(
            self._store,
            embed_model=PortBackedEmbedding(embedder),
        )

    @property
    def mode(self) -> str:
        """The retriever's name, and it names the framework too.

        An evaluation comparing the reference path with this one has to be able
        to tell the two apart in its own output. Reporting plain "hybrid" for
        both would make the two reports look like two runs of one retriever,
        which is the shape a regression hides in.
        """

        arm = "hybrid" if self._sparse_encoder is not None else "dense"
        return f"llama_index+{arm}"

    async def candidates(
        self,
        *,
        query: str,
        tenant_id: str,
        principal_id: str,
        knowledge_base_id: str,
        limit: int,
    ) -> tuple[ScoredChunk, ...]:
        hybrid = self._sparse_encoder is not None
        retriever = self._index.as_retriever(
            similarity_top_k=limit,
            vector_store_query_mode=(
                VectorStoreQueryMode.HYBRID if hybrid else VectorStoreQueryMode.DEFAULT
            ),
            # Both are the full candidate budget. They travel to the store,
            # which deliberately does not use them to shorten either arm before
            # fusion -- see its docstring; they are set consistently so that a
            # reader of this call is not told two different numbers.
            sparse_top_k=limit,
            hybrid_top_k=limit,
            filters=build_filters(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                principal_id=principal_id,
            ),
        )
        nodes = await retriever.aretrieve(QueryBundle(query_str=query))
        return tuple(from_node(node) for node in nodes)


__all__ = ["LlamaIndexCandidateRetriever"]
