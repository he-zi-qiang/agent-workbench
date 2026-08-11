"""The graph arm, against real Qdrant and real PostgreSQL.

The test that matters is ``test_the_bridge_document_surfaces``: it reproduces
the measured failure -- an anchor document carrying the query's words, a bridge
document carrying none of them, and enough distractors sharing the anchor's
vocabulary to fill top-k -- and asserts the arm reaches what the other two
cannot. Every other test here guards a way that could be true accidentally.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text

from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.adapters.persistence.knowledge_graph import (
    PostgresKnowledgeGraphStore,
)
from agent_workbench.adapters.retrieval.seed_expansion import SeedExpansionRetriever
from agent_workbench.adapters.vector.qdrant import QdrantVectorIndex
from agent_workbench.ports.vector_index import IndexedChunk

QDRANT_URL_ENV_VAR = "AGENT_WORKBENCH_TEST_QDRANT_URL"
TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TENANT = "tenant_a"
KB = "kb_main"
OWNER = "user_owner"
IDENTITY = "extractor+v1+fake"
SIZE = 4

# The query points at the anchor. The bridge shares no vector direction with
# it -- only the entity the anchor names.
QUERY = (1.0, 0.0, 0.0, 0.0)
ANCHOR_VECTOR = (0.99, 0.14, 0.0, 0.0)
NEIGHBOUR_VECTOR = (0.97, 0.24, 0.0, 0.0)
BRIDGE_VECTOR = (0.0, 0.0, 1.0, 0.0)

MARLIN = ("team marlin", "team", "Team Marlin")


class _Embedder:
    identity = "fake"

    async def embed_query(self, text: str) -> tuple[float, ...]:
        return QUERY

    async def embed_documents(self, texts: tuple[str, ...]):  # pragma: no cover
        return tuple(QUERY for _ in texts)


def _urls() -> tuple[str, str]:
    url = os.environ.get(QDRANT_URL_ENV_VAR)
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not url:
        pytest.skip(f"{QDRANT_URL_ENV_VAR} is not set")
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return url, dsn


def _chunk(
    name: str, vector: tuple[float, ...], *, principals: tuple[str, ...] = (OWNER,)
) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=f"chk_{name}",
        document_id=f"doc_{name}",
        document_version="ver_1",
        tenant_id=TENANT,
        knowledge_base_id=KB,
        owner_id=OWNER,
        authorized_principals=principals,
        source_revision=1,
        text=name,
        ordinal=0,
        vector=vector,
    )


def _run(
    scenario: Callable[
        [SeedExpansionRetriever, PostgresKnowledgeGraphStore], Awaitable[Any]
    ],
    *,
    bridge_principals: tuple[str, ...] = (OWNER,),
) -> Any:
    url, dsn = _urls()
    collection = f"test_{uuid.uuid4().hex}"

    async def execute() -> Any:
        client = AsyncQdrantClient(url=url)
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("TRUNCATE kg_relations, kg_mentions, kg_entities CASCADE")
                )
            index = QdrantVectorIndex(client, collection=collection)
            await index.ensure_collection(vector_size=SIZE)
            await index.upsert(
                (
                    _chunk("anchor", ANCHOR_VECTOR),
                    # Distractors: near the anchor, and enough of them to fill
                    # every slot the other arms have.
                    _chunk("near_a", NEIGHBOUR_VECTOR),
                    _chunk("near_b", NEIGHBOUR_VECTOR),
                    _chunk("bridge", BRIDGE_VECTOR, principals=bridge_principals),
                )
            )
            store = PostgresKnowledgeGraphStore(engine)
            # The anchor names Marlin; so does the bridge. Nothing else does.
            for chunk_id, document_id in (
                ("chk_anchor", "doc_anchor"),
                ("chk_bridge", "doc_bridge"),
            ):
                await store.record_chunk(
                    tenant_id=TENANT,
                    knowledge_base_id=KB,
                    document_id=document_id,
                    document_version="ver_1",
                    chunk_id=chunk_id,
                    graph_identity=IDENTITY,
                    entities=(MARLIN,),
                    relations=(),
                )
            retriever = SeedExpansionRetriever(
                embedder=_Embedder(),
                index=index,
                graph=store,
                graph_identity=IDENTITY,
                seed_count=1,
            )
            return await scenario(retriever, store)
        finally:
            try:
                await client.delete_collection(collection)
            finally:
                await client.close()
                await engine.dispose()

    return asyncio.run(execute())


async def _documents(
    retriever: SeedExpansionRetriever, *, limit: int = 3
) -> tuple[str, ...]:
    hits = await retriever.candidates(
        query="who carries the rotation",
        tenant_id=TENANT,
        principal_id=OWNER,
        knowledge_base_id=KB,
        limit=limit,
    )
    return tuple(hit.document_id for hit in hits)


def test_the_bridge_document_surfaces() -> None:
    """The measured failure, reproduced and fixed.

    doc_bridge is orthogonal to the query in vector space -- the only thing
    connecting it is the entity doc_anchor names. Without the graph arm it
    cannot be in the top 3; the control below is the same fixture with the arm
    unable to contribute.
    """

    async def scenario(retriever: SeedExpansionRetriever, store: Any) -> Any:
        return await _documents(retriever)

    found = _run(scenario)

    assert "doc_anchor" in found
    assert "doc_bridge" in found


def test_without_the_graph_the_bridge_is_out_of_reach() -> None:
    """The control. Same corpus, same query, an identity nothing was written
    under -- so the arm finds nothing and the distractors keep the slots."""

    async def scenario(retriever: SeedExpansionRetriever, store: Any) -> Any:
        blind = SeedExpansionRetriever(
            embedder=retriever.embedder,
            index=retriever.index,
            graph=retriever.graph,
            graph_identity="a different extractor",
            seed_count=1,
        )
        return await _documents(blind)

    found = _run(scenario)

    assert "doc_anchor" in found
    assert "doc_bridge" not in found


def test_the_arm_never_re_nominates_its_own_seed() -> None:
    """A seed returned by the graph would take a second rank in a fusion that
    counts ranks, inflating exactly the documents that needed no help."""

    async def scenario(retriever: SeedExpansionRetriever, store: Any) -> Any:
        return await store.expand_from_seeds(
            tenant_id=TENANT,
            knowledge_base_id=KB,
            graph_identity=IDENTITY,
            seed_chunk_ids=("chk_anchor",),
            limit=10,
        )

    nominations = _run(scenario)

    assert [n.chunk_id for n in nominations] == ["chk_bridge"]


def test_a_nomination_this_principal_may_not_read_is_not_returned() -> None:
    """The graph must not become a way around the index narrowing.

    doc_bridge is nominated exactly as in the first test -- the mention rows
    are identical -- and differs only in whose ACL copy it carries. This is the
    index-side narrowing, not the authorization: RetrievalService still
    re-checks every survivor against PostgreSQL.
    """

    async def scenario(retriever: SeedExpansionRetriever, store: Any) -> Any:
        return await _documents(retriever)

    found = _run(scenario, bridge_principals=("user_stranger",))

    assert "doc_anchor" in found
    assert "doc_bridge" not in found


def test_a_graph_that_fails_degrades_to_the_other_arms() -> None:
    """Retrieval that failed because its optional arm did would be worse than
    the degradation."""

    class _Broken:
        async def expand_from_seeds(self, **kwargs: Any):
            raise RuntimeError("the graph is unavailable")

    async def scenario(retriever: SeedExpansionRetriever, store: Any) -> Any:
        broken = SeedExpansionRetriever(
            embedder=retriever.embedder,
            index=retriever.index,
            graph=_Broken(),  # type: ignore[arg-type]
            graph_identity=IDENTITY,
            seed_count=1,
        )
        return await _documents(broken)

    found = _run(scenario)

    assert "doc_anchor" in found
    assert "doc_bridge" not in found
