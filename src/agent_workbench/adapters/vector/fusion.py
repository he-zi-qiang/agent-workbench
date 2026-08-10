"""Ordering an arm, and fusing arms, without knowing where they came from.

Lifted out of the Qdrant adapter because fusion stopped being one adapter's
business. ADR-033 moved the fusion into this process so that the ranks going
into it are ours; what stayed behind is the assumption that the only arms
worth fusing are the two a single Qdrant collection can answer. A retriever
that nominates chunks some other way -- from an entity index, from a
relationship index -- has to reach the same RRF, or it is a second fusion with
its own scale and no evaluation can compare the two.

So this module holds the parts that are pure: how an arm is ordered, and how
several ordered arms become one list. Turning an engine's response into a
``ScoredChunk`` stays with the engine's adapter, because that is the part that
knows what a payload looks like.

Nothing here fuses twice. ``fused`` takes arms that nothing has fused and runs
once over them; calling it on its own output would produce an ordering no
retriever produced, which is the thing ``VectorIndexPort`` forbids in words.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent_workbench.ports.vector_index import ScoredChunk

# The constant in ``1 / (k + rank)``. Two, because that is what Qdrant's
# server-side RRF used, and ADR-033 moved the fusion without moving the scale:
# a different k here would change every fused score and quietly invalidate the
# evaluation numbers this path is compared on. Ranks are zero-based, as they
# were there -- the top of an arm contributes 1/2, not 1/3.
RRF_K = 2


def rank_key(chunk: ScoredChunk) -> tuple[float, str]:
    return (-chunk.score, chunk.chunk_id)


def ranked(chunks: Iterable[ScoredChunk]) -> tuple[ScoredChunk, ...]:
    """One arm's candidates, in an order identical on every identical query.

    An engine orders by score and says nothing about candidates whose scores
    are equal -- so it returns them however it happened to produce them. RRF
    makes that common rather than exotic: it scores by rank sums over small
    integers, so on a modest corpus several candidates land on exactly the
    same value. Measured on the 38-question evaluation set, repeating one
    query returned a different chunk order on 9 of 38 questions, and a
    different *top-3 document* list on 3 of 38 -- enough to move recall@1.

    Two things depend on that not happening. An evaluation cannot attribute a
    difference between two runs if a single retriever disagrees with itself
    more than the two runs differ, which is what blocked ADR-017's equivalence
    step. And a user asking the same question twice can otherwise get
    different evidence and different citations, because ``RetrievalService``
    cuts to top_k after this: a chunk that lost a coin flip at rank 3 is
    simply gone.

    ``chunk_id`` is the tie-break because it is derived from the chunk, so it
    is stable across a re-index -- a tie-break on point insertion order or on
    anything the engine chooses would only move the nondeterminism somewhere
    harder to see. Score still dominates: this orders *within* equal scores
    and cannot promote a lower-scored candidate over a higher one.

    Where this is applied matters. On a single arm it is the whole fix. On a
    *fused* result it is not a fix at all: RRF builds its scores out of ranks
    within each arm, so a tie inside an arm has already perturbed the numbers
    by the time they arrive, and sorting them stably just reproduces one
    arbitrary outcome per query. Which is why every caller applies this to
    each arm before ``fused`` rather than to what ``fused`` returns.
    """

    return tuple(sorted(chunks, key=rank_key))


def fused(*arms: tuple[ScoredChunk, ...], limit: int) -> tuple[ScoredChunk, ...]:
    """Reciprocal rank fusion over arms that already have a settled order.

    ``1 / (RRF_K + rank)`` summed across the arms a chunk appears in, which is
    the formula Qdrant's own fusion used -- kept identical so that moving the
    fusion did not also move the scale an evaluation is measured on.

    Variadic on purpose, and it always was: two arms is what a hybrid query
    has, not what RRF is. An arm nominated from an entity or relationship
    index fuses here on the same terms as the dense and sparse ones, and
    fusing still happens exactly once.

    The arms must arrive ordered by ``ranked``. That is the whole point: the
    input to this function is where reproducibility is won or lost, and a
    caller handing over an arm in engine order would get a stable sort of
    unstable numbers.

    Chunks an arm scored equally share a rank, rather than being dealt
    consecutive ones in ``chunk_id`` order. Both spellings are reproducible,
    so this is not about determinism -- it is that consecutive ranks would let
    an arm vote on an order it did not perceive. The sparse arm over a
    one-term query scores every matching chunk identically; dealing it
    0,1,2,3 turns alphabetical accident into fusion weight, and measurably so,
    because it demotes a chunk the dense arm ranked strictly first. A tie
    means the arm has no opinion, and no opinion has to fuse as no opinion.
    """

    contributions: dict[str, float] = {}
    seen: dict[str, ScoredChunk] = {}
    for arm in arms:
        rank = 0
        for position, chunk in enumerate(arm):
            # The arm is sorted, so equal scores form one run; the run keeps
            # the rank of its first member, and the next distinct score takes
            # its own position. A chunk's rank is therefore how many
            # candidates strictly beat it in this arm.
            if position and arm[position - 1].score != chunk.score:
                rank = position
            contributions[chunk.chunk_id] = contributions.get(
                chunk.chunk_id, 0.0
            ) + 1.0 / (RRF_K + rank)
            # The payload is identical whichever arm returned it; only the
            # score differs, and that is about to be replaced by the fused
            # one.
            seen.setdefault(chunk.chunk_id, chunk)

    result = tuple(
        seen[chunk_id].model_copy(update={"score": score})
        for chunk_id, score in contributions.items()
    )
    return tuple(sorted(result, key=rank_key))[:limit]


__all__ = ["RRF_K", "fused", "rank_key", "ranked"]
