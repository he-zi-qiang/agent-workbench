"""Turning text into vectors, and saying which model did it.

Two methods rather than one, because a query and a passage are not the same
input to most embedding models. Several -- BGE among them -- prepend different
instructions to each, and a port that offered a single ``embed`` would force an
adapter to guess. A guess that is wrong costs recall silently: nothing errors,
the neighbourhood is simply worse, and no test that only checks shapes would
ever notice.

``identity`` is not decoration. Vectors from two different models are not
comparable, so a collection is only meaningful alongside the model that filled
it. The identity travels into the index's own identity, which is what makes
"the embedder changed" a re-index rather than a slow corruption of the
neighbourhood.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

Vector = tuple[float, ...]


@runtime_checkable
class EmbeddingPort(Protocol):
    """Dense embeddings for passages and for queries."""

    @property
    def dimension(self) -> int:
        """Length of every vector this embedder produces."""
        ...

    @property
    def identity(self) -> str:
        """Model and revision, as they should appear in an index identity.

        Stable for a given model and revision, and different whenever either
        changes. A deployment that pins a model by a moving tag gets an
        identity that moves with it, which is the honest answer: the vectors
        did move.
        """
        ...

    async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        """Embed passages, in the order given.

        The order is part of the contract: the caller pairs results with the
        chunks it sent by position, and a reordering would attach every vector
        to the wrong text without failing anything.
        """
        ...

    async def embed_query(self, text: str) -> Vector:
        """Embed one query."""
        ...


__all__ = ["EmbeddingPort", "Vector"]
