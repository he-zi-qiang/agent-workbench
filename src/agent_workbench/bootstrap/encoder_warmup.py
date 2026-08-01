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


async def warm_encoders(*encoders: object) -> None:
    """Run one throwaway encode through each encoder that has a query path.

    ``None`` entries are skipped so a caller can pass optional components
    without a chain of conditionals at every call site.
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
            else:
                continue
        except Exception:
            # Never fatal. See the module docstring: this buys latency, and a
            # process that refused to start over it would be trading a slow
            # first request for no requests at all.
            logger.warning("could not warm %s; the first request will pay it", name)
            continue
        logger.info("warmed %s in %.1fs", name, time.monotonic() - started)


__all__ = ["WARMUP_TEXT", "DenseEncoder", "QueryEncoder", "warm_encoders"]
