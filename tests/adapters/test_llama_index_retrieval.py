"""The properties only the LlamaIndex path can break.

The contract suite in tests/vector/test_authorized_retrieval.py already runs
every retrieval scenario against both implementations, which is what proves the
external behaviour did not move. This file covers what that suite cannot see,
because it observes the funnel from the outside: what the adapter *asks* the
index for, and what it does with the answer.

Three things are worth pinning here, and each has a way of failing silently:

* a filter that gets dropped instead of refused returns *more* rows, and the
  ACL check downstream would still let the caller's own documents through, so
  every existing assertion would stay green while the query had crossed a
  tenant boundary;
* a node that loses a field on the round trip produces a chunk with an invented
  value -- a page number the source does not have, a source revision that no
  longer matches PostgreSQL -- and both fail closed in a way that looks like
  "found nothing";
* a second fusion is a re-sort, and a re-sorted list is still a list of
  authorized chunks. Nothing downstream can tell that the ordering stopped
  being the one Qdrant produced.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from llama_index.core.schema import NodeWithScore, TextNode
from llama_index.core.vector_stores.types import (
    FilterCondition,
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
    VectorStoreQuery,
    VectorStoreQueryMode,
)

from agent_workbench.adapters.llama_index import (
    LlamaIndexCandidateRetriever,
    NodeMappingError,
    PortBackedEmbedding,
    PortBackedVectorStore,
    UnsupportedFilterError,
    build_filters,
    from_node,
    to_node,
)
from agent_workbench.ports.sparse import SparseVector
from agent_workbench.ports.vector_index import ScoredChunk

TENANT = "tenant_a"
KB = "kb_main"
PRINCIPAL = "user_reader"
SIZE = 4


class _Embedder:
    """A dense embedder that records what it was asked to embed."""

    dimension = SIZE
    identity = "stub@v1"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Any, ...]:
        return tuple((1.0,) * SIZE for _ in texts)

    async def embed_query(self, text: str) -> Any:
        self.queries.append(text)
        return (1.0,) * SIZE


class _Sparse:
    """A lexical encoder that records what it was asked to encode."""

    vocabulary_size = 250002
    identity = "stub-lexical@v1"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def encode_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[SparseVector, ...]:
        return tuple(SparseVector(indices=(7,), values=(1.0,)) for _ in texts)

    async def encode_query(self, text: str) -> SparseVector:
        self.queries.append(text)
        return SparseVector(indices=(7,), values=(1.0,))


def _chunk(
    chunk_id: str, *, score: float, page: int | None = None, revision: int = 1
) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=chunk_id,
        document_id=f"doc_{chunk_id}",
        document_version=f"ver_{chunk_id}",
        tenant_id=TENANT,
        knowledge_base_id=KB,
        source_revision=revision,
        text=f"passage {chunk_id}",
        ordinal=0,
        page=page,
        score=score,
    )


class _RecordingIndex:
    """An index that answers, and remembers exactly how it was asked."""

    def __init__(self, chunks: tuple[ScoredChunk, ...]) -> None:
        self._chunks = chunks
        self.search_calls: list[dict[str, Any]] = []
        self.hybrid_calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> tuple[ScoredChunk, ...]:
        self.search_calls.append(kwargs)
        return self._chunks

    async def search_hybrid(self, **kwargs: Any) -> tuple[ScoredChunk, ...]:
        self.hybrid_calls.append(kwargs)
        return self._chunks

    @property
    def calls(self) -> int:
        return len(self.search_calls) + len(self.hybrid_calls)


def _retrieve(
    index: _RecordingIndex,
    *,
    sparse: _Sparse | None = None,
    embedder: _Embedder | None = None,
    limit: int = 12,
) -> tuple[ScoredChunk, ...]:
    retriever = LlamaIndexCandidateRetriever(
        embedder=embedder if embedder is not None else _Embedder(),
        index=index,  # pyright: ignore[reportArgumentType]
        sparse_encoder=sparse,  # pyright: ignore[reportArgumentType]
    )
    return asyncio.run(
        retriever.candidates(
            query="who fuses the results",
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            knowledge_base_id=KB,
            limit=limit,
        )
    )


# --- the narrowing reaches the index ----------------------------------------


def test_the_asking_principal_reaches_the_index() -> None:
    """The filters are LlamaIndex's; the narrowing has to survive translation."""

    index = _RecordingIndex((_chunk("a", score=0.9),))

    _retrieve(index)

    assert index.search_calls[0]["tenant_id"] == TENANT
    assert index.search_calls[0]["knowledge_base_id"] == KB
    assert index.search_calls[0]["authorized_principals"] == (PRINCIPAL,)


def test_the_candidate_budget_is_the_limit_it_was_given() -> None:
    """A retriever that quietly asked for top_k would starve the rerank step."""

    index = _RecordingIndex((_chunk("a", score=0.9),))

    _retrieve(index, limit=17)

    assert index.search_calls[0]["limit"] == 17


def test_a_hybrid_query_is_one_call_that_fuses_inside_qdrant() -> None:
    """Two ranked lists in this process is the shape a second fusion needs."""

    index = _RecordingIndex((_chunk("a", score=0.9), _chunk("b", score=0.4)))
    sparse = _Sparse()

    _retrieve(index, sparse=sparse)

    assert index.calls == 1
    assert index.search_calls == []
    call = index.hybrid_calls[0]
    # Neither arm is shortened before fusion. Halving them here would make RRF
    # choose between two already-truncated lists, which is a different
    # retriever from the one being evaluated.
    assert call["dense_limit"] == call["sparse_limit"] == call["limit"]
    assert sparse.queries == ["who fuses the results"]


def test_the_dense_arm_alone_never_reaches_the_hybrid_call() -> None:
    """The control: without a sparse encoder the two paths must not converge."""

    index = _RecordingIndex((_chunk("a", score=0.9),))

    _retrieve(index)

    assert index.hybrid_calls == []
    assert len(index.search_calls) == 1


def test_the_index_order_is_returned_unchanged() -> None:
    """A re-sort is what a second fusion looks like from the outside.

    Descending scores would hide it, so the index deliberately answers with an
    ascending one: any adapter that re-ranked by score would reverse this.
    """

    index = _RecordingIndex(
        (
            _chunk("first", score=0.1),
            _chunk("second", score=0.5),
            _chunk("third", score=0.9),
        )
    )

    got = _retrieve(index)

    assert tuple(chunk.chunk_id for chunk in got) == ("first", "second", "third")
    assert tuple(chunk.score for chunk in got) == (0.1, 0.5, 0.9)


# --- an unrecognised filter is a refusal ------------------------------------


def _query(filters: MetadataFilters | None, **kwargs: Any) -> VectorStoreQuery:
    return VectorStoreQuery(
        query_embedding=[1.0] * SIZE, similarity_top_k=4, filters=filters, **kwargs
    )


def _ask(store: PortBackedVectorStore, query: VectorStoreQuery) -> Any:
    return asyncio.run(store.aquery(query))


@pytest.mark.parametrize(
    ("name", "filters"),
    (
        ("none at all", None),
        (
            # Every other check passes on this one: three distinct required
            # keys, each present once, each matched with ==. Only the condition
            # is wrong, so only the condition check can reject it.
            #
            # The first version of this case used two tenant_id filters ORed
            # together, and a sabotage round proved it worthless: with the
            # condition check deleted the query was still refused, by the
            # duplicate-key guard. The test passed for a reason that had
            # nothing to do with its name -- and an OR of these three terms is
            # the genuinely dangerous shape, since it matches every document
            # belonging to the knowledge base *or* readable by the principal,
            # in any tenant.
            "an OR of the three required terms",
            MetadataFilters(
                filters=[
                    MetadataFilter(
                        key="tenant_id", value=TENANT, operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="knowledge_base_id", value=KB, operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="authorized_principal",
                        value=PRINCIPAL,
                        operator=FilterOperator.EQ,
                    ),
                ],
                condition=FilterCondition.OR,
            ),
        ),
        (
            # Kept as its own case now that the OR one no longer covers it.
            "the same key filtered twice",
            MetadataFilters(
                filters=[
                    MetadataFilter(
                        key="tenant_id", value=TENANT, operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="tenant_id", value="tenant_b", operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="knowledge_base_id", value=KB, operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="authorized_principal",
                        value=PRINCIPAL,
                        operator=FilterOperator.EQ,
                    ),
                ],
                condition=FilterCondition.AND,
            ),
        ),
        (
            "a key this store cannot express",
            MetadataFilters(
                filters=[
                    MetadataFilter(
                        key="tenant_id", value=TENANT, operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="knowledge_base_id", value=KB, operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="authorized_principal",
                        value=PRINCIPAL,
                        operator=FilterOperator.EQ,
                    ),
                    MetadataFilter(
                        key="classification",
                        value="secret",
                        operator=FilterOperator.EQ,
                    ),
                ],
                condition=FilterCondition.AND,
            ),
        ),
        (
            "an operator that is not equality",
            MetadataFilters(
                filters=[
                    MetadataFilter(
                        key="tenant_id", value=TENANT, operator=FilterOperator.NE
                    ),
                    MetadataFilter(
                        key="knowledge_base_id", value=KB, operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="authorized_principal",
                        value=PRINCIPAL,
                        operator=FilterOperator.EQ,
                    ),
                ],
                condition=FilterCondition.AND,
            ),
        ),
        (
            "a missing tenant",
            MetadataFilters(
                filters=[
                    MetadataFilter(
                        key="knowledge_base_id", value=KB, operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="authorized_principal",
                        value=PRINCIPAL,
                        operator=FilterOperator.EQ,
                    ),
                ],
                condition=FilterCondition.AND,
            ),
        ),
        (
            "a nested group",
            MetadataFilters(
                filters=[
                    MetadataFilter(
                        key="tenant_id", value=TENANT, operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="knowledge_base_id", value=KB, operator=FilterOperator.EQ
                    ),
                    MetadataFilter(
                        key="authorized_principal",
                        value=PRINCIPAL,
                        operator=FilterOperator.EQ,
                    ),
                    MetadataFilters(
                        filters=[
                            MetadataFilter(
                                key="tenant_id",
                                value="tenant_b",
                                operator=FilterOperator.EQ,
                            )
                        ],
                        condition=FilterCondition.OR,
                    ),
                ],
                condition=FilterCondition.AND,
            ),
        ),
    ),
)
def test_a_filter_this_store_cannot_express_is_refused(
    name: str, filters: MetadataFilters | None
) -> None:
    """Every one of these would otherwise have run a *wider* query.

    That is the failure this fails closed against: dropping a narrowing does
    not raise, it returns more rows, and the ACL check afterwards would still
    pass every row the caller happens to own.
    """

    index = _RecordingIndex((_chunk("a", score=0.9),))
    store = PortBackedVectorStore(index)  # pyright: ignore[reportArgumentType]

    with pytest.raises(UnsupportedFilterError):
        _ask(store, _query(filters))

    # And it refused before asking, rather than after.
    assert index.calls == 0


def test_the_filter_shape_the_retriever_builds_is_accepted() -> None:
    """The control for the six refusals above.

    Without it, a translation that rejected everything would pass all of them
    and retrieve nothing in production.
    """

    index = _RecordingIndex((_chunk("a", score=0.9),))
    store = PortBackedVectorStore(index)  # pyright: ignore[reportArgumentType]

    result = _ask(
        store,
        _query(
            build_filters(
                tenant_id=TENANT, knowledge_base_id=KB, principal_id=PRINCIPAL
            )
        ),
    )

    assert index.calls == 1
    assert [node.node_id for node in result.nodes] == ["a"]


def test_a_hybrid_query_without_a_lexical_runtime_is_refused() -> None:
    """Answering it densely would put a dense run under a hybrid label."""

    index = _RecordingIndex((_chunk("a", score=0.9),))
    store = PortBackedVectorStore(index)  # pyright: ignore[reportArgumentType]

    with pytest.raises(UnsupportedFilterError):
        _ask(
            store,
            _query(
                build_filters(
                    tenant_id=TENANT, knowledge_base_id=KB, principal_id=PRINCIPAL
                ),
                mode=VectorStoreQueryMode.HYBRID,
                query_str="who fuses",
            ),
        )

    assert index.calls == 0


# --- the round trip keeps what downstream reads -----------------------------


def test_a_chunk_survives_the_round_trip_intact() -> None:
    """Every field here is load bearing somewhere after retrieval."""

    original = _chunk("a", score=0.75, page=4, revision=9)

    restored = from_node(NodeWithScore(node=to_node(original), score=0.75))

    assert restored == original


def test_a_chunk_without_a_page_does_not_acquire_one() -> None:
    """A format with no pages has no page 1; inventing one misdirects a reader."""

    original = _chunk("a", score=0.5, page=None)

    restored = from_node(NodeWithScore(node=to_node(original), score=0.5))

    assert restored.page is None


def test_the_source_revision_survives_as_an_integer() -> None:
    """It is compared for equality against the row PostgreSQL holds.

    A revision that came back as "9" rather than 9 would never match, every
    candidate would be dropped as stale, and retrieval would look like it
    merely found nothing.
    """

    restored = from_node(
        NodeWithScore(node=to_node(_chunk("a", score=0.5, revision=9)), score=0.5)
    )

    assert restored.source_revision == 9
    assert isinstance(restored.source_revision, int)


@pytest.mark.parametrize(
    "missing",
    ("document_id", "document_version", "tenant_id", "source_revision", "ordinal"),
)
def test_a_node_missing_a_field_produces_no_chunk_at_all(missing: str) -> None:
    """Defaulting any of these invents a fact retrieval would then act on."""

    node = to_node(_chunk("a", score=0.5))
    metadata = dict(node.metadata)
    del metadata[missing]
    node.metadata = metadata

    with pytest.raises(NodeMappingError):
        from_node(NodeWithScore(node=node, score=0.5))


def test_a_node_that_says_nothing_about_a_page_is_refused() -> None:
    """Absent-and-recorded and absent-and-unknown are different states."""

    node = to_node(_chunk("a", score=0.5))
    metadata = dict(node.metadata)
    del metadata["page"]
    node.metadata = metadata

    with pytest.raises(NodeMappingError):
        from_node(NodeWithScore(node=node, score=0.5))


def test_an_unscored_node_is_refused() -> None:
    """Rank is meaning here; a scoreless node is an unknown position, not zero."""

    with pytest.raises(NodeMappingError):
        from_node(NodeWithScore(node=to_node(_chunk("a", score=0.5)), score=None))


def test_a_foreign_node_produces_no_chunk() -> None:
    """Nothing else wrote into this collection, and the mapping says so."""

    with pytest.raises(NodeMappingError):
        from_node(NodeWithScore(node=TextNode(id_="x", text="hello"), score=0.5))


# --- the roles this adapter refuses to take on ------------------------------


def test_the_store_refuses_to_be_written_through() -> None:
    """A second write path into one collection is what ADR-017 forbids."""

    store = PortBackedVectorStore(_RecordingIndex(()))  # pyright: ignore[reportArgumentType]

    with pytest.raises(NotImplementedError):
        store.add([TextNode(id_="x", text="hello")])
    with pytest.raises(NotImplementedError):
        store.delete("doc_a")


def test_the_store_hands_out_no_native_client() -> None:
    """A caller holding the Qdrant client could skip both checks below it."""

    assert PortBackedVectorStore(_RecordingIndex(())).client is None  # pyright: ignore[reportArgumentType]


def test_the_embedding_refuses_to_run_synchronously() -> None:
    """The port is async; a sync path would block or nest an event loop."""

    embedding = PortBackedEmbedding(_Embedder())  # pyright: ignore[reportArgumentType]

    with pytest.raises(NotImplementedError):
        embedding._get_query_embedding("who fuses")
    with pytest.raises(NotImplementedError):
        embedding._get_text_embedding("a passage")


def test_the_query_is_embedded_by_this_project_s_own_embedder() -> None:
    """Not a second copy of the model LlamaIndex would have loaded itself."""

    embedder = _Embedder()

    _retrieve(_RecordingIndex((_chunk("a", score=0.9),)), embedder=embedder)

    assert embedder.queries == ["who fuses the results"]


def test_each_call_builds_its_own_retriever() -> None:
    """The filters carry the asker, so a cached retriever leaks a narrowing."""

    index = _RecordingIndex((_chunk("a", score=0.9),))
    retriever = LlamaIndexCandidateRetriever(
        embedder=_Embedder(),  # pyright: ignore[reportArgumentType]
        index=index,  # pyright: ignore[reportArgumentType]
    )

    async def both() -> None:
        for principal in ("user_first", "user_second"):
            await retriever.candidates(
                query="who fuses",
                tenant_id=TENANT,
                principal_id=principal,
                knowledge_base_id=KB,
                limit=4,
            )

    asyncio.run(both())

    assert [call["authorized_principals"] for call in index.search_calls] == [
        ("user_first",),
        ("user_second",),
    ]
