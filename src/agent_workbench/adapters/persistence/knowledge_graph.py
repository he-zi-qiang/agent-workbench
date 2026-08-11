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

from sqlalchemy import delete, distinct, func, select
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

    async def expand_from_seeds(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        graph_identity: str,
        seed_chunk_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[ChunkNomination, ...]:
        if not seed_chunk_ids or limit < 1:
            return ()

        seeds = set(seed_chunk_ids)
        # One statement, not two round trips. The self-join is the hop: from a
        # seed's mention to every other mention of the same entity.
        neighbours = kg_mentions.alias("neighbours")
        # How many documents each bridging entity appears in, computed in the
        # same statement rather than fetched per entity. This is the weight --
        # see the scoring note below for why counting mentions was wrong.
        spread = (
            select(
                kg_mentions.c.entity_id.label("entity_id"),
                func.count(distinct(kg_mentions.c.document_id)).label("documents"),
            )
            .where(
                kg_mentions.c.tenant_id == tenant_id,
                kg_mentions.c.knowledge_base_id == knowledge_base_id,
            )
            .group_by(kg_mentions.c.entity_id)
            .subquery()
        )
        query = (
            select(
                neighbours.c.chunk_id,
                neighbours.c.document_id,
                neighbours.c.document_version,
                kg_mentions.c.entity_id,
                spread.c.documents,
            )
            .select_from(
                kg_mentions.join(
                    neighbours, neighbours.c.entity_id == kg_mentions.c.entity_id
                )
                .join(kg_entities, kg_entities.c.entity_id == kg_mentions.c.entity_id)
                .join(spread, spread.c.entity_id == kg_mentions.c.entity_id)
            )
            .where(
                kg_mentions.c.tenant_id == tenant_id,
                kg_mentions.c.knowledge_base_id == knowledge_base_id,
                kg_mentions.c.chunk_id.in_(list(seeds)),
                neighbours.c.tenant_id == tenant_id,
                neighbours.c.knowledge_base_id == knowledge_base_id,
                # Excluded in the query rather than filtered afterwards: a
                # ``limit`` applied to a list still holding the seeds would
                # spend the arm's budget re-nominating what the other arms
                # already returned.
                neighbours.c.chunk_id.notin_(list(seeds)),
                kg_entities.c.graph_identity == graph_identity,
            )
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).mappings().all()

        # Summed entity specificity, not a count of bridging entities. The
        # count was measured and it made retrieval worse: on the 2026-08-10
        # ablation this arm cost 4 questions of full_coverage@3, because an
        # entity like `aw-core` appears in five documents and reaches all of
        # them at full strength, flooding the top with everything that shares
        # a hub. Counting rewards exactly that -- a chunk sharing three hubs
        # outranks one sharing the single entity the question is about.
        #
        # 1/documents is the correction, and it is the familiar one: a bridge
        # is informative in proportion to how few things it connects. An
        # entity in two documents contributes 0.5, one in five contributes
        # 0.2, so a hub has to appear four times over to outweigh one specific
        # link. Contributions still sum, so several specific bridges beat one.
        #
        # Deliberately not a log: the corpora here are small enough that the
        # difference between 2 and 5 documents matters more than the shape of
        # the curve, and a linear reciprocal is one somebody can verify by
        # reading a row.
        weighted: dict[str, float] = {}
        counted: dict[str, set[str]] = {}
        seen: dict[str, tuple[str, str]] = {}
        for row in rows:
            chunk_id = row["chunk_id"]
            entity_id = row["entity_id"]
            already = counted.setdefault(chunk_id, set())
            if entity_id in already:
                # One entity mentioned twice in a seed is still one bridge.
                continue
            already.add(entity_id)
            documents = max(int(row["documents"]), 1)
            weighted[chunk_id] = weighted.get(chunk_id, 0.0) + 1.0 / documents
            seen.setdefault(chunk_id, (row["document_id"], row["document_version"]))
        nominations = [
            ChunkNomination(
                chunk_id=chunk_id,
                document_id=seen[chunk_id][0],
                document_version=seen[chunk_id][1],
                score=score,
            )
            for chunk_id, score in weighted.items()
        ]
        nominations.sort(key=lambda n: (-n.score, n.chunk_id))
        return tuple(nominations[:limit])

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
