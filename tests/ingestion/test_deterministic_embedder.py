"""The shape of the embedding contract, without a model in the loop."""

from __future__ import annotations

import asyncio
import math

import pytest

from agent_workbench.adapters.embedding import DeterministicEmbedder
from agent_workbench.ports.embedding import EmbeddingPort


def test_it_satisfies_the_port() -> None:
    assert isinstance(DeterministicEmbedder(), EmbeddingPort)


def test_every_vector_has_the_configured_dimension() -> None:
    embedder = DeterministicEmbedder(dimension=1024)

    vectors = asyncio.run(embedder.embed_documents(("a", "b")))

    assert [len(vector) for vector in vectors] == [1024, 1024]


def test_vectors_are_unit_length() -> None:
    """Cosine distance is only well behaved on normalized vectors."""

    vector = asyncio.run(DeterministicEmbedder(dimension=64).embed_query("fusion"))

    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


def test_the_same_text_embeds_the_same_way() -> None:
    """Determinism is the whole point: a test must not depend on a run."""

    embedder = DeterministicEmbedder()

    first = asyncio.run(embedder.embed_documents(("fusion",)))
    second = asyncio.run(embedder.embed_documents(("fusion",)))

    assert first == second


def test_different_text_embeds_differently() -> None:
    embedder = DeterministicEmbedder()

    vectors = asyncio.run(embedder.embed_documents(("fusion", "retrieval")))

    assert vectors[0] != vectors[1]


def test_a_query_and_a_passage_are_not_the_same_input() -> None:
    """Real models prepend different instructions to each.

    Kept distinct here so a caller that wires the two paths up backwards fails
    a test rather than quietly losing recall.
    """

    embedder = DeterministicEmbedder()

    passage = asyncio.run(embedder.embed_documents(("fusion",)))[0]
    query = asyncio.run(embedder.embed_query("fusion"))

    assert passage != query


def test_results_keep_the_order_they_were_given() -> None:
    """The caller pairs vectors with chunks by position."""

    embedder = DeterministicEmbedder()

    batch = asyncio.run(embedder.embed_documents(("a", "b", "c")))
    singles = [asyncio.run(embedder.embed_documents((t,)))[0] for t in ("a", "b", "c")]

    assert list(batch) == singles


def test_the_identity_does_not_look_like_a_real_model() -> None:
    """An index built with this must never be mistaken for one built with BGE."""

    identity = DeterministicEmbedder(dimension=8).identity

    assert "deterministic" in identity
    assert "8" in identity


def test_a_dimension_of_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="dimension"):
        DeterministicEmbedder(dimension=0)
