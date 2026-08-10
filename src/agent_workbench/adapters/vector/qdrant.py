"""Chunk vectors in Qdrant.

Point ids are UUIDv5 over the chunk id. Qdrant only accepts unsigned integers
and UUIDs, and the chunk id is neither -- but a hash of it is stable, so
re-indexing the same chunk lands on the same point. Generating an id instead
would turn every retried delivery into another near-duplicate that retrieval
returns alongside the original.

Filtering happens in the query, not after it. A ``limit`` applied to unfiltered
results and then narrowed in Python returns fewer rows than asked for, or none,
depending on how the neighbourhood happens to be laid out -- and it moves the
tenant boundary into the caller. Both the boundary and the count belong to the
same statement.

The payload keeps text alongside the vector. It is the only copy retrieval
needs to build a context packet, and fetching it from PostgreSQL per candidate
would be one query per hit on the hot path. The authority is still PostgreSQL:
this text is a copy that a re-index replaces.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from agent_workbench.domain.errors import IncompatibleSchemaError
from agent_workbench.ports.vector_index import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    IndexedChunk,
    ScoredChunk,
)

# Fixed, so a point id is reproducible across processes and releases. Changing
# it re-homes every point, which is a re-index rather than a deployment.
POINT_NAMESPACE = uuid.UUID("6f1a5f9e-0f6a-5c3e-9a4b-2f7c1d8e5b30")

# Filtered on for every query, so Qdrant needs an index on each of them.
FILTER_KEYS = ("tenant_id", "knowledge_base_id", "authorized_principals")

# The constant in ``1 / (k + rank)``. Two, because that is what Qdrant's
# server-side RRF used, and ADR-033 moves the fusion without moving the scale:
# a different k here would change every fused score and quietly invalidate the
# evaluation numbers this path is compared on. Ranks are zero-based, as they
# were there -- the top of an arm contributes 1/2, not 1/3.
RRF_K = 2


def point_id(chunk_id: str) -> str:
    """The stable point id for a chunk."""

    return str(uuid.uuid5(POINT_NAMESPACE, chunk_id))


class QdrantVectorIndex:
    """``VectorIndexPort`` over one Qdrant collection."""

    __slots__ = ("_client", "_collection")

    def __init__(self, client: AsyncQdrantClient, *, collection: str) -> None:
        self._client = client
        self._collection = collection

    async def ensure_collection(self, *, vector_size: int) -> None:
        if vector_size < 1:
            raise ValueError("vector_size must be positive")

        if await self._client.collection_exists(self._collection):
            await self._require_matching_size(vector_size)
            return

        try:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config={
                    DENSE_VECTOR_NAME: models.VectorParams(
                        size=vector_size,
                        distance=models.Distance.COSINE,
                    )
                },
                # Declared whether or not this process has a sparse encoder. A
                # collection created without it could not gain one later
                # without a rebuild, and the cost of an unused named vector is
                # nothing until points carry one.
                sparse_vectors_config={SPARSE_VECTOR_NAME: models.SparseVectorParams()},
            )
        except Exception:
            # Another process created it between the check and the call. That
            # is the ordinary startup race, not a failure -- but the size it
            # created still has to be the one we expect.
            if not await self._client.collection_exists(self._collection):
                raise
            await self._require_matching_size(vector_size)
            return

        for key in FILTER_KEYS:
            await self._client.create_payload_index(
                collection_name=self._collection,
                field_name=key,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    async def _require_matching_size(self, vector_size: int) -> None:
        existing = await self._client.get_collection(self._collection)
        vectors = existing.config.params.vectors
        if not isinstance(vectors, dict) or DENSE_VECTOR_NAME not in vectors:
            raise IncompatibleSchemaError(
                f"collection {self._collection} has no {DENSE_VECTOR_NAME} vector"
            )
        found = vectors[DENSE_VECTOR_NAME].size
        if found != vector_size:
            # Recreating would discard an index something is querying right
            # now, and silently accepting it would mean queries whose vectors
            # cannot be compared with what is stored.
            raise IncompatibleSchemaError(
                f"collection {self._collection} stores {found}-dimensional "
                f"vectors, but this process embeds {vector_size}-dimensional ones"
            )

    async def upsert(self, chunks: tuple[IndexedChunk, ...]) -> int:
        if not chunks:
            return 0

        await self._client.upsert(
            collection_name=self._collection,
            points=[
                models.PointStruct(
                    id=point_id(chunk.chunk_id),
                    vector=_vectors(chunk),
                    payload={
                        "chunk_id": chunk.chunk_id,
                        "document_id": chunk.document_id,
                        "document_version": chunk.document_version,
                        "tenant_id": chunk.tenant_id,
                        "knowledge_base_id": chunk.knowledge_base_id,
                        "owner_id": chunk.owner_id,
                        "authorized_principals": list(chunk.authorized_principals),
                        "source_revision": chunk.source_revision,
                        "text": chunk.text,
                        "ordinal": chunk.ordinal,
                        "page": chunk.page,
                    },
                )
                for chunk in chunks
            ],
            wait=True,
        )
        return len(chunks)

    async def search_hybrid(
        self,
        *,
        vector: tuple[float, ...],
        sparse_indices: tuple[int, ...],
        sparse_values: tuple[float, ...],
        tenant_id: str,
        knowledge_base_id: str,
        authorized_principals: tuple[str, ...],
        limit: int,
        dense_limit: int,
        sparse_limit: int,
    ) -> tuple[ScoredChunk, ...]:
        """Two single-arm queries, fused once here (ADR-033).

        The fusion used to be a ``FusionQuery`` over two prefetches, which is
        one request rather than two and would be the better shape if it could
        be reproduced. It cannot: RRF scores by rank within each arm, and an
        arm returns points it scored equally in whatever order it produced
        them, so a tied point's rank -- and therefore the fused score built
        from it -- is the engine's arbitrary choice. Ordering the fused output
        afterwards cannot repair that, because the disagreement is already in
        the scores. Measured: ten re-indexes of one fixture gave ten different
        hybrid orders, and twice the strictly-best point was not first.

        Fusing here is still fusing *once*. Each arm below is a raw retriever
        result that nothing has fused, and RRF runs over the two of them one
        time. What moves is only who decides the ranks inside an arm: passing
        each through ``_ranked`` first makes a tied point's rank follow from
        its ``chunk_id``, which survives a re-index.
        """

        narrowing = self._narrowing(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            authorized_principals=authorized_principals,
        )
        # Stated on each arm rather than once around the pair. It was already
        # written per-prefetch for this reason and the reason is unchanged:
        # each arm should choose among candidates this principal may read, and
        # a narrowing that held only because an optimiser pushed it down would
        # be a property of the engine rather than of these queries.
        dense_response, sparse_response = await asyncio.gather(
            self._client.query_points(
                collection_name=self._collection,
                query=list(vector),
                using=DENSE_VECTOR_NAME,
                query_filter=narrowing,
                limit=dense_limit,
                with_payload=True,
            ),
            self._client.query_points(
                collection_name=self._collection,
                query=models.SparseVector(
                    indices=list(sparse_indices), values=list(sparse_values)
                ),
                using=SPARSE_VECTOR_NAME,
                query_filter=narrowing,
                limit=sparse_limit,
                with_payload=True,
            ),
        )
        return _fused(
            _ranked(dense_response.points),
            _ranked(sparse_response.points),
            limit=limit,
        )

    async def search(
        self,
        *,
        vector: tuple[float, ...],
        tenant_id: str,
        knowledge_base_id: str,
        authorized_principals: tuple[str, ...],
        limit: int,
    ) -> tuple[ScoredChunk, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if not authorized_principals:
            # No principal can match, so the filter would return nothing. Said
            # here rather than left to Qdrant, because "nobody is authorized"
            # and "the index is empty" must not look like the same answer to
            # whoever is reading this code later.
            return ()

        response = await self._client.query_points(
            collection_name=self._collection,
            query=list(vector),
            using=DENSE_VECTOR_NAME,
            query_filter=self._narrowing(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                authorized_principals=authorized_principals,
            ),
            limit=limit,
            with_payload=True,
        )

        return _ranked(response.points)

    @staticmethod
    def _narrowing(
        *,
        tenant_id: str,
        knowledge_base_id: str,
        authorized_principals: tuple[str, ...],
    ) -> models.Filter:
        """The candidate narrowing, shared by every search on this index.

        One definition rather than one per method: two copies are two chances
        for the hybrid path to narrow differently from the dense one, and a
        difference there is a difference in who can be retrieved.
        """

        return models.Filter(
            must=[
                models.FieldCondition(
                    key="tenant_id",
                    match=models.MatchValue(value=tenant_id),
                ),
                models.FieldCondition(
                    key="knowledge_base_id",
                    match=models.MatchValue(value=knowledge_base_id),
                ),
                models.FieldCondition(
                    key="authorized_principals",
                    match=models.MatchAny(any=list(authorized_principals)),
                ),
            ]
        )

    async def delete_document(self, *, tenant_id: str, document_id: str) -> None:
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="tenant_id",
                            match=models.MatchValue(value=tenant_id),
                        ),
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchValue(value=document_id),
                        ),
                    ]
                )
            ),
            wait=True,
        )


def _text(payload: dict[str, Any] | None, key: str) -> str:
    """A payload string, or a refusal.

    The payload is this adapter's own writing, so a missing key means the
    collection was written by something else -- a different schema version, or
    another service sharing the name. Guessing a default would put that
    something else's data into a context packet.
    """

    value = (payload or {}).get(key)
    if not isinstance(value, str):
        raise IncompatibleSchemaError(f"indexed point has no string {key!r}")
    return value


def _number(payload: dict[str, Any] | None, key: str) -> int:
    value = (payload or {}).get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise IncompatibleSchemaError(f"indexed point has no integer {key!r}")
    return value


def _optional_number(payload: dict[str, Any] | None, key: str) -> int | None:
    """Read a number that a point is allowed not to have.

    Distinct from ``_number``, which refuses a point missing a field the schema
    requires. A page is genuinely absent for every format without pages and for
    every point written before pages were carried, so "not there" is an answer
    rather than a corrupt row.
    """

    value = (payload or {}).get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise IncompatibleSchemaError(f"indexed point has a non-integer {key!r}")
    return value


def _vectors(chunk: IndexedChunk) -> dict[str, Any]:
    """Named vectors for one point, omitting sparse when there is none.

    Writing an empty sparse vector would make a point that matches every sparse
    query at zero weight rather than one that matches none.
    """

    vectors: dict[str, Any] = {DENSE_VECTOR_NAME: list(chunk.vector)}
    if chunk.sparse_indices:
        vectors[SPARSE_VECTOR_NAME] = models.SparseVector(
            indices=list(chunk.sparse_indices),
            values=list(chunk.sparse_values),
        )
    return vectors


def _ranked(points: Any) -> tuple[ScoredChunk, ...]:
    """Returned points, in an order that is the same on every identical query.

    Qdrant orders by score, and says nothing about points whose scores are
    equal -- so it returns them however it happened to produce them. RRF makes
    that common rather than exotic: it scores by rank sums over small integers,
    so on a modest corpus several candidates land on exactly the same value.
    Measured on the 38-question evaluation set, repeating one query returned a
    different chunk order on 9 of 38 questions, and a different *top-3
    document* list on 3 of 38 -- enough to move recall@1.

    Two things depend on that not happening. An evaluation cannot attribute a
    difference between two runs if a single retriever disagrees with itself
    more than the two runs differ, which is what blocked ADR-017's equivalence
    step. And a user asking the same question twice can otherwise get different
    evidence and different citations, because ``RetrievalService`` cuts to
    top_k after this: a chunk that lost a coin flip at rank 3 is simply gone.

    ``chunk_id`` is the tie-break because it is derived from the chunk, so it
    is stable across a re-index -- a tie-break on point insertion order or on
    anything the engine chooses would only move the nondeterminism somewhere
    harder to see. Score still dominates: this orders *within* equal scores and
    cannot promote a lower-scored candidate over a higher one.

    Where this is applied matters, and the paragraph above used to overstate
    it. On a single-arm result it is the whole fix. On a *fused* one it is not
    a fix at all: RRF builds its scores out of ranks within each arm, so a tie
    inside an arm has already perturbed the numbers by the time they arrive,
    and sorting them stably just reproduces one arbitrary outcome per query.
    That is why ADR-033 has ``search_hybrid`` apply this to each arm before
    fusing rather than to the fused result -- same function, and now upstream
    of the thing that was actually unstable.
    """

    return tuple(sorted((_scored(point) for point in points), key=_rank_key))


def _rank_key(chunk: ScoredChunk) -> tuple[float, str]:
    return (-chunk.score, chunk.chunk_id)


def _fused(*arms: tuple[ScoredChunk, ...], limit: int) -> tuple[ScoredChunk, ...]:
    """Reciprocal rank fusion over arms that already have a settled order.

    ``1 / (RRF_K + rank)`` summed across the arms a chunk appears in, which is
    the formula Qdrant's own fusion uses -- kept identical so that this change
    moves where the ranks come from without also moving the scale an
    evaluation is measured on.

    The arms must arrive ranked by ``_ranked``. That is the whole point: the
    input to this function is where reproducibility is won or lost, and a
    caller handing over an arm in engine order would get a stable sort of
    unstable numbers.

    Chunks an arm scored equally share a rank, rather than being dealt
    consecutive ones in ``chunk_id`` order. Both spellings are reproducible, so
    this is not about determinism -- it is that consecutive ranks would let an
    arm vote on an order it did not perceive. The sparse arm over a one-term
    query scores every matching chunk identically; dealing it 0,1,2,3 turns
    alphabetical accident into fusion weight, and measurably so, because it
    demotes a chunk the dense arm ranked strictly first. A tie means the arm
    has no opinion, and no opinion has to fuse as no opinion.
    """

    contributions: dict[str, float] = {}
    seen: dict[str, ScoredChunk] = {}
    for arm in arms:
        rank = 0
        for position, chunk in enumerate(arm):
            # The arm is sorted, so equal scores form one run; the run keeps
            # the rank of its first member, and the next distinct score takes
            # its own position. A chunk's rank is therefore how many candidates
            # strictly beat it in this arm.
            if position and arm[position - 1].score != chunk.score:
                rank = position
            contributions[chunk.chunk_id] = contributions.get(
                chunk.chunk_id, 0.0
            ) + 1.0 / (RRF_K + rank)
            # The payload is identical whichever arm returned it; only the
            # score differs, and that is about to be replaced by the fused one.
            seen.setdefault(chunk.chunk_id, chunk)

    fused = tuple(
        seen[chunk_id].model_copy(update={"score": score})
        for chunk_id, score in contributions.items()
    )
    return tuple(sorted(fused, key=_rank_key))[:limit]


def _scored(point: Any) -> ScoredChunk:
    """One returned point, as the domain sees it.

    Note what is absent: the payload's copy of the ACL. A caller that could
    read it might treat it as the answer, and it is only ever a narrowing --
    PostgreSQL decides, before and after.
    """

    return ScoredChunk(
        chunk_id=_text(point.payload, "chunk_id"),
        document_id=_text(point.payload, "document_id"),
        document_version=_text(point.payload, "document_version"),
        tenant_id=_text(point.payload, "tenant_id"),
        knowledge_base_id=_text(point.payload, "knowledge_base_id"),
        source_revision=_number(point.payload, "source_revision"),
        text=_text(point.payload, "text"),
        ordinal=_number(point.payload, "ordinal"),
        # Absent from points written before pages were carried, and absent for
        # every format that has none. Optional rather than defaulted, so an old
        # point reads as "no page" instead of as page one.
        page=_optional_number(point.payload, "page"),
        score=point.score,
    )


__all__ = ["FILTER_KEYS", "POINT_NAMESPACE", "QdrantVectorIndex", "point_id"]
