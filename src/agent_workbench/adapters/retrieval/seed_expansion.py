"""A ``CandidateRetrieverPort`` whose third arm starts where the first two end.

ADR-037 §2.7. The shape is not the four peer arms §2.1 drew, and the reason is
measured: on the cross-document questions hybrid half-answered, the bridge
entity is named in the document the other arms already found (7/7) and never
in the query (0/7). An arm embedding the query against entity names could not
reach it. An arm that starts from the found chunks can.

So the pipeline has two stages and one fusion:

    dense ─┐
           ├─► seeds ─► expand ─► graph arm ─┐
    sparse ┘                                 │
      │                                      │
      └──────────────────────────────────────┴─► one RRF ─► candidates

Fusing still happens exactly once. The seeds are used to *nominate* -- to
decide which entities to expand from -- and never to score: no arm's ranks are
combined twice, and what enters the fusion is three lists nothing has fused.
That distinction is the boundary ADR-033 drew, and it is worth stating because
the pipeline now has an arrow between two arms where before there was none.

The graph arm is best-effort *in one direction only*. A knowledge base with no
extracted graph, or one whose rows were written by another extractor, means
two arms instead of three -- a degradation, not a failure, and the ordinary
state of every knowledge base until the second pass has run over it.

The index is not optional in the same way, and this module does not pretend it
is. The dense and sparse arms have just used the same client successfully by
the time the graph arm fetches, so a failure there is a bug or an outage that
already broke the other two; catching it would turn a defect into a silently
missing arm. The first version did exactly that, and swallowed a real one.

What this cannot do is tell a caller that a particular call ran degraded.
``mode`` reports the configured shape, because ``CandidateRetrieverPort`` says
it must. So an evaluation comparing this retriever against hybrid has to
establish separately that the graph had rows to contribute -- otherwise "the
graph did not help" and "the graph was not there" produce the same numbers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

from agent_workbench.adapters.vector.fusion import fused, ranked
from agent_workbench.ports.candidates import ScoredChunk
from agent_workbench.ports.embedding import EmbeddingPort
from agent_workbench.ports.knowledge_graph import KnowledgeGraphStore
from agent_workbench.ports.sparse import SparseEncoderPort
from agent_workbench.ports.vector_index import VectorIndexPort

#: How many of the fused-so-far candidates seed the expansion. Small on
#: purpose: the measured failures had the anchor document at rank 1 every
#: time, and every extra seed spends the arm's budget expanding from a chunk
#: that was already a worse answer.
DEFAULT_SEED_COUNT: Final[int] = 3


@dataclass(frozen=True, slots=True)
class SeedExpansionRetriever:
    """Dense, sparse and a graph arm expanded from what those two found."""

    embedder: EmbeddingPort
    index: VectorIndexPort
    graph: KnowledgeGraphStore
    graph_identity: str
    sparse_encoder: SparseEncoderPort | None = None
    graph_arm_limit: int = 12
    seed_count: int = DEFAULT_SEED_COUNT

    @property
    def mode(self) -> str:
        """What is configured, in the vocabulary the evaluation reports use.

        Deliberately describes the configuration rather than the last call:
        ``CandidateRetrieverPort`` says so, and a mode that changed per query
        would make an ablation's index identity depend on which question was
        asked last.
        """

        lexical = "hybrid" if self.sparse_encoder is not None else "dense"
        return f"{lexical}+graph"

    async def candidates(
        self,
        *,
        query: str,
        tenant_id: str,
        principal_id: str,
        knowledge_base_id: str,
        limit: int,
    ) -> tuple[ScoredChunk, ...]:
        principals = (principal_id,)
        vector = await self.embedder.embed_query(query)

        arms: list[tuple[ScoredChunk, ...]] = []
        if self.sparse_encoder is None:
            arms.append(
                await self.index.search(
                    vector=vector,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    authorized_principals=principals,
                    limit=limit,
                )
            )
        else:
            weights = await self.sparse_encoder.encode_query(query)
            dense_arm, sparse_arm = await asyncio.gather(
                self.index.search(
                    vector=vector,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    authorized_principals=principals,
                    limit=limit,
                ),
                self.index.search_sparse(
                    sparse_indices=weights.indices,
                    sparse_values=weights.values,
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                    authorized_principals=principals,
                    limit=limit,
                ),
            )
            arms.extend((dense_arm, sparse_arm))

        graph_arm = await self._expanded(
            arms,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            principals=principals,
        )
        if graph_arm:
            arms.append(graph_arm)

        return fused(*arms, limit=limit)

    async def _expanded(
        self,
        arms: list[tuple[ScoredChunk, ...]],
        *,
        tenant_id: str,
        knowledge_base_id: str,
        principals: tuple[str, ...],
    ) -> tuple[ScoredChunk, ...]:
        """The graph arm, or nothing if the graph cannot contribute.

        Seeds come from each arm's own top, not from a fusion of the two. A
        fusion here would be a first RRF whose output then entered a second
        one -- the ordering no retriever produced -- and it would buy nothing:
        the seeds are a set, and which of two arms proposed a chunk does not
        change what it mentions.
        """

        seeds: list[str] = []
        for arm in arms:
            for chunk in arm[: self.seed_count]:
                if chunk.chunk_id not in seeds:
                    seeds.append(chunk.chunk_id)
        if not seeds:
            return ()

        try:
            nominations = await self.graph.expand_from_seeds(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                graph_identity=self.graph_identity,
                seed_chunk_ids=tuple(seeds),
                limit=self.graph_arm_limit,
            )
        except Exception:
            # Two arms instead of three. The graph really is optional: a
            # knowledge base whose second pass has not run has no rows here,
            # and an unmigrated deployment has no tables. Retrieval that failed
            # for either reason would be worse than the degradation.
            return ()

        if not nominations:
            return ()

        # A nomination is an id: the graph stores provenance, not payload. The
        # chunks are fetched from the index, *not* taken from the other arms'
        # output -- a chunk one of them already returned is the one case this
        # arm does not care about, so filtering to what they held would leave
        # it able to re-rank and never to reach.
        # Not guarded. The other arms just used this client; a failure here is
        # a defect or an outage that already broke them, and swallowing it
        # would turn either into an arm that quietly contributes nothing.
        found = await self.index.fetch(
            chunk_ids=tuple(n.chunk_id for n in nominations),
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            authorized_principals=principals,
        )

        # The graph's ordering, not the index's. `fetch` ranks by a score that
        # means nothing here, and the arm's opinion is how many distinct seed
        # entities reached each chunk.
        scores = {n.chunk_id: n.score for n in nominations}
        return ranked(
            chunk.model_copy(update={"score": scores[chunk.chunk_id]})
            for chunk in found
            if chunk.chunk_id in scores
        )


__all__ = ["DEFAULT_SEED_COUNT", "SeedExpansionRetriever"]
