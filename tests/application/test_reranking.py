"""What reranking is allowed to change, and what it must never change.

Reranking is an optional quality step wrapped around an authorization
boundary, so the tests that matter are about the boundary rather than about
ranking quality. Whether BGE-reranker-v2-m3 improves nDCG is an evaluation
question with real weights and a gold set (WP05-08); it is not something a
stand-in can answer, and a test that pretended otherwise would be measuring
the stand-in.

What a stand-in can answer, and what a real model cannot be made to answer on
demand, is what happens when the reranker hangs, raises, or returns a result
that does not line up with its input. Those are the paths this file exercises.

Every test here pairs a positive with a negative: an assertion that reranking
changed the order is worthless without one showing the same setup leaves the
order alone when reranking is absent, because otherwise it is satisfied by a
service that ignores the reranker entirely.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from agent_workbench.adapters.reranking import (
    FailingReranker,
    LexicalOverlapReranker,
    MiscountingReranker,
    SlowReranker,
)
from agent_workbench.application.retrieval import (
    RetrievalRequest,
    RetrievalService,
)
from agent_workbench.ports.documents import ReadableDocument
from agent_workbench.ports.embedding import Vector
from agent_workbench.ports.vector_index import ScoredChunk

TENANT = "tenant_a"
KB = "kb_main"
PRINCIPAL = "user_reader"
QUERY = "fencing token lease"

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def chunk(
    chunk_id: str,
    *,
    document_id: str,
    text: str,
    score: float,
    revision: int = 1,
) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        document_version=f"{document_id}_v1",
        tenant_id=TENANT,
        knowledge_base_id=KB,
        source_revision=revision,
        text=text,
        ordinal=0,
        score=score,
    )


# The retriever's order is deliberately the opposite of the query's lexical
# overlap, so "the reranker ran" and "the reranker did not run" produce
# different orders. If they agreed, every assertion below would pass either way.
CANDIDATES = (
    chunk("c1", document_id="doc_1", text="unrelated prose about weather", score=0.9),
    chunk("c2", document_id="doc_2", text="a lease and nothing else", score=0.8),
    chunk("c3", document_id="doc_3", text="fencing token lease renewal", score=0.7),
)


@dataclass(frozen=True, slots=True)
class StubIndex:
    """Returns a fixed candidate list, whatever is asked."""

    candidates: tuple[ScoredChunk, ...] = CANDIDATES

    async def search(self, **_: object) -> tuple[ScoredChunk, ...]:
        return self.candidates

    async def search_hybrid(self, **_: object) -> tuple[ScoredChunk, ...]:
        return self.candidates


@dataclass(frozen=True, slots=True)
class StubEmbedder:
    dimension: int = 4
    identity: str = "stub@v1"

    async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        return tuple((0.0, 0.0, 0.0, 1.0) for _ in texts)

    async def embed_query(self, text: str) -> Vector:
        return (0.0, 0.0, 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class StubDocuments:
    """Authorizes a chosen subset, at the revisions the candidates carry."""

    readable: frozenset[str] = frozenset({"doc_1", "doc_2", "doc_3"})
    revisions: dict[str, int] = field(default_factory=dict[str, int])

    async def readable_versions(
        self, *, tenant_id: str, principal_id: str, document_ids: tuple[str, ...]
    ) -> tuple[ReadableDocument, ...]:
        return tuple(
            ReadableDocument(
                document_id=document_id,
                knowledge_base_id=KB,
                source_revision=self.revisions.get(document_id, 1),
            )
            for document_id in document_ids
            if document_id in self.readable
        )


def service(**overrides: object) -> RetrievalService:
    defaults: dict[str, object] = {
        "embedder": StubEmbedder(),
        "index": StubIndex(),
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


def ids(context: object) -> tuple[str, ...]:
    packet = context.packet
    return tuple(item.chunk_id for item in packet.chunks)


async def test_without_a_reranker_the_retrievers_order_survives() -> None:
    """The control. Everything below is a claim of difference from this."""

    context = await service().retrieve(request())

    assert ids(context) == ("c1", "c2", "c3")
    assert context.reranked is False


async def test_a_reranker_reorders_the_authorized_candidates() -> None:
    context = await service(reranker=LexicalOverlapReranker()).retrieve(request())

    # c3 shares three query terms, c2 shares one, c1 none -- the exact reverse
    # of what the index returned.
    assert ids(context) == ("c3", "c2", "c1")
    assert context.reranked is True


async def test_the_reranker_never_sees_an_unauthorized_passage() -> None:
    """The property WP05-04 exists for, checked at the reranker's own input.

    Checking the output would not be enough. Both the success path and the
    fail-open path return a subset of the authorized list, so an implementation
    that scored every candidate and then filtered would produce an identical
    result while having fed a revoked passage through a cross-encoder -- which
    is a model reading text the asker may not read.
    """

    reranker = LexicalOverlapReranker()
    documents = StubDocuments(readable=frozenset({"doc_1", "doc_3"}))

    context = await service(reranker=reranker, documents=documents).retrieve(request())

    assert len(reranker.calls) == 1
    _, passages = reranker.calls[0]
    assert "a lease and nothing else" not in passages
    assert set(passages) == {
        "unrelated prose about weather",
        "fencing token lease renewal",
    }
    assert ids(context) == ("c3", "c1")


async def test_a_stale_revision_is_dropped_before_the_reranker_too() -> None:
    """Authorization is revision equality, not readability, on this path as well."""

    reranker = LexicalOverlapReranker()
    # doc_3 has moved on in PostgreSQL; the indexed point is from revision 1.
    documents = StubDocuments(revisions={"doc_3": 2})

    context = await service(reranker=reranker, documents=documents).retrieve(request())

    _, passages = reranker.calls[0]
    assert "fencing token lease renewal" not in passages
    assert ids(context) == ("c2", "c1")


async def test_a_timeout_falls_back_to_the_retrievers_order() -> None:
    context = await service(
        reranker=SlowReranker(),
        rerank_timeout_seconds=0.01,
    ).retrieve(request())

    assert ids(context) == ("c1", "c2", "c3")
    assert context.reranked is False


async def test_a_raising_reranker_falls_back_to_the_retrievers_order() -> None:
    context = await service(reranker=FailingReranker()).retrieve(request())

    assert ids(context) == ("c1", "c2", "c3")
    assert context.reranked is False


async def test_a_misaligned_score_list_is_refused_rather_than_applied() -> None:
    """Wrong-length scores are a defect, and applying them would look like taste.

    Pairing score[i] with passage[i] when the lists differ in length produces a
    plausible ordering built on nothing. It is rejected instead.
    """

    context = await service(reranker=MiscountingReranker()).retrieve(request())

    assert ids(context) == ("c1", "c2", "c3")
    assert context.reranked is False


async def test_fail_open_never_widens_what_was_authorized() -> None:
    """The fallback is the authorized list, not the list the index returned."""

    documents = StubDocuments(readable=frozenset({"doc_1"}))

    context = await service(reranker=FailingReranker(), documents=documents).retrieve(
        request()
    )

    assert ids(context) == ("c1",)
    assert context.reranked is False


async def test_reranking_happens_before_top_k_not_after() -> None:
    """Otherwise the reranker can only promote inside the retriever's choice.

    With top_k=1 and truncation first, the reranker would be handed one
    candidate and could never surface c3. The assertion is that it does.
    """

    context = await service(reranker=LexicalOverlapReranker()).retrieve(
        request(top_k=1)
    )

    assert ids(context) == ("c3",)


async def test_cancellation_is_not_swallowed_by_fail_open() -> None:
    """A cancelled request must stay cancelled.

    Fail-open turns errors into a usable answer, and a cancellation is not an
    error to recover from -- treating it as one would keep a request alive
    after whoever asked has gone.
    """

    started = asyncio.Event()

    @dataclass(frozen=True, slots=True)
    class BlockingReranker:
        identity: str = "blocking@v1"

        async def rerank(
            self, query: str, passages: tuple[str, ...]
        ) -> tuple[float, ...]:
            started.set()
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    task = asyncio.create_task(
        service(reranker=BlockingReranker(), rerank_timeout_seconds=3600).retrieve(
            request()
        )
    )
    # Bounded. An unbounded wait here turns "the reranker was never called"
    # into a hung test instead of a failing one, which is worse than useless:
    # it makes a real regression look like an infrastructure problem. The bound
    # is generous rather than tuned, so it cannot become a flaky race.
    async with asyncio.timeout(10):
        await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_an_empty_candidate_set_does_not_call_the_reranker() -> None:
    """No passages is not a ranking problem, and a model call would be waste."""

    reranker = LexicalOverlapReranker()
    documents = StubDocuments(readable=frozenset())

    context = await service(reranker=reranker, documents=documents).retrieve(request())

    assert reranker.calls == []
    assert ids(context) == ()
    assert context.reranked is False


async def test_mode_names_the_reranker_when_one_is_configured() -> None:
    """An ablation report that cannot name the arm cannot compare arms."""

    assert service().mode == "dense"
    assert service(reranker=LexicalOverlapReranker()).mode == "dense+rerank"
