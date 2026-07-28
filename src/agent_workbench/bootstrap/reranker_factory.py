"""Building the reranker, or explaining precisely why there is none.

Same optional runtime as the embedder, and the same refusal to substitute
something. But the absence means something different here, and the difference
is the reason this is a separate factory rather than a branch in that one.

Without an embedder there is no chat at all: nothing can turn a question into a
query, so the route is not registered. Without a reranker there is still chat --
retrieval returns a usable order, and reranking was only ever going to improve
it. So a missing reranker degrades the answer's quality, while a missing
embedder removes the capability.

That asymmetry is why this returns ``None`` and the caller carries on. It is
also why the reason is still reported: "chat is running without reranking"
looks exactly like "chat is running with reranking" from the outside, and an
ablation report written against a process that silently had no reranker would
attribute the difference to the model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_workbench.adapters.reranking.bge_reranker import (
    BgeReranker,
    load_cross_encoder,
)
from agent_workbench.bootstrap.projections import RerankerConfig
from agent_workbench.ports.reranker import RerankerPort, RerankerUnavailableError


@dataclass(frozen=True, slots=True)
class RerankerUnavailable:
    """Why this process has no reranker, in words somebody can act on."""

    reason: str


def build_reranker(
    config: RerankerConfig,
    *,
    loader: Callable[..., Any] = load_cross_encoder,
) -> RerankerPort | RerankerUnavailable:
    """Load the configured reranker, or say what is missing.

    Every failure here is an absence rather than a refusal, which is the
    opposite of the embedder's rule and follows from what each one does. A
    mismatched embedder would write vectors into a collection that cannot hold
    them -- a configuration that disagrees with itself, and starting anyway
    corrupts an index. A reranker has no such shape to disagree about: it
    produces one number per pair whatever it is. So there is nothing it can be
    wrong about that is worth refusing to serve chat over.
    """

    device = None if config.device == "auto" else config.device
    try:
        return BgeReranker.load(
            model_id=config.model_id,
            revision=config.revision,
            batch_size=config.batch_size,
            device=device,
            loader=loader,
        )
    except RerankerUnavailableError as missing:
        return RerankerUnavailable(reason=str(missing))
    except OSError as unreachable:
        # No weights locally and nothing to fetch them from. Distinguished from
        # a missing runtime because the fix is different: one is an install,
        # the other is a download or a mounted cache.
        return RerankerUnavailable(
            reason=(
                f"the weights for {config.model_id}@{config.revision} could not "
                f"be loaded ({type(unreachable).__name__}); chat serves "
                "unreranked retrieval results"
            )
        )


__all__ = ["RerankerUnavailable", "build_reranker"]
