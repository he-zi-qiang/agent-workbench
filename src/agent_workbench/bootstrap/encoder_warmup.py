"""Pay the first encode before anyone is waiting on it.

A loaded model is not a ready model. The first forward pass on an accelerator
compiles kernels for the shapes it sees, and on Apple's MPS backend that cost is
not small: measured on BGE-M3's lexical head, the first ``encode`` takes **29.4
seconds** and every later one takes **0.06** -- a 500x difference that belongs
entirely to the first caller.

Left alone, that caller is a request. It showed up as an agentic chat turn
failing with ``knowledge_search exceeded its 30s timeout``: the model asked to
search, the tool's own timeout is 30 seconds, and the very first search in the
process needed 29.4 of them plus retrieval. The second would have taken a
twentieth of a second. Nothing was slow -- something was cold, and a tool
timeout is exactly the wrong place to discover that.

So a process that holds encoders warms them while it is still starting, where a
delay is a slower boot rather than a failed request. It is deliberately not a
correctness mechanism: a warm-up that fails is logged and the process continues,
because a machine that cannot spare the seconds still answers correctly, just
slowly, and refusing to start over a performance optimisation would trade a
small cost for a total outage.

CPU shows the same shape far more mildly (2.8s then 0.12s), so this is worth
doing regardless of the device rather than only when MPS is detected -- a rule
that inspected the backend would be one more thing to keep true.

The reranker is warmed by the same call, and it is the piece a rule written
about "encoders" most easily loses. It is on by default, it loads a
multi-gigabyte cross-encoder of its own, and its first forward pass is cold for
exactly the reason the other two are. What made it slip through is that it does
not share their shape: dense and sparse each turn one string into one vector,
while a cross-encoder reads a query and a passage *together* and returns a
score, so it answers to neither ``embed_query`` nor ``encode_query``. Matching
on those two names skipped it in silence -- the process logged two encoders
warmed and then handed the first reranked search a cold forward on top of a
retrieval path that had already been paid for. Same cliff, one component
further along, and just as able to spend a tool's whole timeout.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: Short, and never empty. The point is to make the backend compile its kernels,
#: which needs a real forward pass; the text itself is irrelevant and stays tiny
#: so the warm-up costs one sequence rather than a batch.
WARMUP_TEXT = "warm up"


@runtime_checkable
class QueryEncoder(Protocol):
    """The one method both encoders share, and the only one warming needs."""

    async def encode_query(self, text: str) -> object: ...


@runtime_checkable
class DenseEncoder(Protocol):
    async def embed_query(self, text: str) -> object: ...


@runtime_checkable
class RerankEncoder(Protocol):
    """A cross-encoder's query path, which is neither of the two above.

    Kept separate rather than folded into ``QueryEncoder`` because the
    difference is not the method's name, it is the argument: scoring needs
    something to score against, so warming this shape means constructing a
    pair, not passing a string.
    """

    async def rerank(
        self, query: str, passages: tuple[str, ...]
    ) -> tuple[float, ...]: ...


async def warm_encoders(*encoders: object) -> None:
    """Run one throwaway forward pass through each component with a query path.

    ``None`` entries are skipped so a caller can pass optional components
    without a chain of conditionals at every call site -- and the reranker is
    exactly that kind of caller-side optional, absent whenever its weights did
    not load, or whenever there is no embedder and therefore nothing for it to
    reorder. Not absent because a deployment turned it off: it has no off
    switch, and a comment that implies one sends the next reader looking for a
    setting that the config types refuse to accept.
    """

    for encoder in encoders:
        if encoder is None:
            continue
        name = type(encoder).__name__
        started = time.monotonic()
        try:
            if isinstance(encoder, DenseEncoder):
                await encoder.embed_query(WARMUP_TEXT)
            elif isinstance(encoder, QueryEncoder):
                await encoder.encode_query(WARMUP_TEXT)
            elif isinstance(encoder, RerankEncoder):
                # One passage rather than none. An adapter is entitled to
                # return an empty result without consulting the model when
                # there is nothing to score -- BgeReranker returns `()` on the
                # first line for exactly that case -- so an empty tuple here
                # would run no forward pass at all and then log a warm-up that
                # never happened, which is worse than not warming: it hides it.
                await encoder.rerank(WARMUP_TEXT, (WARMUP_TEXT,))
            else:
                continue
        except Exception:
            # Never fatal. See the module docstring: this buys latency, and a
            # process that refused to start over it would be trading a slow
            # first request for no requests at all.
            logger.warning("could not warm %s; the first request will pay it", name)
            continue
        logger.info("warmed %s in %.1fs", name, time.monotonic() - started)


__all__ = [
    "WARMUP_TEXT",
    "DenseEncoder",
    "QueryEncoder",
    "RerankEncoder",
    "warm_encoders",
]
