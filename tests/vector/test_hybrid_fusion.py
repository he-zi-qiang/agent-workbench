"""Dense and sparse, fused once by Qdrant.

The hard part to test is not that a fused query returns something -- a dense
query returns the same something. It is that the sparse arm contributes: a
point reachable only by term overlap has to surface, and one reachable only by
meaning has to keep surfacing. A fusion where one arm does nothing looks
exactly like the other arm alone, which is how a broken sparse encoder hides
(ADR-013).
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

# Two points that no single arm can both find:
#  - NEAR is close in dense space and shares no query terms.
#  - LEXICAL shares the query's rare term and is far in dense space.
NEAR_VECTOR = (1.0, 0.0, 0.0, 0.0)
FAR_VECTOR = (0.0, 0.0, 0.0, 1.0)
QUERY_VECTOR = (0.98, 0.2, 0.0, 0.0)
RARE_TERM = 4242


def _url() -> str:
    url = os.environ.get(QDRANT_URL_ENV_VAR)
    if not url:
        pytest.skip(f"{QDRANT_URL_ENV_VAR} is not set")
    return url


def _chunk(
    name: str,
    vector: tuple[float, ...],
    *,
    terms: tuple[int, ...] = (),
    principals: tuple[str, ...] = (OWNER,),
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
        sparse_indices=terms,
        sparse_values=tuple(1.0 for _ in terms),
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
                    _chunk("near", NEAR_VECTOR),
                    _chunk("lexical", FAR_VECTOR, terms=(RARE_TERM,)),
                    # Nearer to the query than doc_near, and readable by
                    # somebody else. It exists to occupy the dense prefetch
                    # slot if the narrowing is applied too late.
                    _chunk(
                        "stranger",
                        QUERY_VECTOR,
                        terms=(RARE_TERM,),
                        principals=("user_stranger",),
                    ),
                )
            )
            return await scenario(index)
        finally:
            try:
                await client.delete_collection(collection)
            finally:
                await client.close()

    return asyncio.run(execute())


async def _hybrid(index: QdrantVectorIndex, **overrides: Any) -> tuple[str, ...]:
    hits = await index.search_hybrid(
        vector=overrides.get("vector", QUERY_VECTOR),
        sparse_indices=overrides.get("indices", (RARE_TERM,)),
        sparse_values=overrides.get("values", (1.0,)),
        tenant_id=overrides.get("tenant", TENANT),
        knowledge_base_id=KB,
        authorized_principals=overrides.get("principals", (OWNER,)),
        limit=10,
        # One each, deliberately. With a larger prefetch the dense arm reaches
        # both points on its own -- there are only two -- and every assertion
        # below would pass with the sparse arm deleted. A limit that cannot
        # exclude anything cannot demonstrate anything.
        dense_limit=overrides.get("dense_limit", 1),
        sparse_limit=overrides.get("sparse_limit", 1),
    )
    return tuple(hit.document_id for hit in hits)


def test_dense_alone_does_not_find_the_lexical_match() -> None:
    """Establishes the gap the sparse arm has to close.

    Without this, the fusion test below would pass even if sparse did nothing.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[str, ...]:
        hits = await index.search(
            vector=QUERY_VECTOR,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=1,
        )
        return tuple(hit.document_id for hit in hits)

    assert _run(scenario) == ("doc_near",)


def test_fusion_surfaces_what_only_the_sparse_arm_reaches() -> None:
    """The point of hybrid: a term match a dense query ranked last."""

    assert "doc_lexical" in _run(_hybrid)


def test_fusion_keeps_what_only_the_dense_arm_reaches() -> None:
    """Adding sparse must not cost the semantic match."""

    assert "doc_near" in _run(_hybrid)


def test_a_query_with_no_terms_still_returns_dense_candidates() -> None:
    """An empty sparse arm degrades to dense, rather than returning nothing."""

    async def scenario(index: QdrantVectorIndex) -> tuple[str, ...]:
        return await _hybrid(index, indices=(), values=())

    assert "doc_near" in _run(scenario)


def test_a_point_without_sparse_weights_is_still_retrievable() -> None:
    """A collection may hold both while sparse is rolled out."""

    async def scenario(index: QdrantVectorIndex) -> tuple[str, ...]:
        return await _hybrid(index)

    # doc_near carries no sparse vector at all.
    assert "doc_near" in _run(scenario)


# --- the narrowing applies to both arms --------------------------------------


def test_another_tenant_gets_nothing_from_either_arm() -> None:
    """The filter is repeated on each prefetch, not applied after fusion."""

    async def scenario(index: QdrantVectorIndex) -> tuple[str, ...]:
        return await _hybrid(index, tenant="tenant_b")

    assert _run(scenario) == ()


def test_a_principal_sees_only_what_is_granted_to_them() -> None:
    """A term match is not a way around the ACL narrowing.

    The stranger has a point of their own, so this asserts what they *can*
    reach as well as what they cannot. "Returns nothing" would have been the
    weaker claim -- an index that returned nothing to everybody would satisfy
    it.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[str, ...]:
        return await _hybrid(index, principals=("user_stranger",))

    found = _run(scenario)

    assert "doc_stranger" in found
    assert "doc_near" not in found
    assert "doc_lexical" not in found


def test_a_nearer_unauthorized_point_does_not_displace_an_authorized_one() -> None:
    """doc_stranger is nearer the query and shares its rare term.

    With a prefetch of one per arm it would win both if the narrowing did not
    apply first. What this does *not* establish is where the filter is
    written: Qdrant pushes an outer filter down to the prefetches, so moving it
    onto the fusion query passes this too. Said here because the code comment
    beside that filter used to claim otherwise, and a test that cannot tell two
    spellings apart should not be read as choosing between them.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[str, ...]:
        return await _hybrid(index)

    found = _run(scenario)

    assert "doc_stranger" not in found
    assert "doc_near" in found
    assert "doc_lexical" in found


# --- the raw sparse arm (B1: what a >2-arm retriever has to start from) ------


async def _sparse(index: QdrantVectorIndex, **overrides: Any) -> tuple[str, ...]:
    hits = await index.search_sparse(
        sparse_indices=overrides.get("indices", (RARE_TERM,)),
        sparse_values=overrides.get("values", (1.0,)),
        tenant_id=overrides.get("tenant", TENANT),
        knowledge_base_id=KB,
        authorized_principals=overrides.get("principals", (OWNER,)),
        limit=overrides.get("limit", 10),
    )
    return tuple(hit.document_id for hit in hits)


def test_the_sparse_arm_queries_the_lexical_vector_not_the_dense_one() -> None:
    """Which vector was asked, established by what cannot come back.

    doc_near is the control and carries no sparse vector at all, so it is
    unreachable by a sparse query however the terms score. An implementation
    that quietly answered with the dense vector -- or with a fused list --
    returns it and fails here.

    Deliberately *not* named for term overlap: Qdrant returns a point that
    carries a sparse vector even when no query term matches it, scoring the
    miss zero rather than dropping the point. Membership therefore says which
    vector was queried and nothing about whether the terms met. That claim is
    the next test's, and separating them is why a sabotage that broke the
    terms was caught by exactly one of the two.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[str, ...]:
        return await _sparse(index)

    found = _run(scenario)

    assert "doc_lexical" in found
    assert "doc_near" not in found


def test_a_matching_term_outscores_a_missing_one() -> None:
    """What membership cannot say: the overlap is what produced the score.

    Both queries return doc_lexical -- see above -- so only the scores
    separate a query whose term is in the chunk from one whose term is not.
    Measured on this fixture: weight 1.0 on the stored term scores 1.0, and a
    term the chunk does not carry scores 0.0.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[float, float]:
        async def score(term: int) -> float:
            hits = await index.search_sparse(
                sparse_indices=(term,),
                sparse_values=(1.0,),
                tenant_id=TENANT,
                knowledge_base_id=KB,
                authorized_principals=(OWNER,),
                limit=10,
            )
            return next(h.score for h in hits if h.document_id == "doc_lexical")

        return await score(RARE_TERM), await score(RARE_TERM + 1)

    matched, missed = _run(scenario)

    assert matched > missed
    assert missed == 0.0


def test_the_sparse_arm_is_unfused_so_its_scores_are_the_engine_s() -> None:
    """What separates this from ``search_hybrid``: no RRF has touched it.

    A fused score here is ``1/(2+rank)`` -- 0.5 at the top, and never above
    1.0 for a chunk one arm returned. The raw arm carries the engine's own
    sparse similarity, which for this fixture is the dot product of the term
    weights and is greater than 1. Asserting the *shape* of the number rather
    than its value keeps this from breaking when the fixture's weights change.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[float, ...]:
        hits = await index.search_sparse(
            sparse_indices=(RARE_TERM,),
            sparse_values=(2.0,),
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=10,
        )
        return tuple(hit.score for hit in hits)

    scores = _run(scenario)

    assert scores, "the lexical point should have been returned"
    # 1/(RRF_K + 0) = 0.5 is the largest a single-arm fused score can be.
    assert max(scores) > 0.5


def test_the_sparse_arm_narrows_by_principal_like_every_other_search() -> None:
    async def scenario(index: QdrantVectorIndex) -> tuple[str, ...]:
        return await _sparse(index, principals=("user_stranger",))

    found = _run(scenario)

    assert "doc_stranger" in found
    assert "doc_lexical" not in found


def test_the_sparse_arm_refuses_the_same_inputs_search_refuses() -> None:
    """Mirrored guards, asserted rather than assumed.

    ``search_hybrid`` deliberately does *not* carry these -- see its
    docstring -- so the two methods really do differ here, and a test is what
    keeps that difference a decision rather than an oversight.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[Any, ...]:
        with pytest.raises(ValueError, match="limit must be positive"):
            await _sparse(index, limit=0)
        empty = await _sparse(index, principals=())
        return (empty,)

    (empty,) = _run(scenario)

    assert empty == ()
