"""Building the embedder, or explaining precisely why there is none.

The real embedder needs an optional extra whose runtime and weights are several
gigabytes. Requiring it for the whole API would make uploads, artifacts and
health checks depend on a machine-learning stack they never touch, so a process
without it still starts -- and serves everything except chat.

That is the shape worth being deliberate about. The alternative, substituting
some stand-in embedder so the route can exist, would produce a chat endpoint
that answers questions with vectors that mean nothing: retrieval would return
whatever the hash happened to put nearby, confidently and with citations. A
missing feature is legible; a feature that lies is not.

So this returns ``None`` rather than raising. The absence is expected, the
caller registers no chat route because of it, and the reason is reported once
at startup rather than discovered per request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_workbench.adapters.embedding.bge import (
    BgeM3Embedder,
    EmbeddingBackendUnavailableError,
    load_sentence_transformer,
)
from agent_workbench.bootstrap.projections import EmbeddingConfig
from agent_workbench.ports.embedding import EmbeddingPort


@dataclass(frozen=True, slots=True)
class EmbeddingUnavailable:
    """Why this process has no embedder, in words somebody can act on."""

    reason: str


def build_embedder(
    config: EmbeddingConfig,
    *,
    loader: Callable[..., Any] = load_sentence_transformer,
) -> EmbeddingPort | EmbeddingUnavailable:
    """Load the configured embedder, or say what is missing.

    A wrong vector width is a refusal rather than an absence: it means the
    configuration disagrees with itself, and starting anyway would build a
    collection nothing could write into. ``BgeM3Embedder.load`` raises for
    that, and the raise is deliberate -- unlike a missing extra, it is not a
    state anybody chose.
    """

    device = None if config.device == "auto" else config.device
    try:
        return BgeM3Embedder.load(
            model_id=config.model_id,
            revision=config.revision,
            expected_dimension=config.vector_size,
            batch_size=config.batch_size,
            device=device,
            loader=loader,
        )
    except EmbeddingBackendUnavailableError as missing:
        return EmbeddingUnavailable(reason=str(missing))
    except OSError as unreachable:
        # No weights locally and nothing to fetch them from. Distinguished from
        # a missing runtime because the fix is different: one is an install,
        # the other is a download or a mounted cache.
        return EmbeddingUnavailable(
            reason=(
                f"the weights for {config.model_id}@{config.revision} could not "
                f"be loaded ({type(unreachable).__name__}); this process serves "
                "everything except chat"
            )
        )


__all__ = ["EmbeddingUnavailable", "build_embedder"]
