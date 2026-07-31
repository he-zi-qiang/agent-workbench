from __future__ import annotations

from typing import Any

from agent_workbench.bootstrap.projections import EmbeddingConfig
from agent_workbench.bootstrap.sparse_factory import (
    SparseEncodingUnavailable,
    build_sparse_encoder,
)


class _LexicalModel:
    def encode(self, sentences: list[str], **_: Any) -> dict[str, object]:
        return {"lexical_weights": [{} for _ in sentences]}


def _config(*, enabled: bool = True) -> EmbeddingConfig:
    return EmbeddingConfig(
        model_id="BAAI/bge-m3",
        revision="fixed-revision",
        vector_size=1024,
        batch_size=8,
        device="cpu",
        sparse_enabled=enabled,
        sparse_vocabulary_size=250_002,
    )


def test_sparse_factory_builds_the_configured_lexical_projection() -> None:
    built = build_sparse_encoder(
        _config(),
        loader=lambda *_args, **_kwargs: _LexicalModel(),
        vocabulary_size=lambda _model, _revision: 250_002,
    )

    assert not isinstance(built, SparseEncodingUnavailable)
    assert built.identity == "BAAI/bge-m3@fixed-revision-sparse"


def test_sparse_factory_reports_an_explicitly_disabled_arm() -> None:
    built = build_sparse_encoder(_config(enabled=False))

    assert isinstance(built, SparseEncodingUnavailable)
    assert "disabled" in built.reason
