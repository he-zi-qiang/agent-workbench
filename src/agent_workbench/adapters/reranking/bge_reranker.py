"""BGE-reranker-v2-m3, loaded from local weights.

Optional for the same reason the embedder is: the runtime is a multi-gigabyte
install, the weights are more, and a checkout without them must still import
this module and run every other test. The import happens inside the loader
rather than at module scope.

This is a cross-encoder, not a bi-encoder. It reads the query and the passage
in one forward pass instead of comparing two vectors that were each produced
without knowledge of the other, which is what makes it worth running and also
what makes it expensive: cost is one pass per candidate, so it belongs behind
retrieval over tens of passages and nowhere near a corpus.

Its scores are unnormalised logits. They order passages within a single call
and mean nothing across calls, so this adapter does not rescale them into a
range that would invite a threshold. ``RerankerPort`` says the same thing; it
is repeated here because a scale that looks like a probability is the kind of
thing a later caller helpfully compares against 0.5.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from agent_workbench.adapters.concurrency.call_runner import (
    BlockingCallRunner,
    offload,
)
from agent_workbench.ports.reranker import (
    RerankerContractError,
    RerankerUnavailableError,
)


class CrossEncoderModel(Protocol):
    """The part of a cross-encoder this adapter uses.

    Declared rather than imported: type-checking must not depend on an optional
    extra, and stating the surface here means a change to it is a change this
    adapter has to notice.
    """

    def predict(
        self,
        sentences: list[tuple[str, str]],
        *,
        batch_size: int,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> Sequence[float]: ...


def load_cross_encoder(
    model_id: str,
    *,
    revision: str,
    device: str | None = None,
) -> CrossEncoderModel:
    """Load the model, or say plainly what is missing."""

    try:
        # Unresolvable to the type checker by design: CI does not install the
        # extra, and requiring it there would make an optional dependency
        # mandatory for anyone running the gates.
        import sentence_transformers  # pyright: ignore[reportMissingImports]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RerankerUnavailableError(
            "the real reranker needs the 'embedding' extra; "
            "install it with: uv sync --extra embedding"
        ) from exc

    # trust_remote_code stays off: loading a model must not execute code the
    # repository happens to ship alongside the weights.
    return cast(
        "CrossEncoderModel",
        sentence_transformers.CrossEncoder(  # pyright: ignore[reportUnknownMemberType]
            model_id,
            revision=revision,
            device=device,
            trust_remote_code=False,
        ),
    )


@dataclass(frozen=True, slots=True)
class BgeReranker:
    """Cross-encoder relevance scores, batched, off the event loop."""

    model: CrossEncoderModel
    model_id: str
    revision: str
    batch_size: int = 8
    #: ADR-042. ``None`` means the shared default executor; every production
    #: composition passes the bounded runner instead.
    runner: BlockingCallRunner | None = None

    @classmethod
    def load(
        cls,
        *,
        model_id: str,
        revision: str,
        batch_size: int = 8,
        device: str | None = None,
        loader: Callable[..., CrossEncoderModel] = load_cross_encoder,
        runner: BlockingCallRunner | None = None,
    ) -> BgeReranker:
        """Load the model.

        Unlike the embedder there is no width to check: a reranker produces one
        number per pair, and no configured shape can disagree with that. What
        can disagree is the identity, which is why it is carried rather than
        rediscovered.

        ``loader`` is injectable so the batching and contract checks can be
        exercised against a stand-in.
        """

        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        return cls(
            model=loader(model_id, revision=revision, device=device),
            model_id=model_id,
            revision=revision,
            batch_size=batch_size,
            runner=runner,
        )

    @property
    def identity(self) -> str:
        return f"{self.model_id}@{self.revision}"

    async def rerank(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        if not passages:
            return ()

        def run() -> tuple[float, ...]:
            scores = self.model.predict(
                [(query, passage) for passage in passages],
                batch_size=self.batch_size,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return tuple(float(score) for score in scores)

        scores = await offload(self.runner, run, name="bge-reranker-score")
        if len(scores) != len(passages):
            # Positional alignment is the entire contract. A short or long
            # result would silently pair scores with the wrong passages, which
            # reads as a bad ranking rather than as a defect.
            raise RerankerContractError(
                f"{self.identity} returned {len(scores)} scores "
                f"for {len(passages)} passages"
            )
        return scores


__all__ = [
    "BgeReranker",
    "CrossEncoderModel",
    "load_cross_encoder",
]
