"""BGE-M3 lexical weights, and the one check that tells them from a fake.

ADR-013: sentence-transformers' SparseEncoder attaches a SparseAutoEncoder to
BGE-M3 and produces a 4096-dimensional re-encoding of the dense vector. It
stores, fuses and evaluates without raising, and matches no terms. The only
assertion that separates the two is dimensionality: real lexical weights index
the tokenizer's vocabulary, and nothing else does.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from agent_workbench.adapters.embedding.bge_sparse import BgeM3SparseEncoder
from agent_workbench.ports.sparse import SparseEncoderPort, SparseVector

WEIGHTS_ENV_VAR = "AGENT_WORKBENCH_TEST_EMBEDDING_MODEL"

MODEL_ID = "BAAI/bge-m3"
REVISION = "main"
# XLM-RoBERTa, which BGE-M3 is built on.
VOCABULARY = 250002
# What SparseEncoder produces instead, and what this guard exists to reject.
AUTOENCODER_WIDTH = 4096


class _StandInEncoder:
    """Satisfies the encoder Protocol without loading anything."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def encode(
        self,
        sentences: list[str],
        *,
        batch_size: int,
        return_dense: bool,
        return_sparse: bool,
        return_colbert_vecs: bool,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "batch_size": batch_size,
                "return_dense": return_dense,
                "return_sparse": return_sparse,
            }
        )
        return {
            "lexical_weights": [
                {"7": 0.9, "3": 0.4}
                for _ in sentences  # deliberately unsorted
            ]
        }


def _load(width: int = VOCABULARY, **overrides: Any) -> BgeM3SparseEncoder:
    encoder = overrides.pop("encoder", _StandInEncoder())
    return BgeM3SparseEncoder.load(
        model_id=MODEL_ID,
        revision=REVISION,
        expected_vocabulary_size=overrides.pop("expected", VOCABULARY),
        loader=lambda *a, **k: encoder,
        vocabulary_size=lambda *a: width,
        **overrides,
    )


# --- the guard from ADR-013 --------------------------------------------------


def test_a_sparse_head_that_is_not_the_vocabulary_is_refused() -> None:
    """The whole point. A 4096-wide head stores and fuses and matches nothing."""

    with pytest.raises(ValueError, match="ADR-013"):
        _load(width=AUTOENCODER_WIDTH)


def test_the_refusal_names_both_widths() -> None:
    """So the reader can see it is an autoencoder rather than a vocabulary."""

    with pytest.raises(ValueError, match=f"{AUTOENCODER_WIDTH}"):
        _load(width=AUTOENCODER_WIDTH)


def test_the_tokenizers_vocabulary_is_accepted() -> None:
    """The control: the refusal is about width, not about loading."""

    assert _load().vocabulary_size == VOCABULARY


# --- the adapter's own behaviour ---------------------------------------------


def test_it_satisfies_the_port() -> None:
    assert isinstance(_load(), SparseEncoderPort)


def test_weights_are_sorted_by_index() -> None:
    """Two encodings of one passage must not differ only in term order."""

    vector = asyncio.run(_load().encode_query("fusion"))

    assert vector.indices == (3, 7)
    assert vector.values == (0.4, 0.9)


def test_only_sparse_is_requested() -> None:
    """Asking for dense here would pay for a vector nothing in this path uses."""

    encoder = _StandInEncoder()
    asyncio.run(_load(encoder=encoder).encode_documents(("a",)))

    assert encoder.calls[0]["return_sparse"] is True
    assert encoder.calls[0]["return_dense"] is False


def test_an_empty_batch_does_not_call_the_model() -> None:
    encoder = _StandInEncoder()

    assert asyncio.run(_load(encoder=encoder).encode_documents(())) == ()
    assert encoder.calls == []


def test_the_identity_is_distinct_from_the_dense_one() -> None:
    """They index different collections and must not share an identity."""

    assert _load().identity.endswith("-sparse")


def test_a_non_positive_batch_size_is_refused() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        _load(batch_size=0)


# --- what only the real model can answer -------------------------------------


def _real() -> BgeM3SparseEncoder:
    if not os.environ.get(WEIGHTS_ENV_VAR):
        pytest.skip(
            f"{WEIGHTS_ENV_VAR} is not set: real lexical weights need the "
            "'embedding' extra and local weights"
        )
    return BgeM3SparseEncoder.load(
        model_id=MODEL_ID, revision=REVISION, expected_vocabulary_size=VOCABULARY
    )


def test_the_real_model_indexes_its_own_vocabulary() -> None:
    """The assertion ADR-013 says is the only one that can tell real from fake."""

    assert _real().vocabulary_size == VOCABULARY


def test_the_real_model_weights_real_terms() -> None:
    """A term the passage uses must carry weight; sparse is term matching."""

    encoder = _real()
    vector: SparseVector = asyncio.run(encoder.encode_query("reciprocal rank fusion"))

    assert len(vector) > 0
    assert max(vector.indices) < VOCABULARY
    assert all(value > 0 for value in vector.values)


def test_the_real_model_gives_different_terms_to_different_text() -> None:
    """If two unrelated passages weighted the same terms, it is not lexical."""

    encoder = _real()
    one = asyncio.run(encoder.encode_query("reciprocal rank fusion"))
    other = asyncio.run(encoder.encode_query("preheat the oven and rest the dough"))

    assert set(one.indices) != set(other.indices)
