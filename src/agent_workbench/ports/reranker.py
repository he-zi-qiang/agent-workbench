"""Scoring a query against passages that have already been retrieved.

A reranker reads the query and the passage together, which is what the
retriever could not do: dense and sparse both encode a passage once, before any
question exists, and then compare two vectors. Reading the pair is more
expensive by orders of magnitude, which is why it runs over tens of candidates
rather than the whole corpus, and why it is allowed to be slow enough to need a
timeout.

``rerank`` returns one score per passage, positionally, rather than a reordered
list of passages. That is deliberate and it is the safety property of this
port. An adapter that returned passages could drop one, repeat one, or return
something that was never given to it, and the caller would have no way to tell
a bug from a ranking. Scores make the contract checkable by length, and they
leave the reordering with the caller -- which is the layer that knows which
candidates PostgreSQL authorized. "The reranker cannot introduce a passage the
asker may not read" is then true by construction rather than by trust.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RerankerPort(Protocol):
    """Relevance of each passage to one query."""

    @property
    def identity(self) -> str:
        """Model and revision, as they appear in an evaluation report.

        A reranker is a ranking decision, so a report that names the retriever
        but not this is a report whose numbers cannot be reproduced.
        """
        ...

    async def rerank(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        """Score each passage against the query, in the order given.

        Exactly ``len(passages)`` scores, aligned by position. Higher is more
        relevant; the scale is the model's own and is comparable only within
        one call, so callers may sort by it and must not threshold on it.
        """
        ...


class RerankerUnavailableError(RuntimeError):
    """The optional reranking runtime is not installed in this environment."""


class RerankerContractError(RuntimeError):
    """An adapter returned something that is not one score per passage."""


__all__ = [
    "RerankerContractError",
    "RerankerPort",
    "RerankerUnavailableError",
]
