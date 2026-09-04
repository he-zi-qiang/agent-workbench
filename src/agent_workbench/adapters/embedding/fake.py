"""A deterministic embedder, for everything that is not the model.

Hashes text into a fixed-size vector. It has no semantics -- similar sentences
are not near each other -- and that is deliberate: the tests it serves are
about the index, the pipeline and the authorization boundary, none of which
should pass or fail because a model's neighbourhood shifted.

What it does reproduce faithfully is the shape of the contract: a fixed
dimension, unit-length vectors so cosine distance behaves, stable output for
stable input, and an identity that is visibly not a real model's. That last
part matters most -- an index built with this must never be mistakable for one
built with BGE, and the identity is what a collection is named after.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass

from agent_workbench.ports.embedding import Vector
from agent_workbench.ports.sparse import SparseVector

# Prepended before hashing so the same string embeds differently as a query
# than as a passage, the way a real model's instructions make it. Without this
# a test could not tell the two paths apart, and a caller that wired them up
# backwards would look correct.
DOCUMENT_PREFIX = "passage: "
QUERY_PREFIX = "query: "


@dataclass(frozen=True, slots=True)
class DeterministicEmbedder:
    """Stable pseudo-embeddings with no meaning behind them."""

    dimension: int = 8

    def __post_init__(self) -> None:
        if self.dimension < 1:
            raise ValueError("dimension must be positive")

    @property
    def identity(self) -> str:
        return f"deterministic-hash-v1-{self.dimension}"

    async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        return tuple(self._embed(DOCUMENT_PREFIX + text) for text in texts)

    async def embed_query(self, text: str) -> Vector:
        return self._embed(QUERY_PREFIX + text)

    def _embed(self, text: str) -> Vector:
        # Enough digest bytes for the whole vector, extended by counter so the
        # dimension is not capped at what one hash happens to produce.
        raw = b""
        block = 0
        needed = self.dimension * 4
        while len(raw) < needed:
            raw += hashlib.sha256(f"{block}:{text}".encode()).digest()
            block += 1

        values = [
            struct.unpack_from(">i", raw, offset * 4)[0] / 2**31
            for offset in range(self.dimension)
        ]
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0.0:  # pragma: no cover - a zero digest is not reachable
            return tuple([1.0] + [0.0] * (self.dimension - 1))
        return tuple(value / norm for value in values)


@dataclass(frozen=True, slots=True)
class DeterministicSparseEncoder:
    """Stable pseudo lexical weights, with no vocabulary behind them.

    The same bargain as :class:`DeterministicEmbedder`, for the other arm: a
    term index is the hash of a whitespace token, so the same text always gives
    the same indices, two different texts almost never share one, and nothing
    about the result depends on a model. It exists so the encoder service and
    its client adapters (ADR-0106) can be exercised end to end, over a real
    socket, without weights -- what those tests are about is that a vector
    survives the wire unchanged, not what the vector means.

    ``vocabulary_size`` defaults to BGE-M3's real one so a projection built
    from ``config.default.toml`` accepts this double where it would accept the
    model; the identity says loudly that it is not the model.
    """

    vocabulary_size: int = 250_002

    @property
    def identity(self) -> str:
        return f"deterministic-hash-sparse-v1-{self.vocabulary_size}"

    async def encode_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[SparseVector, ...]:
        return tuple(self._encode(DOCUMENT_PREFIX + text) for text in texts)

    async def encode_query(self, text: str) -> SparseVector:
        return self._encode(QUERY_PREFIX + text)

    def _encode(self, text: str) -> SparseVector:
        weights: dict[int, float] = {}
        for token in text.split():
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.vocabulary_size
            weights[index] = weights.get(index, 0.0) + 1.0
        # Sorted by index, the way the real adapter orders them: the same terms
        # in a different order are the same vector, and a digest comparison
        # downstream should say so.
        items = sorted(weights.items())
        return SparseVector(
            indices=tuple(index for index, _ in items),
            values=tuple(value for _, value in items),
        )


__all__ = [
    "DOCUMENT_PREFIX",
    "QUERY_PREFIX",
    "DeterministicEmbedder",
    "DeterministicSparseEncoder",
]
