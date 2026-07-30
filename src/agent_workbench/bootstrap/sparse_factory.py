"""Build the configured lexical encoder without hiding an absent runtime.

Dense-only retrieval is a valid, explicitly reported mode for the API.  The
ingestion process is stricter: when the deployment says that hybrid retrieval
is enabled, starting without the trained lexical projection would create
dense-only points in a collection advertised as hybrid.  Returning a typed
absence here lets each composition root make that policy decision itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_workbench.adapters.embedding.bge_sparse import (
    BgeM3SparseEncoder,
    load_bge_m3,
)
from agent_workbench.bootstrap.projections import EmbeddingConfig
from agent_workbench.ports.sparse import (
    SparseEncoderPort,
    SparseEncodingUnavailableError,
)


@dataclass(frozen=True, slots=True)
class SparseEncodingUnavailable:
    """Why this process cannot produce BGE-M3 lexical term weights."""

    reason: str


def build_sparse_encoder(
    config: EmbeddingConfig,
    *,
    loader: Callable[..., Any] = load_bge_m3,
    vocabulary_size: Callable[[str, str], int] | None = None,
) -> SparseEncoderPort | SparseEncodingUnavailable:
    """Load the configured lexical encoder, or return an actionable absence."""

    if not config.sparse_enabled:
        return SparseEncodingUnavailable(
            reason="sparse encoding is disabled by the embedding configuration"
        )

    try:
        return BgeM3SparseEncoder.load(
            model_id=config.model_id,
            revision=config.revision,
            expected_vocabulary_size=config.sparse_vocabulary_size,
            batch_size=config.batch_size,
            loader=loader,
            vocabulary_size=vocabulary_size,
        )
    except (SparseEncodingUnavailableError, ImportError, OSError) as missing:
        return SparseEncodingUnavailable(reason=str(missing))


__all__ = ["SparseEncodingUnavailable", "build_sparse_encoder"]
