"""Whether `[rag.retrieval]` actually reaches retrieval (ADR-097).

Until ADR-097 the five numbers under `[rag.retrieval]` were validated against
each other at startup and then read by nobody: `RetrievalService` was built
without them, and what bounded a search was `request.top_k *
candidate_multiplier` -- two dataclass defaults. Lowering `rerank_top_k` in a
deployment's config changed nothing at all, while `docs/configuration.md` §8
described it as a ceiling a request could only narrow within.

That is what this file exists to keep true, and every test here pairs a
positive with a negative for the reason `test_reranking.py` gives: an assertion
that "the configured number was used" is worthless without one showing the
unconfigured service still behaves the way it always did, because otherwise a
service that ignored the request entirely would satisfy it.

What is *not* tested here is whether the shipped numbers retrieve better than
the multiplier they replace. That is an evaluation question over a gold set
with real weights (known-gaps A-03), and a stand-in index cannot answer it --
a test that pretended otherwise would be measuring the stand-in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_workbench.adapters.retrieval import ReferenceVectorIndexRetriever
from agent_workbench.application.retrieval import (
    DEFAULT_CANDIDATE_MULTIPLIER,
    RetrievalRequest,
    RetrievalService,
)
from agent_workbench.ports.documents import ReadableDocument
from agent_workbench.ports.embedding import Vector
from agent_workbench.ports.sparse import SparseVector
from agent_workbench.ports.vector_index import ScoredChunk

TENANT = "tenant_a"
KB = "kb_main"
PRINCIPAL = "user_reader"
QUERY = "fencing token lease"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def chunk(ordinal: int) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=f"c{ordinal}",
        document_id=f"doc_{ordinal}",
        document_version=f"doc_{ordinal}_v1",
        tenant_id=TENANT,
        knowledge_base_id=KB,
        source_revision=1,
        text=f"passage {ordinal} about a lease",
        ordinal=0,
        score=1.0 - ordinal / 100,
    )


#: More than any ceiling asserted below, so a cut is always observable rather
#: than an artefact of the stub running out of candidates.
CANDIDATES = tuple(chunk(i) for i in range(12))


@dataclass(slots=True)
class RecordingIndex:
    """Returns a fixed list and remembers what it was asked for.

    The recorded call is the whole point: the numbers under test never change
    *what* a stub returns, so asserting on results alone could not tell a
    configured funnel from an ignored one.
    """

    calls: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    async def search(self, **kwargs: object) -> tuple[ScoredChunk, ...]:
        self.calls.append(kwargs)
        return CANDIDATES

    async def search_hybrid(self, **kwargs: object) -> tuple[ScoredChunk, ...]:
        self.calls.append(kwargs)
        return CANDIDATES


@dataclass(frozen=True, slots=True)
class StubEmbedder:
    dimension: int = 4
    identity: str = "stub@v1"

    async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        return tuple((0.0, 0.0, 0.0, 1.0) for _ in texts)

    async def embed_query(self, text: str) -> Vector:
        return (0.0, 0.0, 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class StubSparseEncoder:
    vocabulary_size: int = 64
    identity: str = "stub-sparse@v1"

    async def encode_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[SparseVector, ...]:
        return tuple(SparseVector(indices=(7,), values=(1.0,)) for _ in texts)

    async def encode_query(self, text: str) -> SparseVector:
        return SparseVector(indices=(7,), values=(1.0,))


@dataclass(frozen=True, slots=True)
class StubDocuments:
    """Authorizes every candidate at the revision it carries."""

    async def readable_versions(
        self, *, tenant_id: str, principal_id: str, document_ids: tuple[str, ...]
    ) -> tuple[ReadableDocument, ...]:
        return tuple(
            ReadableDocument(
                document_id=document_id, knowledge_base_id=KB, source_revision=1
            )
            for document_id in document_ids
        )


def build(
    index: RecordingIndex, *, hybrid: bool = False, **overrides: object
) -> RetrievalService:
    defaults: dict[str, object] = {
        "candidate_retriever": ReferenceVectorIndexRetriever(
            embedder=StubEmbedder(),
            index=index,  # pyright: ignore[reportArgumentType]
            sparse_encoder=StubSparseEncoder() if hybrid else None,  # pyright: ignore[reportArgumentType]
            dense_top_k=overrides.pop("dense_top_k", None),  # pyright: ignore[reportArgumentType]
            sparse_top_k=overrides.pop("sparse_top_k", None),  # pyright: ignore[reportArgumentType]
        ),
        "documents": StubDocuments(),
    }
    defaults.update(overrides)
    return RetrievalService(**defaults)  # pyright: ignore[reportArgumentType]


def request(top_k: int = 3) -> RetrievalRequest:
    return RetrievalRequest(
        query=QUERY,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        knowledge_base_id=KB,
        top_k=top_k,
    )


async def test_the_configured_funnel_decides_how_many_candidates_are_asked_for() -> (
    None
):
    index = RecordingIndex()

    await build(index, fused_top_k=9).retrieve(request(top_k=3))

    # 9 because the deployment said 9 -- not 3 * DEFAULT_CANDIDATE_MULTIPLIER.
    assert index.calls[0]["limit"] == 9


async def test_without_a_configured_funnel_the_multiplier_still_decides() -> None:
    """The control. Without it, a service that ignored `fused_top_k` and always
    asked for 9 would pass the test above."""

    index = RecordingIndex()

    await build(index).retrieve(request(top_k=3))

    assert index.calls[0]["limit"] == 3 * DEFAULT_CANDIDATE_MULTIPLIER


async def test_the_configured_pool_does_not_move_with_the_request() -> None:
    """A funnel is a deployment's number, not a multiple of what was asked."""

    index = RecordingIndex()
    service = build(index, fused_top_k=9)

    await service.retrieve(request(top_k=1))
    await service.retrieve(request(top_k=8))

    assert [call["limit"] for call in index.calls] == [9, 9]


async def test_a_request_may_not_ask_for_more_than_the_configured_ceiling() -> None:
    index = RecordingIndex()

    context = await build(index, fused_top_k=12, rerank_top_k=2).retrieve(
        request(top_k=7)
    )

    # The request asked for 7 and the index offered 12 authorized candidates,
    # so 2 can only come from the ceiling. This is the line that makes
    # `docs/configuration.md` §8 true.
    assert len(context.packet.chunks) == 2


async def test_a_request_below_the_ceiling_still_gets_what_it_asked_for() -> None:
    """The control: the ceiling narrows, it does not overwrite."""

    index = RecordingIndex()

    context = await build(index, fused_top_k=12, rerank_top_k=8).retrieve(
        request(top_k=3)
    )

    assert len(context.packet.chunks) == 3


async def test_without_a_ceiling_a_request_gets_everything_it_asked_for() -> None:
    """The other control: before ADR-097 nothing bounded a well-formed request,
    and an unconfigured service must still behave that way."""

    index = RecordingIndex()

    context = await build(index, fused_top_k=12).retrieve(request(top_k=7))

    assert len(context.packet.chunks) == 7


async def test_configured_arms_reach_the_hybrid_query() -> None:
    index = RecordingIndex()

    await build(
        index, hybrid=True, dense_top_k=5, sparse_top_k=6, fused_top_k=9
    ).retrieve(request(top_k=3))

    call = index.calls[0]
    assert (call["dense_limit"], call["sparse_limit"], call["limit"]) == (5, 6, 9)


async def test_unconfigured_arms_receive_the_fused_budget() -> None:
    """The control, and it also pins the property `reference.py` argues for:
    each arm proposes a full candidate set, so fusion chooses between two whole
    lists rather than two already-truncated ones."""

    index = RecordingIndex()

    await build(index, hybrid=True, fused_top_k=9).retrieve(request(top_k=3))

    call = index.calls[0]
    assert (call["dense_limit"], call["sparse_limit"], call["limit"]) == (9, 9, 9)
