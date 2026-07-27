"""Degrading legibly when the optional runtime is not installed.

The decision under test is that a missing embedder is an *absence*, reported
once, and not a substitution. Substituting a stand-in so the chat route could
exist would produce an endpoint that answers from vectors with no meaning --
confidently, with citations. A missing feature is legible; a lying one is not.
"""

from __future__ import annotations

import sys

import pytest

from agent_workbench.bootstrap.embedding_factory import (
    EmbeddingUnavailable,
    build_embedder,
)
from agent_workbench.bootstrap.projections import EmbeddingConfig
from agent_workbench.ports.embedding import EmbeddingPort


def _config(**overrides: object) -> EmbeddingConfig:
    fields: dict[str, object] = {
        "model_id": "BAAI/bge-m3",
        "revision": "main",
        "vector_size": 1024,
        "batch_size": 16,
        "device": "auto",
    }
    fields.update(overrides)
    return EmbeddingConfig(**fields)  # pyright: ignore[reportArgumentType]


def test_a_missing_runtime_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The absence is expected, so it is a value the caller can branch on."""

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    outcome = build_embedder(_config())

    assert isinstance(outcome, EmbeddingUnavailable)


def test_the_reason_says_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reported once at startup, so it has to be actionable on its own."""

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    outcome = build_embedder(_config())

    assert isinstance(outcome, EmbeddingUnavailable)
    assert "--extra embedding" in outcome.reason


def test_nothing_is_substituted_when_the_runtime_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole decision, stated as an assertion.

    A stand-in embedder here would make chat answer from vectors that mean
    nothing, which is worse than not answering at all.
    """

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    outcome = build_embedder(_config())

    assert not isinstance(outcome, EmbeddingPort)


class _StandInEncoder:
    """Reports a width, so the mismatch branch is reachable without weights."""

    def get_embedding_dimension(self) -> int:
        return 1024


def test_a_width_that_disagrees_with_the_configuration_raises() -> None:
    """Not an absence: the configuration disagrees with itself.

    Starting anyway would build a collection nothing could write into, so this
    is a refusal rather than a degraded mode -- it is not a state anybody
    chose, and the caller must not treat it as one.
    """

    with pytest.raises(ValueError, match="vector_size"):
        build_embedder(_config(vector_size=7), loader=lambda *a, **k: _StandInEncoder())


def test_a_matching_width_produces_an_embedder() -> None:
    """The control: the refusals above are about configuration, not loading."""

    built = build_embedder(
        _config(vector_size=1024), loader=lambda *a, **k: _StandInEncoder()
    )

    assert isinstance(built, EmbeddingPort)
