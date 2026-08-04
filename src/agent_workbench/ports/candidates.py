"""Proposing candidates: the one job a retrieval framework is allowed to have.

Retrieval in this system is three steps that must stay separable -- propose,
authorize, present. This port is the first of them and nothing else. What comes
back is a *proposal*: chunks some retriever thinks are relevant, filtered by a
copy of an ACL that was true when somebody last indexed them. It is not an
answer, it is not authorized, and it is not what anyone will be shown.

The separation is what makes ADR-017 implementable. LlamaIndex may own query
embedding, the retriever contract and the mapping from stored points back to
nodes; it may not own who is allowed to read the result, because a framework
that decides both what is relevant and what is permitted has no seam at which a
second, authoritative check can happen. ``RetrievalService`` performs that check
against PostgreSQL on everything this port returns, and again before an answer
is released.

Two things about the shape of this protocol are deliberate.

``limit`` is a candidate budget, not a page size. The caller asks for more than
it will keep, because authorization removes some and reranking only means
something when it has more to choose from than it will return. An implementation
that quietly treats it as top_k does not fail anything -- it just narrows the
funnel before the parts that were supposed to narrow it, and the loss shows up
as slightly worse answers nobody can attribute.

``mode`` names what this retriever *is*, so a report cannot mislabel it. Two
evaluation runs are only comparable when the thing being compared is named, and
"hybrid" that silently ran dense -- because a sparse runtime failed to load --
is the single most expensive way for an evaluation to be wrong: it produces a
plausible number for a retriever that never ran.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_workbench.ports.vector_index import ScoredChunk


@runtime_checkable
class CandidateRetrieverPort(Protocol):
    """Chunks a retriever proposes for one question, in rank order."""

    @property
    def mode(self) -> str:
        """Which retriever this is, for a report to name.

        Describes what is configured, not what the last call did. Whether a
        given call actually fused, reranked or fell back is a fact about that
        call and belongs on its result, not on a property that cannot see it.
        """
        ...

    async def candidates(
        self,
        *,
        query: str,
        tenant_id: str,
        principal_id: str,
        knowledge_base_id: str,
        limit: int,
    ) -> tuple[ScoredChunk, ...]:
        """Propose up to ``limit`` chunks, best first.

        ``principal_id`` narrows; it does not authorize. Implementations pass
        it to the index so a query returns less, and the caller re-checks every
        survivor against PostgreSQL before it becomes context or a citation.
        An implementation that dropped it would return more rather than fewer
        candidates and still be correct at this boundary -- which is precisely
        why this boundary is not where permission is decided.

        Rank order is part of the contract. Everything downstream -- the rerank
        tie-break, the cut to top_k, the order citations appear in -- treats
        position as meaning, so a retriever that returns a set is returning
        something narrower than it claims.
        """
        ...


__all__ = ["CandidateRetrieverPort"]
