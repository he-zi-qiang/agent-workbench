"""Paying the first encode before a request does.

The cost this exists for is real and was measured on this project's own model:
BGE-M3's lexical head takes 29.4 seconds on its first ``encode`` and 0.06 on
every one after. That is not a slow encoder, it is a cold accelerator compiling
kernels -- and left where it fell, the bill went to whichever request arrived
first. It arrived as an agentic chat turn dying on ``knowledge_search exceeded
its 30s timeout``.

These tests are about the protocol rather than the timing: that every encoder
handed over is actually touched, that a failure to warm is not a failure to
start, and that the shape of an encoder decides which method is called. The
timing itself belongs to the machine and is recorded in the module docstring
and the checklist, not asserted here -- a test that measured it would fail on
whichever laptop happened to be faster.
"""

from __future__ import annotations

import asyncio

from agent_workbench.adapters.reranking.fake import (
    FailingReranker,
    LexicalOverlapReranker,
)
from agent_workbench.bootstrap.encoder_warmup import WARMUP_TEXT, warm_encoders


class _Dense:
    """Shaped like the dense embedder: it answers `embed_query`."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed_query(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        return (0.0,)


class _Sparse:
    """Shaped like the lexical encoder: it answers `encode_query`."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def encode_query(self, text: str) -> object:
        self.calls.append(text)
        return object()


class _Exploding:
    async def encode_query(self, text: str) -> object:
        raise RuntimeError("no accelerator here")


class _Unrelated:
    """Something with none of the warmable query paths -- a store, say.

    It used to be described here as "a reranker, say", and that description was
    the defect in one line: a reranker really did fall through to the skip, and
    a test said so approvingly.
    """


def test_every_encoder_handed_over_is_actually_touched() -> None:
    dense, sparse = _Dense(), _Sparse()

    asyncio.run(warm_encoders(dense, sparse))

    assert dense.calls == [WARMUP_TEXT]
    assert sparse.calls == [WARMUP_TEXT]


def test_each_encoder_is_warmed_through_its_own_method() -> None:
    """The two ports differ, and warming the wrong one warms nothing.

    A dense embedder has no ``encode_query`` and a lexical encoder has no
    ``embed_query``; calling the absent one would raise, be swallowed as a
    warning, and leave the process exactly as cold as before.
    """

    dense, sparse = _Dense(), _Sparse()

    asyncio.run(warm_encoders(dense, sparse))

    assert len(dense.calls) == 1
    assert len(sparse.calls) == 1


def test_the_reranker_is_warmed_too() -> None:
    """The default-on runtime that used to pay its first forward at request time.

    Warming matched two shapes, ``embed_query`` and ``encode_query``, and a
    cross-encoder answers neither -- it scores a query against passages in one
    pass, so its query path is ``rerank``. It therefore fell through the same
    ``else: continue`` that skips a document store, silently, while being on by
    default. The first ``/v1/search`` with reranking then paid a cold forward
    that the two encoders beside it had already been spared.
    """

    reranker = LexicalOverlapReranker()

    asyncio.run(warm_encoders(reranker))

    assert len(reranker.calls) == 1


def test_the_reranker_is_warmed_with_a_passage_to_actually_score() -> None:
    """An empty passage tuple would warm nothing while looking like it did.

    ``BgeReranker.rerank`` returns ``()`` before touching the model when there
    are no passages -- a sensible shortcut for a caller whose retrieval came
    back empty, and a silent no-op for a warm-up that took it. The whole point
    is to run one real forward pass, so the pair has to be a pair.
    """

    reranker = LexicalOverlapReranker()

    asyncio.run(warm_encoders(reranker))

    ((query, passages),) = reranker.calls
    assert query == WARMUP_TEXT
    assert len(passages) == 1
    assert passages[0]


def test_a_failed_warm_up_does_not_stop_the_process() -> None:
    """It buys latency. A process that refused to start over it would trade a
    slow first request for no requests at all."""

    sparse = _Sparse()

    asyncio.run(warm_encoders(_Exploding(), sparse))

    # And the one after the failure is still warmed: a single bad encoder must
    # not cost the rest their warm-up.
    assert sparse.calls == [WARMUP_TEXT]


def test_a_reranker_that_cannot_be_warmed_is_also_survivable() -> None:
    """Same rule, and it has to hold for the newest shape too.

    A reranker is the heaviest thing warmed here and so the likeliest to run
    out of memory doing it. Refusing to start over that would take down chat,
    search and uploads to protect one request from being slow.
    """

    sparse = _Sparse()

    asyncio.run(warm_encoders(FailingReranker(), sparse))

    assert sparse.calls == [WARMUP_TEXT]


def test_absent_and_unrelated_components_are_skipped() -> None:
    """Callers pass optional pieces without a chain of conditionals."""

    sparse = _Sparse()

    asyncio.run(warm_encoders(None, _Unrelated(), sparse))

    assert sparse.calls == [WARMUP_TEXT]


def test_warming_nothing_is_not_an_error() -> None:
    """A process assembled without chat holds no encoders at all."""

    asyncio.run(warm_encoders())
