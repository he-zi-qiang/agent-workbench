"""The Qdrant index, against a real Qdrant.

An in-memory double would reproduce the parts nobody gets wrong and none of the
parts that matter: how a payload filter composes, whether a named vector is
queryable, what a mismatched collection does. Those are the reasons this
adapter exists, so they are tested against the thing itself -- the same choice
the PostgreSQL suites already make.

Vectors here are hand-written, not embedded. What is under test is the index,
and a model in the loop would make every assertion depend on something that
has its own reasons to change.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient, models

from agent_workbench.adapters.vector import QdrantVectorIndex, point_id
from agent_workbench.domain.errors import IncompatibleSchemaError
from agent_workbench.ports.vector_index import IndexedChunk

QDRANT_URL_ENV_VAR = "AGENT_WORKBENCH_TEST_QDRANT_URL"

TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"
KB = "kb_main"
OTHER_KB = "kb_other"
OWNER = "user_owner"
NEIGHBOUR = "user_neighbour"

SIZE = 4
# Deliberately axis-aligned: cosine distance between them is then obvious from
# reading the test, rather than something to take on trust.
NORTH = (1.0, 0.0, 0.0, 0.0)
EAST = (0.0, 1.0, 0.0, 0.0)
SOUTH = (0.0, 0.0, 1.0, 0.0)


def _url() -> str:
    url = os.environ.get(QDRANT_URL_ENV_VAR)
    if not url:
        pytest.skip(f"{QDRANT_URL_ENV_VAR} is not set")
    return url


def _chunk(
    chunk_id: str,
    vector: tuple[float, ...],
    *,
    tenant_id: str = TENANT,
    knowledge_base_id: str = KB,
    document_id: str = "doc_1",
    document_version: str = "ver_1",
    granted: tuple[str, ...] = (OWNER,),
    source_revision: int = 1,
    ordinal: int = 0,
    text: str = "Dense and sparse retrieval are fused once per query.",
) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        document_version=document_version,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        owner_id=OWNER,
        authorized_principals=granted,
        source_revision=source_revision,
        text=text,
        ordinal=ordinal,
        vector=vector,
    )


def _run(scenario: Callable[[QdrantVectorIndex], Awaitable[Any]]) -> Any:
    """One scenario against a collection nothing else touches."""

    url = _url()
    collection = f"test_{uuid.uuid4().hex}"

    async def execute() -> Any:
        client = AsyncQdrantClient(url=url)
        try:
            index = QdrantVectorIndex(client, collection=collection)
            await index.ensure_collection(vector_size=SIZE)
            return await scenario(index)
        finally:
            try:
                await client.delete_collection(collection)
            finally:
                await client.close()

    return asyncio.run(execute())


# --- the collection ----------------------------------------------------------


def test_ensuring_the_collection_twice_is_harmless() -> None:
    """Every process calls it at startup, so it has to be idempotent."""

    async def scenario(index: QdrantVectorIndex) -> int:
        await index.ensure_collection(vector_size=SIZE)
        return await index.upsert((_chunk("chk_1", NORTH),))

    assert _run(scenario) == 1


def test_a_collection_of_the_wrong_dimension_is_refused() -> None:
    """Recreating it would discard an index something is querying right now."""

    async def scenario(index: QdrantVectorIndex) -> None:
        await index.ensure_collection(vector_size=SIZE + 1)

    with pytest.raises(IncompatibleSchemaError, match="dimensional"):
        _run(scenario)


def test_read_alias_queries_the_active_generation_not_the_write_collection() -> None:
    """A blue/green alias must select retrieval's collection at query time."""

    async def scenario() -> tuple[int, str]:
        client = AsyncQdrantClient(url=_url())
        write_collection = f"test_write_{uuid.uuid4().hex}"
        active_collection = f"test_active_{uuid.uuid4().hex}"
        alias = f"test_alias_{uuid.uuid4().hex}"
        try:
            write = QdrantVectorIndex(client, collection=write_collection)
            active = QdrantVectorIndex(client, collection=active_collection)
            await write.ensure_collection(vector_size=SIZE)
            await active.ensure_collection(vector_size=SIZE)
            # The write collection deliberately remains empty. The only point
            # is in the active generation, so a successful query proves the
            # index passed the alias rather than the write collection.
            await active.upsert((_chunk("chk_active", NORTH),))
            await client.update_collection_aliases(
                [
                    models.CreateAliasOperation(
                        create_alias=models.CreateAlias(
                            collection_name=active_collection,
                            alias_name=alias,
                        )
                    )
                ]
            )
            reader = QdrantVectorIndex(client, collection=alias)
            hits = await reader.search(
                vector=NORTH,
                tenant_id=TENANT,
                knowledge_base_id=KB,
                authorized_principals=(OWNER,),
                limit=10,
            )
            return len(hits), hits[0].chunk_id
        finally:
            with suppress(Exception):
                await client.update_collection_aliases(
                    [
                        models.DeleteAliasOperation(
                            delete_alias=models.DeleteAlias(alias_name=alias)
                        )
                    ]
                )
            for collection in (write_collection, active_collection):
                with suppress(Exception):
                    await client.delete_collection(collection)
            await client.close()

    assert asyncio.run(scenario()) == (1, "chk_active")


# --- idempotent writes -------------------------------------------------------


def test_re_indexing_the_same_chunk_overwrites_it() -> None:
    """At-least-once delivery plus a generated id is a pile of duplicates."""

    async def scenario(index: QdrantVectorIndex) -> tuple[int, str]:
        await index.upsert((_chunk("chk_1", NORTH, text="first"),))
        await index.upsert((_chunk("chk_1", NORTH, text="second"),))
        hits = await index.search(
            vector=NORTH,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=10,
        )
        return len(hits), hits[0].text

    assert _run(scenario) == (1, "second")


def test_the_point_id_is_derived_from_the_chunk_id() -> None:
    """Stable across processes and releases, or re-indexing is not idempotent."""

    assert point_id("chk_1") == point_id("chk_1")
    assert point_id("chk_1") != point_id("chk_2")


# --- what a query is allowed to see ------------------------------------------


def test_another_tenants_chunk_is_not_a_candidate() -> None:
    async def scenario(index: QdrantVectorIndex) -> int:
        await index.upsert((_chunk("chk_1", NORTH, tenant_id=OTHER_TENANT),))
        hits = await index.search(
            vector=NORTH,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=10,
        )
        return len(hits)

    assert _run(scenario) == 0


def test_another_knowledge_base_is_not_a_candidate() -> None:
    async def scenario(index: QdrantVectorIndex) -> int:
        await index.upsert((_chunk("chk_1", NORTH, knowledge_base_id=OTHER_KB),))
        hits = await index.search(
            vector=NORTH,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=10,
        )
        return len(hits)

    assert _run(scenario) == 0


def test_a_chunk_granted_to_nobody_relevant_is_not_a_candidate() -> None:
    """The payload filter narrows. It is not the authorization decision."""

    async def scenario(index: QdrantVectorIndex) -> int:
        await index.upsert((_chunk("chk_1", NORTH, granted=(OWNER,)),))
        hits = await index.search(
            vector=NORTH,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(NEIGHBOUR,),
            limit=10,
        )
        return len(hits)

    assert _run(scenario) == 0


def test_a_granted_principal_sees_the_chunk() -> None:
    """The control: the filter refuses by grant, not by existing."""

    async def scenario(index: QdrantVectorIndex) -> int:
        await index.upsert((_chunk("chk_1", NORTH, granted=(OWNER, NEIGHBOUR)),))
        hits = await index.search(
            vector=NORTH,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(NEIGHBOUR,),
            limit=10,
        )
        return len(hits)

    assert _run(scenario) == 1


def test_an_empty_principal_set_matches_nothing() -> None:
    """Asking on behalf of nobody is not the same as asking for everything."""

    async def scenario(index: QdrantVectorIndex) -> int:
        await index.upsert((_chunk("chk_1", NORTH),))
        hits = await index.search(
            vector=NORTH,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(),
            limit=10,
        )
        return len(hits)

    assert _run(scenario) == 0


# --- filtering is in the query, not after it ---------------------------------


def test_the_limit_counts_only_permitted_candidates() -> None:
    """The reason the filter has to be in the statement.

    Two chunks the caller may not see sit nearer the query than the one it
    may. Narrowing after a limit of one would return an empty page and call it
    the end of the results.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[int, str]:
        await index.upsert(
            (
                _chunk("chk_hidden_a", NORTH, granted=(NEIGHBOUR,), ordinal=0),
                _chunk("chk_hidden_b", NORTH, granted=(NEIGHBOUR,), ordinal=1),
                _chunk("chk_visible", EAST, granted=(OWNER,), ordinal=2),
            )
        )
        hits = await index.search(
            vector=NORTH,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=1,
        )
        return len(hits), hits[0].chunk_id

    assert _run(scenario) == (1, "chk_visible")


def test_results_come_back_nearest_first() -> None:
    async def scenario(index: QdrantVectorIndex) -> list[str]:
        await index.upsert(
            (
                _chunk("chk_near", NORTH, ordinal=0),
                _chunk("chk_far", SOUTH, ordinal=1),
            )
        )
        hits = await index.search(
            vector=NORTH,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=10,
        )
        return [hit.chunk_id for hit in hits]

    assert _run(scenario) == ["chk_near", "chk_far"]


# --- what a candidate carries back -------------------------------------------


def test_a_candidate_carries_what_a_citation_needs() -> None:
    """A citation the reader cannot follow is not a citation."""

    async def scenario(index: QdrantVectorIndex) -> Any:
        await index.upsert(
            (
                _chunk(
                    "chk_1",
                    NORTH,
                    document_version="ver_7",
                    source_revision=3,
                    ordinal=5,
                    text="Qdrant performs one fusion per query.",
                ),
            )
        )
        hits = await index.search(
            vector=NORTH,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=1,
        )
        return hits[0]

    hit = _run(scenario)

    assert hit.document_id == "doc_1"
    assert hit.document_version == "ver_7"
    assert hit.source_revision == 3
    assert hit.ordinal == 5
    assert hit.text == "Qdrant performs one fusion per query."


# --- removal -----------------------------------------------------------------


def test_deleting_a_document_removes_all_of_its_chunks() -> None:
    async def scenario(index: QdrantVectorIndex) -> int:
        await index.upsert(
            (
                _chunk("chk_1", NORTH, document_id="doc_1", ordinal=0),
                _chunk("chk_2", EAST, document_id="doc_1", ordinal=1),
                _chunk("chk_3", SOUTH, document_id="doc_2", ordinal=0),
            )
        )
        await index.delete_document(tenant_id=TENANT, document_id="doc_1")
        hits = await index.search(
            vector=NORTH,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=10,
        )
        return len(hits)

    assert _run(scenario) == 1


def test_deleting_does_not_reach_into_another_tenant() -> None:
    """A document id is not unique across tenants, and deletion is permanent."""

    async def scenario(index: QdrantVectorIndex) -> int:
        await index.upsert(
            (
                _chunk("chk_mine", NORTH, tenant_id=TENANT, document_id="doc_1"),
                _chunk(
                    "chk_theirs", NORTH, tenant_id=OTHER_TENANT, document_id="doc_1"
                ),
            )
        )
        await index.delete_document(tenant_id=TENANT, document_id="doc_1")
        hits = await index.search(
            vector=NORTH,
            tenant_id=OTHER_TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=10,
        )
        return len(hits)

    assert _run(scenario) == 1
