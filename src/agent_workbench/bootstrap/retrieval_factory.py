"""Choosing which retriever proposes candidates, in one place.

Two processes retrieve -- the API and the Task worker -- and before this
existed each assembled its own funnel inline. That was survivable while there
was one path; with two it is how a deployment ends up serving Chat from
LlamaIndex and Task research from the reference adapter, then comparing the
two and attributing the difference to something else entirely.

The switch is ``rag.llama_index.enabled``. Until this factory, that field had
no reader anywhere in ``src``: the configuration described a framework
integration the process did not have, and turning it off would have changed
nothing. A flag with no consumer is worse than a missing flag, because it reads
like a decision that was implemented.
"""

from __future__ import annotations

from agent_workbench.adapters.llama_index import LlamaIndexCandidateRetriever
from agent_workbench.adapters.retrieval import ReferenceVectorIndexRetriever
from agent_workbench.ports.candidates import CandidateRetrieverPort
from agent_workbench.ports.embedding import EmbeddingPort
from agent_workbench.ports.sparse import SparseEncoderPort
from agent_workbench.ports.vector_index import VectorIndexPort


def build_candidate_retriever(
    *,
    llama_index_enabled: bool,
    embedder: EmbeddingPort,
    index: VectorIndexPort,
    sparse_encoder: SparseEncoderPort | None = None,
    dense_top_k: int | None = None,
    sparse_top_k: int | None = None,
) -> CandidateRetrieverPort:
    """Assemble the configured retriever over one index and one embedder.

    Both paths are handed the *same* index, embedder and sparse encoder. That
    is what makes the two comparable: a difference between their evaluation
    reports is a difference in retrieval, not in what was indexed, which model
    embedded it, or whether a lexical arm was available at all.

    The per-arm ceilings go only to the reference path, and that does not break
    the sentence above (ADR-097 §3.2). The LlamaIndex path forwards its own
    `sparse_top_k`/`hybrid_top_k` to the store, which deliberately does not use
    them to shorten either arm before fusion -- so with the shipped values,
    where the two arms and the fused budget are all equal, both paths ask for
    the same candidates. A deployment that sets the arms *unequal* is choosing
    a retriever the other path cannot mirror, and should not then read an
    equivalence report as if it compared like with like.
    """

    if llama_index_enabled:
        return LlamaIndexCandidateRetriever(
            embedder=embedder, index=index, sparse_encoder=sparse_encoder
        )
    return ReferenceVectorIndexRetriever(
        embedder=embedder,
        index=index,
        sparse_encoder=sparse_encoder,
        dense_top_k=dense_top_k,
        sparse_top_k=sparse_top_k,
    )


__all__ = ["build_candidate_retriever"]
