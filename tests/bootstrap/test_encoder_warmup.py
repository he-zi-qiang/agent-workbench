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
    """Something with neither method -- a reranker, say."""


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


def test_a_failed_warm_up_does_not_stop_the_process() -> None:
    """It buys latency. A process that refused to start over it would trade a
    slow first request for no requests at all."""

    sparse = _Sparse()

    asyncio.run(warm_encoders(_Exploding(), sparse))

    # And the one after the failure is still warmed: a single bad encoder must
    # not cost the rest their warm-up.
    assert sparse.calls == [WARMUP_TEXT]


def test_absent_and_unrelated_components_are_skipped() -> None:
    """Callers pass optional pieces without a chain of conditionals."""

    sparse = _Sparse()

    asyncio.run(warm_encoders(None, _Unrelated(), sparse))

    assert sparse.calls == [WARMUP_TEXT]


def test_warming_nothing_is_not_an_error() -> None:
    """A process assembled without chat holds no encoders at all."""

    asyncio.run(warm_encoders())
