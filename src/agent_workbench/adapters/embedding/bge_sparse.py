"""BGE-M3 lexical weights, from BGE's own library.

Why not sentence-transformers, which this project already depends on: its
``SparseEncoder`` attaches a ``SparseAutoEncoder`` to any model that declares no
sparse head, and BGE-M3 declares none. For BGE-M3 that yields a 4096-dimensional
re-encoding of the dense vector rather than weights over a 250002-token
vocabulary -- a representation with no term to point at. It does not raise; it
retrieves plausibly and matches no words. ADR-013 has the measurement.

The same reasoning that makes the dense adapter optional applies here, so this
lives behind the same extra and imports inside its loader.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from agent_workbench.ports.sparse import (
    SparseEncodingUnavailableError,
    SparseVector,
)


class LexicalEncoder(Protocol):
    """The part of a FlagEmbedding model this adapter uses."""

    def encode(
        self,
        sentences: list[str],
        *,
        batch_size: int,
        return_dense: bool,
        return_sparse: bool,
        return_colbert_vecs: bool,
    ) -> dict[str, Any]: ...


def load_bge_m3(
    model_id: str, *, revision: str, use_fp16: bool = False
) -> LexicalEncoder:
    """Load BGE-M3, or say what is missing."""

    try:
        import FlagEmbedding  # pyright: ignore[reportMissingImports, reportMissingTypeStubs]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SparseEncodingUnavailableError(
            "sparse retrieval needs the 'embedding' extra; "
            "install it with: uv sync --extra embedding"
        ) from exc

    _require_lexical_projection(model_id, revision=revision)
    return cast(
        "LexicalEncoder",
        FlagEmbedding.BGEM3FlagModel(  # pyright: ignore[reportUnknownMemberType]
            model_id, revision=revision, use_fp16=use_fp16
        ),
    )


def _require_lexical_projection(model_id: str, *, revision: str) -> None:
    """Refuse a checkout whose trained lexical head is not actually present.

    BGE-M3 keeps ``sparse_linear.pt`` beside the base weights rather than
    inside them, and FlagEmbedding does not treat its absence as an error: it
    constructs a fresh ``Linear`` and carries on. Everything downstream then
    succeeds while meaning nothing. The vectors are the right width -- the
    width comes from the tokenizer vocabulary, not from this projection, so
    the dimension guard in ``BgeM3SparseEncoder.load`` passes -- they are
    merely a random projection, different in every process.

    That failure has no symptom a caller can see. It cost this project two
    retracted diagnoses and an ablation report whose hybrid arm was a sequence
    of unrelated samples, so the check is here, before the model is built,
    rather than left to whoever next notices that a number moved.
    """

    try:
        # Unresolvable to the type checker by design: huggingface_hub arrives
        # with the 'embedding' extra, which CI does not install. Requiring it
        # there would make an optional dependency mandatory for the gates.
        from huggingface_hub import (  # pyright: ignore[reportMissingImports]
            try_to_load_from_cache,  # pyright: ignore[reportUnknownVariableType]
        )
    except ImportError:  # pragma: no cover - depends on the environment
        # Nothing to check against. This runs only where the sparse runtime is
        # absent too, so the encoder is about to fail for a plainer reason.
        return

    lookup = cast("Callable[..., object]", try_to_load_from_cache)
    cached = lookup(model_id, "sparse_linear.pt", revision=revision)
    if isinstance(cached, str):
        return
    raise SparseEncodingUnavailableError(
        f"{model_id}@{revision} has no cached sparse_linear.pt, so its lexical "
        "weights would be a randomly initialised projection rather than the "
        'trained one. Fetch it with: python -c "from huggingface_hub import '
        f"hf_hub_download; hf_hub_download('{model_id}', 'sparse_linear.pt', "
        f"revision='{revision}')\""
    )


@dataclass(frozen=True, slots=True)
class BgeM3SparseEncoder:
    """Term weights over BGE-M3's vocabulary."""

    model: LexicalEncoder
    model_id: str
    revision: str
    batch_size: int = 16
    _vocabulary_size: int = field(default=0)

    @classmethod
    def load(
        cls,
        *,
        model_id: str,
        revision: str,
        expected_vocabulary_size: int,
        batch_size: int = 16,
        loader: Callable[..., LexicalEncoder] = load_bge_m3,
        vocabulary_size: Callable[[str, str], int] | None = None,
    ) -> BgeM3SparseEncoder:
        """Load the model and refuse anything whose weights are not lexical.

        The vocabulary check is the whole guard. A sparse head of some other
        width produces vectors Qdrant accepts and fusion happily consumes,
        while matching no terms -- so the only way to know the arm is real is
        that its dimensionality is the tokenizer's.
        """

        if batch_size < 1:
            raise ValueError("batch_size must be positive")

        measured = (
            vocabulary_size(model_id, revision)
            if vocabulary_size is not None
            else _tokenizer_vocabulary_size(model_id, revision)
        )
        if measured != expected_vocabulary_size:
            raise ValueError(
                f"{model_id}@{revision} indexes {measured} terms, but "
                f"rag.embedding expects {expected_vocabulary_size}; a width "
                "that is not the tokenizer's is not lexical weights (ADR-013)"
            )
        return cls(
            model=loader(model_id, revision=revision),
            model_id=model_id,
            revision=revision,
            batch_size=batch_size,
            _vocabulary_size=measured,
        )

    @property
    def vocabulary_size(self) -> int:
        return self._vocabulary_size

    @property
    def identity(self) -> str:
        return f"{self.model_id}@{self.revision}-sparse"

    async def encode_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[SparseVector, ...]:
        if not texts:
            return ()
        return await self._encode(texts)

    async def encode_query(self, text: str) -> SparseVector:
        return (await self._encode((text,)))[0]

    async def _encode(self, texts: tuple[str, ...]) -> tuple[SparseVector, ...]:
        def run() -> tuple[SparseVector, ...]:
            output = self.model.encode(
                list(texts),
                batch_size=self.batch_size,
                return_dense=False,
                return_sparse=True,
                return_colbert_vecs=False,
            )
            return tuple(_to_vector(weights) for weights in output["lexical_weights"])

        # Compute-bound and synchronous, like the dense encoder. Awaiting it
        # inline would hold the loop for a whole batch.
        return await asyncio.to_thread(run)


def _to_vector(weights: Any) -> SparseVector:
    """FlagEmbedding returns ``{token_id: weight}``; Qdrant wants two arrays.

    Sorted by index because a sparse vector with the same terms in a different
    order is the same vector, and two encodings of one passage that differ only
    in order would look like different content to anything comparing digests.
    """

    items = sorted(
        (int(token_id), float(weight))
        for token_id, weight in cast("dict[Any, Any]", weights).items()
    )
    return SparseVector(
        indices=tuple(index for index, _ in items),
        values=tuple(value for _, value in items),
    )


def _tokenizer_vocabulary_size(model_id: str, revision: str) -> int:
    """How many terms this model's tokenizer indexes.

    Read from the tokenizer rather than from the encoder, so the number the
    guard compares against comes from the model's own vocabulary and not from
    whatever a sparse head happened to be sized to.
    """

    try:
        import transformers  # pyright: ignore[reportMissingImports]
    except ImportError as exc:  # pragma: no cover - ships with the extra
        raise SparseEncodingUnavailableError(
            "sparse retrieval needs the 'embedding' extra"
        ) from exc

    tokenizer = cast(
        "Any",
        transformers.AutoTokenizer.from_pretrained(  # pyright: ignore[reportUnknownMemberType]
            model_id, revision=revision
        ),
    )
    return int(tokenizer.vocab_size)


__all__ = [
    "BgeM3SparseEncoder",
    "LexicalEncoder",
    "load_bge_m3",
]
