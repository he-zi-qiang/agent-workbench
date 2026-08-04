"""The same query, twice, returns the same order -- including where scores tie.

Qdrant orders by score and says nothing about equal scores, so it returns those
in whatever order it happened to produce. That was measured rather than
assumed: over the 38-question evaluation set, repeating one hybrid query gave a
different chunk order on 9 of 38 questions and a different top-3 *document*
list on 3 of 38.

Two things break when that happens, and only one of them is visible in a
benchmark. An evaluation cannot compare two retrievers when each disagrees with
itself more than they differ from each other -- that is what blocked ADR-017's
equivalence step. The other is a product defect: ``RetrievalService`` cuts to
top_k *after* this, so a chunk that loses a coin flip at rank 3 is not
demoted, it is gone, and the same question asked twice can cite different
sources.

The ties here are constructed rather than hoped for. Points sharing one vector
score identically by construction, so the tie exists on every run of this test
rather than on the runs where the corpus happens to produce one.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.ports.vector_index import IndexedChunk

QDRANT_URL_ENV_VAR = "AGENT_WORKBENCH_TEST_QDRANT_URL"

TENANT = "tenant_a"
KB = "kb_main"
OWNER = "user_owner"
SIZE = 4

#: Every tied point carries this vector, so the engine scores them equally and
#: has nothing but its own internals to order them by.
SHARED = (1.0, 0.0, 0.0, 0.0)
QUERY = (1.0, 0.02, 0.0, 0.0)
#: Strictly the best match, because it *is* the query direction -- cosine 1.0
#: against SHARED's 0.9998.
#:
#: The obvious-looking choice, a vector "past" the query such as
#: (1.0, 0.05, 0, 0), is wrong here and quietly so: the collection is cosine,
#: which measures angle, and that vector sits 1.7 degrees off the query while
#: SHARED sits 1.15 degrees off it. It scores *worse* than the points it was
#: meant to outrank, and the control below would then be asserting the
#: opposite of what it says.
NEARER = QUERY

#: Deliberately not in sorted order, and deliberately not in an order that
#: matches insertion. If the tie-break were "whatever came back" or "insertion
#: order", the expected list below would be a different one.
TIED_NAMES = ("mango", "apple", "pear", "cherry", "banana")

TERM = 4242


def _url() -> str:
    url = os.environ.get(QDRANT_URL_ENV_VAR)
    if not url:
        pytest.skip(f"{QDRANT_URL_ENV_VAR} is not set")
    return url


def _chunk(name: str, vector: tuple[float, ...]) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=f"chk_{name}",
        document_id=f"doc_{name}",
        document_version="ver_1",
        tenant_id=TENANT,
        knowledge_base_id=KB,
        owner_id=OWNER,
        authorized_principals=(OWNER,),
        source_revision=1,
        text=name,
        ordinal=0,
        vector=vector,
        sparse_indices=(TERM,),
        sparse_values=(1.0,),
    )


def _run(scenario: Callable[[QdrantVectorIndex], Awaitable[Any]]) -> Any:
    url = _url()
    collection = f"test_{uuid.uuid4().hex}"

    async def execute() -> Any:
        client = AsyncQdrantClient(url=url)
        try:
            index = QdrantVectorIndex(client, collection=collection)
            await index.ensure_collection(vector_size=SIZE)
            await index.upsert(
                (
                    *(_chunk(name, SHARED) for name in TIED_NAMES),
                    _chunk("zenith", NEARER),
                )
            )
            return await scenario(index)
        finally:
            try:
                await client.delete_collection(collection)
            finally:
                await client.close()

    return asyncio.run(execute())


async def _dense(index: QdrantVectorIndex) -> tuple[str, ...]:
    hits = await index.search(
        vector=QUERY,
        tenant_id=TENANT,
        knowledge_base_id=KB,
        authorized_principals=(OWNER,),
        limit=10,
    )
    return tuple(hit.chunk_id for hit in hits)


async def _hybrid(index: QdrantVectorIndex) -> tuple[str, ...]:
    hits = await index.search_hybrid(
        vector=QUERY,
        sparse_indices=(TERM,),
        sparse_values=(1.0,),
        tenant_id=TENANT,
        knowledge_base_id=KB,
        authorized_principals=(OWNER,),
        limit=10,
        dense_limit=10,
        sparse_limit=10,
    )
    return tuple(hit.chunk_id for hit in hits)


def test_the_tie_is_real() -> None:
    """Without this the rest of the file could pass on scores that never tied.

    A test asserting a stable order over six distinct scores asserts only that
    sorting works. It has to be shown that the engine really is being handed a
    decision it has no rule for.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[float, ...]:
        hits = await index.search(
            vector=QUERY,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=10,
        )
        return tuple(hit.score for hit in hits)

    scores = _run(scenario)

    assert len(scores) == len(TIED_NAMES) + 1
    # One strictly-best point, and the rest sharing a single value.
    assert len(set(scores)) == 2
    assert scores.count(scores[0]) == 1


def test_a_repeated_dense_query_returns_the_same_order() -> None:
    """Recorded as not currently falsifiable, rather than counted as coverage.

    Deleting the tie-break leaves this green: on this fixture the dense arm
    already returned equal-scoring points in a consistent order, and the
    hybrid test below is the one that goes red. That matches what the
    evaluation showed -- the dense arm reproduced across runs while hybrid did
    not -- so this is kept as a regression guard on a property that holds
    today, and not offered as evidence that the tie-break works.
    """

    async def scenario(index: QdrantVectorIndex) -> list[tuple[str, ...]]:
        return [await _dense(index) for _ in range(8)]

    runs = _run(scenario)

    assert len(set(runs)) == 1, f"order varied across identical queries: {set(runs)}"


def test_a_repeated_hybrid_query_returns_the_same_order() -> None:
    """RRF is where this bites hardest -- it scores by rank sums, so it ties."""

    async def scenario(index: QdrantVectorIndex) -> list[tuple[str, ...]]:
        return [await _hybrid(index) for _ in range(8)]

    runs = _run(scenario)

    assert len(set(runs)) == 1, f"order varied across identical queries: {set(runs)}"


def test_tied_candidates_are_ordered_by_chunk_id() -> None:
    """Stable is not enough; it has to be stable on something re-indexable.

    A tie-break on the engine's own ordering would satisfy the repetition tests
    above while still changing after a re-index. ``chunk_id`` is derived from
    the chunk, so it survives one.
    """

    order = _run(_dense)
    tied = order[1:]

    assert order[0] == "chk_zenith"
    assert tied == tuple(sorted(tied))
    assert tied == tuple(sorted(f"chk_{name}" for name in TIED_NAMES))


def test_a_higher_score_still_outranks_a_lower_chunk_id() -> None:
    """The control: the tie-break must not become the ranking.

    ``chk_zenith`` sorts last alphabetically and scores highest. If ordering by
    id had escaped the tie and become the sort, it would come back last.
    """

    order = _run(_dense)

    assert order[0] == "chk_zenith"
    assert order[0] > order[-1]


def test_the_hybrid_and_dense_paths_agree_on_the_tie_break() -> None:
    """Both go through the same mapping, and a report may compare them."""

    async def scenario(index: QdrantVectorIndex) -> tuple[tuple[str, ...], ...]:
        return (await _dense(index), await _hybrid(index))

    dense, hybrid = _run(scenario)

    assert set(dense) == set(hybrid)
    assert sorted(dense[1:]) == sorted(hybrid[1:])
