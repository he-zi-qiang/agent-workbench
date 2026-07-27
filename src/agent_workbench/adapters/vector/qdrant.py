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
        """One request: two prefetches and an RRF fusion, all inside Qdrant."""

        narrowing = self._narrowing(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            authorized_principals=authorized_principals,
        )
        # The filter is stated on each prefetch rather than once on the fusion.
        # Qdrant appears to push an outer filter down -- both spellings return
        # the same points here, and a test that swaps them does not fail -- so
        # this is not load-bearing today. It is written this way because the
        # narrowing is per-arm in intent: each prefetch should be choosing
        # among candidates this principal may read, and relying on a push-down
        # to make that true would make the guarantee a property of the engine's
        # optimiser rather than of this query.
        response = await self._client.query_points(
            collection_name=self._collection,
            prefetch=[
                models.Prefetch(
                    query=list(vector),
                    using=DENSE_VECTOR_NAME,
                    filter=narrowing,
                    limit=dense_limit,
                ),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=list(sparse_indices), values=list(sparse_values)
                    ),
                    using=SPARSE_VECTOR_NAME,
                    filter=narrowing,
                    limit=sparse_limit,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
        return tuple(_scored(point) for point in response.points)

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

        return tuple(_scored(point) for point in response.points)

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
        score=point.score,
    )


__all__ = ["FILTER_KEYS", "POINT_NAMESPACE", "QdrantVectorIndex", "point_id"]
