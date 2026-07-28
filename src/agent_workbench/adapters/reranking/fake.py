"""Rerankers that need no model, for exercising the paths around one.

Three of them, because the interesting behaviour is not "does a good reranker
help" -- that is an evaluation question, answered with real weights and a gold
set. It is what the caller does when a reranker reorders, when it hangs, and
when it raises. Those three need to be provoked deliberately, and a real model
provokes none of them on demand.

``LexicalOverlapReranker`` deliberately ranks by something the retriever does
not, so a test can tell a reranked order apart from the order it was given. A
stand-in that agreed with the retriever would make every assertion about "the
reranker ran" pass whether or not it did.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


def _terms(text: str) -> set[str]:
    return {term for term in text.lower().split() if term}


# What one call was asked to score. Recorded so a test can assert that the
# reranker saw exactly the authorized passages -- the fallback path returns the
# same list either way, so "did it see an unauthorized one" cannot be answered
# by looking at the result.
RerankCall = tuple[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class LexicalOverlapReranker:
    """Score by how many of the query's words a passage repeats."""

    identity: str = "fake-lexical-overlap@v1"
    calls: list[RerankCall] = field(default_factory=list[RerankCall])

    async def rerank(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        self.calls.append((query, passages))
        wanted = _terms(query)
        return tuple(float(len(wanted & _terms(passage))) for passage in passages)


@dataclass(frozen=True, slots=True)
class FailingReranker:
    """Raise instead of scoring."""

    error: Exception = field(default_factory=lambda: RuntimeError("reranker exploded"))
    identity: str = "fake-failing@v1"

    async def rerank(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        raise self.error


@dataclass(frozen=True, slots=True)
class SlowReranker:
    """Sleep past any patience the caller has.

    The delay is long rather than tuned to the timeout under test: a stand-in
    that sleeps for approximately the timeout turns a deterministic assertion
    into a race, and this repository does not accept sleep-calibrated tests.
    """

    delay_seconds: float = 3600.0
    identity: str = "fake-slow@v1"

    async def rerank(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        await asyncio.sleep(self.delay_seconds)
        raise AssertionError("the slow reranker was allowed to finish")


@dataclass(frozen=True, slots=True)
class MiscountingReranker:
    """Return the wrong number of scores.

    Exists because positional alignment is the port's whole contract, and a
    contract nothing can violate in a test is a contract nothing checks.
    """

    extra: int = 1
    identity: str = "fake-miscounting@v1"

    async def rerank(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        return tuple(float(index) for index in range(len(passages) + self.extra))


__all__ = [
    "FailingReranker",
    "LexicalOverlapReranker",
    "MiscountingReranker",
    "SlowReranker",
]
