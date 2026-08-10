"""``KnowledgeGraphStore`` over the three kg tables.

Every write is idempotent by a unique constraint rather than by a read-then-
insert: the second extraction of one document version is an ordinary retry,
and losing the race between two of them must produce one row, not an error.

Every read narrows by ``graph_identity`` inside the query. Rows written by
another extractor describe the same corpus differently, and mixing them would
let a re-extraction change what retrieval returns without changing anything a
reader can see.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.models import (
    kg_entities,
    kg_mentions,
    kg_relations,
)
from agent_workbench.domain.identifiers import new_id
from agent_workbench.ports.knowledge_graph import ChunkNomination, StoredEntity

ENTITY_PREFIX = "ent"
MENTION_PREFIX = "men"
RELATION_PREFIX = "rel"


class PostgresKnowledgeGraphStore:
    """The retrieval graph, in the same database that owns authorization."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record_chunk(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version: str,
        chunk_id: str,
        graph_identity: str,
        entities: tuple[tuple[str, str, str], ...],
        relations: tuple[tuple[str, str, str], ...],
    ) -> tuple[StoredEntity, ...]:
        if not entities:
            return ()

        async with self._engine.begin() as connection:
            merged = await self._merge_entities(
                connection,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                graph_identity=graph_identity,
                entities=entities,
            )
            by_key = {
                (entity.normalized_name, entity.entity_type): entity
                for entity in merged
            }

            await connection.execute(
                pg_insert(kg_mentions)
                .values(
                    [
                        {
                            "mention_id": new_id(MENTION_PREFIX),
                            "entity_id": entity.entity_id,
                            "tenant_id": tenant_id,
                            "knowledge_base_id": knowledge_base_id,
                            "document_id": document_id,
                            "document_version": document_version,
                            "chunk_id": chunk_id,
                        }
                        for entity in merged
                    ]
                )
                # The retry, made harmless. A chunk naming an entity twice is
                # one nomination, and re-extracting a version must not
                # accumulate.
                .on_conflict_do_nothing(constraint="uq_kg_mentions_entity_chunk")
            )

            edges = [
                {
                    "relation_id": new_id(RELATION_PREFIX),
                    "tenant_id": tenant_id,
                    "knowledge_base_id": knowledge_base_id,
                    "subject_entity_id": by_key[subject].entity_id,
                    "object_entity_id": by_key[object_].entity_id,
                    "description": description,
                    "document_id": document_id,
                    "document_version": document_version,
                    "chunk_id": chunk_id,
                    "graph_identity": graph_identity,
                }
                for subject, object_, description in _resolvable(relations, by_key)
            ]
            if edges:
                await connection.execute(
                    pg_insert(kg_relations)
                    .values(edges)
                    .on_conflict_do_nothing(constraint="uq_kg_relations_edge_chunk")
                )
        return merged

    async def _merge_entities(
        self,
        connection: AsyncConnection,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        graph_identity: str,
        entities: tuple[tuple[str, str, str], ...],
    ) -> tuple[StoredEntity, ...]:
        """Insert-or-nothing, then read back every row by its merge key.

        Read-then-insert would lose the race between two workers extracting
        two chunks that name the same thing: both would find nothing and both
        would insert. The unique constraint decides instead, and the read
        afterwards is what tells this caller which id won.
        """

        await connection.execute(
            pg_insert(kg_entities)
            .values(
                [
                    {
                        "entity_id": new_id(ENTITY_PREFIX),
                        "tenant_id": tenant_id,
                        "knowledge_base_id": knowledge_base_id,
                        "normalized_name": normalized,
                        "entity_type": entity_type,
                        "display_name": display,
                        "graph_identity": graph_identity,
                    }
                    for normalized, entity_type, display in entities
                ]
            )
            .on_conflict_do_nothing(constraint="uq_kg_entities_merge_key")
        )

        wanted = {(normalized, kind) for normalized, kind, _ in entities}
        rows = (
            (
                await connection.execute(
                    select(kg_entities).where(
                        kg_entities.c.tenant_id == tenant_id,
                        kg_entities.c.knowledge_base_id == knowledge_base_id,
                        kg_entities.c.graph_identity == graph_identity,
                        kg_entities.c.normalized_name.in_(
                            [normalized for normalized, _ in wanted]
                        ),
                    )
                )
            )
            .mappings()
            .all()
        )
        return tuple(
            StoredEntity(
                entity_id=row["entity_id"],
                normalized_name=row["normalized_name"],
                entity_type=row["entity_type"],
                display_name=row["display_name"],
            )
            for row in rows
            if (row["normalized_name"], row["entity_type"]) in wanted
        )

    async def forget_document(self, *, tenant_id: str, document_id: str) -> int:
        async with self._engine.begin() as connection:
            mentions = await connection.execute(
                delete(kg_mentions).where(
                    kg_mentions.c.tenant_id == tenant_id,
                    kg_mentions.c.document_id == document_id,
                )
            )
            relations = await connection.execute(
                delete(kg_relations).where(
                    kg_relations.c.tenant_id == tenant_id,
                    kg_relations.c.document_id == document_id,
                )
            )
        # Entities are deliberately left standing; see the port.
        return (mentions.rowcount or 0) + (relations.rowcount or 0)

    async def nominations_for_entities(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        graph_identity: str,
        scored_entity_ids: tuple[tuple[str, float], ...],
        limit: int,
    ) -> tuple[ChunkNomination, ...]:
        if not scored_entity_ids or limit < 1:
            return ()

        scores = dict(scored_entity_ids)
        query = (
            select(
                kg_mentions.c.entity_id,
                kg_mentions.c.chunk_id,
                kg_mentions.c.document_id,
                kg_mentions.c.document_version,
            )
            .join(kg_entities, kg_entities.c.entity_id == kg_mentions.c.entity_id)
            .where(
                kg_mentions.c.tenant_id == tenant_id,
                kg_mentions.c.knowledge_base_id == knowledge_base_id,
                kg_mentions.c.entity_id.in_(list(scores)),
                # In the query, not after it. See the module docstring.
                kg_entities.c.graph_identity == graph_identity,
            )
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).mappings().all()
        return _best_per_chunk(rows, scores, key="entity_id", limit=limit)

    async def nominations_for_relations(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        graph_identity: str,
        scored_relation_ids: tuple[tuple[str, float], ...],
        limit: int,
    ) -> tuple[ChunkNomination, ...]:
        if not scored_relation_ids or limit < 1:
            return ()

        scores = dict(scored_relation_ids)
        query = select(
            kg_relations.c.relation_id,
            kg_relations.c.chunk_id,
            kg_relations.c.document_id,
            kg_relations.c.document_version,
        ).where(
            kg_relations.c.tenant_id == tenant_id,
            kg_relations.c.knowledge_base_id == knowledge_base_id,
            kg_relations.c.relation_id.in_(list(scores)),
            kg_relations.c.graph_identity == graph_identity,
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).mappings().all()
        return _best_per_chunk(rows, scores, key="relation_id", limit=limit)


def _resolvable(
    relations: tuple[tuple[str, str, str], ...],
    by_key: dict[tuple[str, str], StoredEntity],
) -> list[tuple[tuple[str, str], tuple[str, str], str]]:
    """Edges whose endpoints both merged to a row this call knows.

    An edge naming something the chunk did not list cannot be stored without
    inventing the entity, which would be this code making a claim. The domain
    already drops most of them; this is the same rule at the point where ids
    exist, because an entity type mismatch only becomes visible here.
    """

    resolved: list[tuple[tuple[str, str], tuple[str, str], str]] = []
    by_name: dict[str, tuple[str, str]] = {
        normalized: (normalized, kind) for normalized, kind in by_key
    }
    for subject, object_, description in relations:
        subject_key = by_name.get(subject)
        object_key = by_name.get(object_)
        if subject_key is None or object_key is None:
            continue
        resolved.append((subject_key, object_key, description))
    return resolved


def _best_per_chunk(
    rows: Any,
    scores: dict[str, float],
    *,
    key: str,
    limit: int,
) -> tuple[ChunkNomination, ...]:
    """One nomination per chunk, carrying the best score that reached it.

    A chunk mentioning three matched entities is still one chunk, and letting
    it appear three times would give it three votes in a fusion that counts
    ranks. Ties break on ``chunk_id`` for the reason every other ordering here
    does: it is derived from the chunk and survives a re-index.
    """

    best: dict[str, ChunkNomination] = {}
    for row in rows:
        score = scores.get(row[key], 0.0)
        current = best.get(row["chunk_id"])
        if current is not None and current.score >= score:
            continue
        best[row["chunk_id"]] = ChunkNomination(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            document_version=row["document_version"],
            score=score,
        )
    ordered = sorted(best.values(), key=lambda n: (-n.score, n.chunk_id))
    return tuple(ordered[:limit])


__all__ = ["PostgresKnowledgeGraphStore"]
