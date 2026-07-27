"""The BGE adapter's own logic, and then the model's actual contract.

Two tiers, deliberately. Everything the adapter itself decides -- batching, the
dimension check, the identity, what happens when the runtime is missing -- runs
everywhere against a stand-in encoder, because none of it needs two gigabytes
of weights to be true.

The second tier is what only the real model can answer: that it produces
vectors of the width the configuration promises. That one is skipped without
weights, and the skip says so rather than passing quietly.
"""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from agent_workbench.adapters.embedding.bge import (
    BgeM3Embedder,
    EmbeddingBackendUnavailableError,
    load_sentence_transformer,
)
from agent_workbench.ports.embedding import EmbeddingPort

WEIGHTS_ENV_VAR = "AGENT_WORKBENCH_TEST_EMBEDDING_MODEL"

MODEL_ID = "BAAI/bge-m3"
REVISION = "main"
CONFIGURED_DIMENSION = 1024


@dataclass
class _StandInEncoder:
    """Satisfies the encoder Protocol without loading anything.

    Records how it was called, which is the only way to assert that the
    adapter batches the way it was configured to.
    """

    dimension: int = 4
    reports_dimension: bool = True
    calls: list[dict[str, object]] = field(default_factory=list)

    def get_sentence_embedding_dimension(self) -> int | None:
        # A real model can return None -- the sentinel and the value have to be
        # separate here, or the None case is untestable.
        return self.dimension if self.reports_dimension else None

    def encode(
        self,
        sentences: list[str],
        *,
        batch_size: int,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> Sequence[Sequence[float]]:
        self.calls.append(
            {
                "sentences": list(sentences),
                "batch_size": batch_size,
                "normalize_embeddings": normalize_embeddings,
            }
        )
        return [
            [float(len(text) + offset) for offset in range(self.dimension)]
            for text in sentences
        ]


def _embedder(encoder: _StandInEncoder, **overrides: object) -> BgeM3Embedder:
    return BgeM3Embedder(
        model=encoder,
        model_id=MODEL_ID,
        revision=str(overrides.get("revision", REVISION)),
        batch_size=int(str(overrides.get("batch_size", 16))),
        _dimension=encoder.dimension,
    )


# --- what the adapter decides ------------------------------------------------


def test_it_satisfies_the_port() -> None:
    assert isinstance(_embedder(_StandInEncoder()), EmbeddingPort)


def test_the_identity_names_the_model_and_the_revision() -> None:
    """Two revisions produce vectors close enough to look interchangeable.

    They are not, and nothing errors when they are mixed -- retrieval simply
    gets worse. The identity is what turns a revision change into a re-index.
    """

    one = _embedder(_StandInEncoder())
    other = _embedder(_StandInEncoder(), revision="a" * 40)

    assert one.identity != other.identity
    assert MODEL_ID in one.identity
    assert REVISION in one.identity


def test_the_configured_batch_size_reaches_the_model() -> None:
    encoder = _StandInEncoder()

    asyncio.run(_embedder(encoder, batch_size=4).embed_documents(("a", "b", "c")))

    assert encoder.calls[0]["batch_size"] == 4


def test_vectors_are_asked_for_normalized() -> None:
    """Cosine distance in Qdrant assumes it; asking the model is cheaper here."""

    encoder = _StandInEncoder()

    asyncio.run(_embedder(encoder).embed_documents(("a",)))

    assert encoder.calls[0]["normalize_embeddings"] is True


def test_documents_keep_their_order() -> None:
    """The caller pairs vectors with chunks by position."""

    encoder = _StandInEncoder()

    vectors = asyncio.run(_embedder(encoder).embed_documents(("a", "bb", "ccc")))

    assert [vector[0] for vector in vectors] == [1.0, 2.0, 3.0]


def test_an_empty_batch_does_not_call_the_model() -> None:
    """Loading a GPU for nothing is a cost with no result."""

    encoder = _StandInEncoder()

    assert asyncio.run(_embedder(encoder).embed_documents(())) == ()
    assert encoder.calls == []


def test_a_query_is_embedded_as_one_vector() -> None:
    vector = asyncio.run(_embedder(_StandInEncoder()).embed_query("fusion"))

    assert len(vector) == 4


# --- refusing to start wrong -------------------------------------------------


def test_a_model_of_the_wrong_width_is_refused_at_load() -> None:
    """It could not write into the collection that width created.

    Qdrant would reject the upsert eventually. Failing here names the cause
    while the process is still starting, rather than at the first document.
    """

    with pytest.raises(ValueError, match="768"):
        _load(_StandInEncoder(dimension=768), expected=CONFIGURED_DIMENSION)


def test_a_model_that_reports_no_dimension_is_refused() -> None:
    """``get_sentence_embedding_dimension`` is documented to return None."""

    with pytest.raises(ValueError, match="does not report"):
        _load(
            _StandInEncoder(reports_dimension=False),
            expected=CONFIGURED_DIMENSION,
        )


def test_a_model_of_the_right_width_loads() -> None:
    """The control: the refusal is about the mismatch, not about loading."""

    embedder = _load(_StandInEncoder(dimension=4), expected=4)

    assert embedder.dimension == 4


def test_a_non_positive_batch_size_is_refused() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        _load(_StandInEncoder(), expected=4, batch_size=0)


def test_a_missing_runtime_says_what_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extra is optional, so its absence has to be a legible message."""

    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", None)

    with pytest.raises(EmbeddingBackendUnavailableError, match="--extra embedding"):
        load_sentence_transformer(MODEL_ID, revision=REVISION)


def _load(
    encoder: _StandInEncoder, *, expected: int, batch_size: int = 16
) -> BgeM3Embedder:
    """Drive the real ``load`` with a stand-in in place of the download."""

    return BgeM3Embedder.load(
        model_id=MODEL_ID,
        revision=REVISION,
        expected_dimension=expected,
        batch_size=batch_size,
        loader=lambda *args, **kwargs: encoder,
    )


# --- what only the real model can answer -------------------------------------


def _real() -> BgeM3Embedder:
    model_id = os.environ.get(WEIGHTS_ENV_VAR)
    if not model_id:
        pytest.skip(
            f"{WEIGHTS_ENV_VAR} is not set: the real BGE-M3 contract needs "
            "the 'embedding' extra and local weights"
        )
    return BgeM3Embedder.load(
        model_id=model_id,
        revision=REVISION,
        expected_dimension=CONFIGURED_DIMENSION,
        batch_size=2,
    )


def test_the_real_model_produces_the_configured_width() -> None:
    """The plan's stated gate: actual output dimension equals the configuration."""

    embedder = _real()

    vectors = asyncio.run(embedder.embed_documents(("dense retrieval", "sparse")))

    assert embedder.dimension == CONFIGURED_DIMENSION
    assert [len(vector) for vector in vectors] == [
        CONFIGURED_DIMENSION,
        CONFIGURED_DIMENSION,
    ]


def test_the_real_model_returns_unit_vectors() -> None:
    vector = asyncio.run(_real().embed_query("how does hybrid fusion work"))

    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-4)


def test_the_real_model_is_deterministic() -> None:
    """A re-index must not move a document because the model felt different."""

    embedder = _real()

    first = asyncio.run(embedder.embed_documents(("dense retrieval",)))
    second = asyncio.run(embedder.embed_documents(("dense retrieval",)))

    assert first[0] == pytest.approx(second[0], abs=1e-6)


def test_the_real_model_puts_related_text_closer_than_unrelated() -> None:
    """The one property the stand-in cannot have, and the reason to run this."""

    embedder = _real()
    query = asyncio.run(embedder.embed_query("how are dense and sparse fused"))
    related, unrelated = asyncio.run(
        embedder.embed_documents(
            (
                "Qdrant fuses dense and sparse candidates with reciprocal rank "
                "fusion in a single query.",
                "Preheat the oven to 200 degrees and rest the dough for an hour.",
            )
        )
    )

    def similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert similarity(query, related) > similarity(query, unrelated)
