"""The retrieval graph against real PostgreSQL.

The properties worth a real database are the ones a fake cannot have: the
unique constraints that make a retried extraction idempotent, the identity
narrowing that keeps two extractors' rows apart, and the join that turns a
matched entity back into the chunk it was read from.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.adapters.persistence.knowledge_graph import (
    PostgresKnowledgeGraphStore,
)

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TENANT = "tenant_a"
KB = "kb_main"
IDENTITY = "deepseek-chat+v1+bge@1"
OTHER_IDENTITY = "deepseek-chat+v2+bge@1"

TABLES = "kg_relations, kg_mentions, kg_entities"

MARLIN = ("team marlin", "team", "Team Marlin")
CINDER = ("cinder rotation", "rotation", "Cinder rotation")
OSPREY = ("team osprey", "team", "Team Osprey")


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _run(scenario: Callable[[PostgresKnowledgeGraphStore], Awaitable[Any]]) -> Any:
    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            return await scenario(PostgresKnowledgeGraphStore(engine))
        finally:
            await engine.dispose()

    return asyncio.run(execute())


async def _record(
    store: PostgresKnowledgeGraphStore,
    *,
    chunk_id: str,
    document_id: str = "doc_teams",
    entities: tuple[tuple[str, str, str], ...] = (MARLIN, CINDER),
    relations: tuple[tuple[str, str, str], ...] = (),
    graph_identity: str = IDENTITY,
) -> Any:
    return await store.record_chunk(
        tenant_id=TENANT,
        knowledge_base_id=KB,
        document_id=document_id,
        document_version="ver_1",
        chunk_id=chunk_id,
        graph_identity=graph_identity,
        entities=entities,
        relations=relations,
    )


# --- merging, and what refuses to merge --------------------------------------


def test_two_chunks_naming_one_thing_share_an_entity() -> None:
    """The entry point merges; that is what buys the cross-document hop."""

    async def scenario(store: PostgresKnowledgeGraphStore) -> tuple[str, str]:
        first = await _record(store, chunk_id="chk_1", entities=(MARLIN,))
        second = await _record(
            store, chunk_id="chk_2", document_id="doc_runbook", entities=(MARLIN,)
        )
        return first[0].entity_id, second[0].entity_id

    one, other = _run(scenario)

    assert one == other


def test_evidence_does_not_merge_with_the_entity() -> None:
    """ADR-037 in one assertion: one entry point, two chunks behind it, each
    still naming the document it was read from."""

    async def scenario(store: PostgresKnowledgeGraphStore) -> Any:
        entity = (await _record(store, chunk_id="chk_1", entities=(MARLIN,)))[0]
        await _record(
            store, chunk_id="chk_2", document_id="doc_runbook", entities=(MARLIN,)
        )
        return await store.nominations_for_entities(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            graph_identity=IDENTITY,
            scored_entity_ids=((entity.entity_id, 0.9),),
            limit=10,
        )

    found = _run(scenario)

    assert {n.chunk_id for n in found} == {"chk_1", "chk_2"}
    assert {n.document_id for n in found} == {"doc_teams", "doc_runbook"}


def test_a_different_graph_identity_is_a_different_graph() -> None:
    """Rows from another extractor must not nominate beside these."""

    async def scenario(store: PostgresKnowledgeGraphStore) -> tuple[Any, Any]:
        mine = (await _record(store, chunk_id="chk_1", entities=(MARLIN,)))[0]
        await _record(
            store,
            chunk_id="chk_other",
            entities=(MARLIN,),
            graph_identity=OTHER_IDENTITY,
        )
        same = await store.nominations_for_entities(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            graph_identity=IDENTITY,
            scored_entity_ids=((mine.entity_id, 0.9),),
            limit=10,
        )
        # The control: asking under the other identity finds its own row and
        # not this one, so the narrowing is a filter rather than an emptiness.
        other_rows = await store.nominations_for_entities(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            graph_identity=OTHER_IDENTITY,
            scored_entity_ids=((mine.entity_id, 0.9),),
            limit=10,
        )
        return same, other_rows

    same, other_rows = _run(scenario)

    assert {n.chunk_id for n in same} == {"chk_1"}
    assert other_rows == ()


# --- idempotence -------------------------------------------------------------


def test_re_extracting_a_chunk_does_not_accumulate_mentions() -> None:
    """A retried outbox event is the ordinary case, not an error."""

    async def scenario(store: PostgresKnowledgeGraphStore) -> Any:
        entity = (await _record(store, chunk_id="chk_1", entities=(MARLIN,)))[0]
        await _record(store, chunk_id="chk_1", entities=(MARLIN,))
        await _record(store, chunk_id="chk_1", entities=(MARLIN,))
        return await store.nominations_for_entities(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            graph_identity=IDENTITY,
            scored_entity_ids=((entity.entity_id, 0.5),),
            limit=10,
        )

    found = _run(scenario)

    assert len(found) == 1


def test_a_chunk_mentioning_several_matches_still_votes_once() -> None:
    """Three matched entities in one chunk is one chunk, not three votes.

    RRF counts ranks, so a chunk appearing three times would out-rank a chunk
    that matched better and only once.
    """

    async def scenario(store: PostgresKnowledgeGraphStore) -> Any:
        stored = await _record(
            store, chunk_id="chk_1", entities=(MARLIN, CINDER, OSPREY)
        )
        return await store.nominations_for_entities(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            graph_identity=IDENTITY,
            scored_entity_ids=tuple(
                (entity.entity_id, 0.3 + index * 0.2)
                for index, entity in enumerate(stored)
            ),
            limit=10,
        )

    found = _run(scenario)

    assert len(found) == 1
    # And it carries the best score that reached it, not the first or the last.
    assert found[0].score == pytest.approx(0.7)


# --- relations ---------------------------------------------------------------


def test_an_edge_nominates_the_chunk_it_was_read_from() -> None:
    async def scenario(store: PostgresKnowledgeGraphStore) -> Any:
        await _record(
            store,
            chunk_id="chk_1",
            entities=(MARLIN, CINDER),
            relations=(("team marlin", "cinder rotation", "Marlin carries Cinder."),),
        )
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.connect() as connection:
                rows = (
                    (
                        await connection.execute(
                            text("SELECT relation_id FROM kg_relations")
                        )
                    )
                    .mappings()
                    .all()
                )
        finally:
            await engine.dispose()
        return await store.nominations_for_relations(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            graph_identity=IDENTITY,
            scored_relation_ids=tuple((row["relation_id"], 0.8) for row in rows),
            limit=10,
        )

    found = _run(scenario)

    assert [n.chunk_id for n in found] == ["chk_1"]


def test_an_edge_reaching_outside_the_chunk_is_not_stored() -> None:
    """Storing it would need an entity nothing mentioned -- an entry point
    with no evidence, which is the merged-graph failure ADR-037 refuses."""

    async def scenario(store: PostgresKnowledgeGraphStore) -> int:
        await _record(
            store,
            chunk_id="chk_1",
            entities=(MARLIN,),
            relations=(("team marlin", "team osprey", "unrelated claim"),),
        )
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.connect() as connection:
                return len(
                    (await connection.execute(text("SELECT 1 FROM kg_relations")))
                    .mappings()
                    .all()
                )
        finally:
            await engine.dispose()

    assert _run(scenario) == 0


# --- forgetting --------------------------------------------------------------


def test_forgetting_a_document_takes_its_evidence_and_leaves_the_entry_point() -> None:
    """An entity with no mentions nominates nothing, so it is inert -- and
    deleting it would race an extraction that has just merged onto it."""

    async def scenario(store: PostgresKnowledgeGraphStore) -> tuple[Any, Any]:
        entity = (await _record(store, chunk_id="chk_1", entities=(MARLIN,)))[0]
        await _record(
            store, chunk_id="chk_2", document_id="doc_runbook", entities=(MARLIN,)
        )
        await store.forget_document(tenant_id=TENANT, document_id="doc_teams")
        remaining = await store.nominations_for_entities(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            graph_identity=IDENTITY,
            scored_entity_ids=((entity.entity_id, 0.9),),
            limit=10,
        )
        return remaining, entity.entity_id

    remaining, _ = _run(scenario)

    assert [n.chunk_id for n in remaining] == ["chk_2"]


# --- specificity weighting (the 2026-08-10 ablation's finding) ---------------


def test_a_hub_entity_nominates_more_weakly_than_a_specific_one() -> None:
    """Measured, not assumed: counting bridges made retrieval worse.

    On the graph ablation the count-based score cost 4 questions of
    full_coverage@3, because an entity in five documents reached all of them
    at full strength and flooded the top with everything sharing a hub. The
    weight is 1/documents, so a bridge is worth what it narrows down to.

    Fixture: `hub` is named by four documents, `rare` by two. A chunk reached
    only through the hub must rank below one reached through the rare entity,
    even though both were reached exactly once.
    """

    HUB = ("hub thing", "thing", "Hub Thing")
    RARE = ("rare thing", "thing", "Rare Thing")

    async def scenario(store: PostgresKnowledgeGraphStore) -> Any:
        # The seed names both entities.
        await _record(
            store, chunk_id="chk_seed", document_id="doc_seed", entities=(HUB, RARE)
        )
        # The hub is everywhere ...
        for index in range(3):
            await _record(
                store,
                chunk_id=f"chk_hub_{index}",
                document_id=f"doc_hub_{index}",
                entities=(HUB,),
            )
        # ... the rare entity is in one other document.
        await _record(
            store, chunk_id="chk_rare", document_id="doc_rare", entities=(RARE,)
        )
        return await store.expand_from_seeds(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            graph_identity=IDENTITY,
            seed_chunk_ids=("chk_seed",),
            limit=10,
        )

    found = _run(scenario)
    scores = {n.chunk_id: n.score for n in found}

    # The rare bridge connects two documents -> 1/2; the hub connects four -> 1/4.
    assert scores["chk_rare"] == pytest.approx(0.5)
    assert scores["chk_hub_0"] == pytest.approx(0.25)
    # And the ordering follows, which is what the fusion actually reads.
    assert found[0].chunk_id == "chk_rare"


def test_several_specific_bridges_still_outweigh_one() -> None:
    """The weights sum, so the arm can still prefer a chunk reached by more
    than one narrow link -- what it must not do is prefer one reached by more
    *hubs*."""

    A = ("alpha", "thing", "Alpha")
    B = ("beta", "thing", "Beta")

    async def scenario(store: PostgresKnowledgeGraphStore) -> Any:
        await _record(
            store, chunk_id="chk_seed", document_id="doc_seed", entities=(A, B)
        )
        # Reached by both narrow entities.
        await _record(
            store, chunk_id="chk_both", document_id="doc_both", entities=(A, B)
        )
        # Reached by one.
        await _record(store, chunk_id="chk_one", document_id="doc_one", entities=(A,))
        return await store.expand_from_seeds(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            graph_identity=IDENTITY,
            seed_chunk_ids=("chk_seed",),
            limit=10,
        )

    found = _run(scenario)

    assert found[0].chunk_id == "chk_both"
    assert found[0].score > found[1].score
