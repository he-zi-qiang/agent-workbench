"""Lexical weights: which terms a passage is about, and how strongly.

Sparse retrieval answers a different question from dense retrieval. Dense finds
passages whose meaning is near a query; sparse finds passages that use the
query's words. They fail differently -- dense misses an exact identifier it has
never seen, sparse misses a paraphrase -- which is the whole reason for fusing
them rather than picking one.

A vector here is indices into a vocabulary and the weight of each. It is not a
compressed dense vector: those have no term to point at, so a fusion built on
one is fusing a representation with itself. ADR-013 records how that mistake
looks from the outside, because it does not raise anything.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_workbench.domain.schema import DomainModel


class SparseVector(DomainModel):
    """Term weights, as positions in a vocabulary and the weight of each."""

    indices: tuple[int, ...] = ()
    values: tuple[float, ...] = ()

    def __len__(self) -> int:
        return len(self.indices)


@runtime_checkable
class SparseEncoderPort(Protocol):
    """Lexical weights for passages and for queries."""

    @property
    def vocabulary_size(self) -> int:
        """How many terms the weights index into.

        Exposed because it is the one number that distinguishes real lexical
        weights from a sparse re-encoding of a dense vector: the first has one
        dimension per token in the tokenizer, the second has whatever its head
        was sized to. A collection built on the wrong one retrieves plausibly
        and matches no terms.
        """
        ...

    @property
    def identity(self) -> str:
        """Model and revision, as they appear in an index identity."""
        ...

    async def encode_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[SparseVector, ...]:
        """Weights for passages, in the order given."""
        ...

    async def encode_query(self, text: str) -> SparseVector:
        """Weights for one query."""
        ...


class SparseEncodingUnavailableError(RuntimeError):
    """The optional sparse runtime is not installed in this environment."""


__all__ = [
    "SparseEncoderPort",
    "SparseEncodingUnavailableError",
    "SparseVector",
]
