"""One document version, end to end into a real Qdrant.

The parts have their own tests; this is about what happens when they are joined
-- chunk ids that survive a re-index, an ACL that reaches the payload, and a
document whose embedding fails leaving nothing behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from agent_workbench.adapters.embedding import DeterministicEmbedder
from agent_workbench.adapters.ingestion import (
    ApproximateTokenCounter,
    TextDocumentParser,
)
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.ingestion import IngestionRequest, IngestionService
from agent_workbench.ports.embedding import Vector

QDRANT_URL_ENV_VAR = "AGENT_WORKBENCH_TEST_QDRANT_URL"

TENANT = "tenant_a"
KB = "kb_main"
OWNER = "user_owner"
NEIGHBOUR = "user_neighbour"
SIZE = 8

PASSAGE = (
    "Dense retrieval finds passages by meaning. Sparse retrieval finds them by "
    "term overlap. Fusing the two happens once, inside Qdrant, so no ranking is "
    "invented twice. Reranking runs afterwards on the fused candidates."
)


def _url() -> str:
    url = os.environ.get(QDRANT_URL_ENV_VAR)
    if not url:
        pytest.skip(f"{QDRANT_URL_ENV_VAR} is not set")
    return url


def _request(**overrides: Any) -> IngestionRequest:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "knowledge_base_id": KB,
        "document_id": "doc_1",
        "document_version": "ver_1",
        "owner_id": OWNER,
        "authorized_principals": (OWNER,),
        "source_revision": 1,
        "media_type": "text/markdown",
        "content": PASSAGE.encode(),
    }
    fields.update(overrides)
    return IngestionRequest(**fields)


def _service(index: QdrantVectorIndex, **overrides: Any) -> IngestionService:
    return IngestionService(
        parser=TextDocumentParser(),
        chunker=overrides.get(
            "chunker",
            Chunker(size_tokens=8, overlap_tokens=2, counter=ApproximateTokenCounter()),
        ),
        embedder=overrides.get("embedder", DeterministicEmbedder(dimension=SIZE)),
        index=index,
        sparse_encoder=overrides.get("sparse_encoder"),
    )


def _run(scenario: Callable[[QdrantVectorIndex], Awaitable[Any]]) -> Any:
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


def test_a_document_becomes_retrievable_chunks() -> None:
    async def scenario(index: QdrantVectorIndex) -> tuple[int, int]:
        service = _service(index)
        written = await service.ingest(_request())
        hits = await index.search(
            vector=await service.embedder.embed_query("how is fusion done"),
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=50,
        )
        return len(written), len(hits)

    written, retrieved = _run(scenario)

    assert written > 1
    assert retrieved == written


def test_re_ingesting_the_same_version_does_not_duplicate_it() -> None:
    """At-least-once delivery is the normal case, not the exception."""

    async def scenario(index: QdrantVectorIndex) -> tuple[int, int]:
        service = _service(index)
        first = await service.ingest(_request())
        await service.ingest(_request())
        hits = await index.search(
            vector=await service.embedder.embed_query("fusion"),
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=50,
        )
        return len(first), len(hits)

    written, retrieved = _run(scenario)

    assert retrieved == written


def test_a_different_embedder_does_not_share_chunk_ids() -> None:
    """Vectors from two models are not comparable, so the points are not either.

    Sharing ids would let a half-migrated index answer with points whose
    vectors were never in the same space as the query.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[str, str]:
        one = _service(index, embedder=DeterministicEmbedder(dimension=SIZE))
        other = _service(index, embedder=_RenamedEmbedder(dimension=SIZE))
        return one.chunk_id("ver_1", 0), other.chunk_id("ver_1", 0)

    first, second = _run(scenario)

    assert first != second


def test_a_different_chunker_does_not_share_chunk_ids() -> None:
    """Different boundaries mean a chunk id would name different text."""

    async def scenario(index: QdrantVectorIndex) -> tuple[str, str]:
        one = _service(index)
        other = _service(
            index,
            chunker=Chunker(
                size_tokens=16, overlap_tokens=2, counter=ApproximateTokenCounter()
            ),
        )
        return one.chunk_id("ver_1", 0), other.chunk_id("ver_1", 0)

    first, second = _run(scenario)

    assert first != second


def test_a_new_version_gets_its_own_chunks() -> None:
    async def scenario(index: QdrantVectorIndex) -> tuple[str, str]:
        service = _service(index)
        return service.chunk_id("ver_1", 0), service.chunk_id("ver_2", 0)

    first, second = _run(scenario)

    assert first != second


def test_the_acl_reaches_the_payload() -> None:
    """Written so a query can narrow on it. It is not the authorization."""

    async def scenario(index: QdrantVectorIndex) -> tuple[int, int]:
        service = _service(index)
        await service.ingest(_request(authorized_principals=(OWNER, NEIGHBOUR)))
        query = await service.embedder.embed_query("fusion")
        granted = await index.search(
            vector=query,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(NEIGHBOUR,),
            limit=50,
        )
        stranger = await index.search(
            vector=query,
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=("user_stranger",),
            limit=50,
        )
        return len(granted), len(stranger)

    granted, stranger = _run(scenario)

    assert granted > 0
    assert stranger == 0


def test_an_empty_document_writes_nothing() -> None:
    """A blank upload is legal, and an empty passage in a context is not."""

    async def scenario(index: QdrantVectorIndex) -> tuple[int, int]:
        service = _service(index)
        written = await service.ingest(_request(content=b""))
        hits = await index.search(
            vector=await service.embedder.embed_query("anything"),
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=50,
        )
        return len(written), len(hits)

    assert _run(scenario) == (0, 0)


def test_a_failing_embedder_leaves_the_index_untouched() -> None:
    """A half-indexed version answers as though that were the whole document."""

    async def scenario(index: QdrantVectorIndex) -> int:
        service = _service(index, embedder=_FailingEmbedder(dimension=SIZE))
        with contextlib.suppress(RuntimeError):
            await service.ingest(_request())
        hits = await index.search(
            vector=(1.0,) + (0.0,) * (SIZE - 1),
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=50,
        )
        return len(hits)

    assert _run(scenario) == 0


def test_an_embedder_returning_the_wrong_count_is_refused() -> None:
    """Vectors are paired with chunks by position, so a short batch misaligns."""

    async def scenario(index: QdrantVectorIndex) -> None:
        service = _service(index, embedder=_ShortEmbedder(dimension=SIZE))
        await service.ingest(_request())

    with pytest.raises(ValueError, match="vectors"):
        _run(scenario)


class _RenamedEmbedder(DeterministicEmbedder):
    """Same vectors, different identity -- so only the identity is under test."""

    @property
    def identity(self) -> str:
        return "pretend-bge-m3-v1"


class _FailingEmbedder(DeterministicEmbedder):
    async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        raise RuntimeError("the embedding backend is down")


class _ShortEmbedder(DeterministicEmbedder):
    async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        full = await super().embed_documents(texts)
        return full[:-1]


# --- sparse in the pipeline --------------------------------------------------


class _FixedSparse:
    """A sparse encoder with one term, so its presence is unmistakable."""

    def __init__(self, term: int = 99, identity: str = "sparse-v1") -> None:
        self._term = term
        self._identity = identity

    @property
    def vocabulary_size(self) -> int:
        return 250002

    @property
    def identity(self) -> str:
        return self._identity

    async def encode_documents(self, texts: tuple[str, ...]) -> tuple[Any, ...]:
        from agent_workbench.ports.sparse import SparseVector

        return tuple(SparseVector(indices=(self._term,), values=(1.0,)) for _ in texts)

    async def encode_query(self, text: str) -> Any:
        from agent_workbench.ports.sparse import SparseVector

        return SparseVector(indices=(self._term,), values=(1.0,))


def test_a_sparse_encoder_changes_the_index_identity() -> None:
    """A half-sparse collection ranks some points by one arm and some by two.

    Different identity means different chunk ids, so the two never share a
    point and a re-index is what it looks like rather than a silent overlay.
    """

    async def scenario(index: QdrantVectorIndex) -> tuple[str, str]:
        dense_only = _service(index)
        hybrid = _service(index, sparse_encoder=_FixedSparse())
        return dense_only.index_identity, hybrid.index_identity

    dense_only, hybrid = _run(scenario)

    assert dense_only != hybrid
    assert dense_only in hybrid


def test_two_sparse_encoders_do_not_share_chunk_ids() -> None:
    async def scenario(index: QdrantVectorIndex) -> tuple[str, str]:
        one = _service(index, sparse_encoder=_FixedSparse(identity="sparse-v1"))
        other = _service(index, sparse_encoder=_FixedSparse(identity="sparse-v2"))
        return one.chunk_id("ver_1", 0), other.chunk_id("ver_1", 0)

    first, second = _run(scenario)

    assert first != second


def test_ingested_chunks_carry_term_weights() -> None:
    """Written by ingestion, so a hybrid query has something to match."""

    async def scenario(index: QdrantVectorIndex) -> tuple[int, int]:
        service = _service(index, sparse_encoder=_FixedSparse())
        written = await service.ingest(_request())
        return len(written), len(written[0].sparse_indices)

    count, terms = _run(scenario)

    assert count > 0
    assert terms == 1


def test_without_an_encoder_chunks_carry_no_terms() -> None:
    """The control: absence is absence, not an empty vector that matches all."""

    async def scenario(index: QdrantVectorIndex) -> tuple[int, ...]:
        written = await _service(index).ingest(_request())
        return written[0].sparse_indices

    assert _run(scenario) == ()


def test_a_sparse_encoder_returning_the_wrong_count_is_refused() -> None:
    """Weights are paired with chunks by position, as vectors are."""

    class _Short(_FixedSparse):
        async def encode_documents(self, texts: tuple[str, ...]) -> tuple[Any, ...]:
            return (await super().encode_documents(texts))[:-1]

    async def scenario(index: QdrantVectorIndex) -> None:
        await _service(index, sparse_encoder=_Short()).ingest(_request())

    with pytest.raises(ValueError, match="sparse encoder"):
        _run(scenario)
