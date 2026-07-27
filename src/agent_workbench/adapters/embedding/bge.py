"""BGE-M3 dense embeddings, loaded from local weights.

Optional on purpose. The runtime this needs is a multi-gigabyte install and the
weights are a couple more, so a checkout without them still imports this module,
still runs every other test, and fails only if something actually asks for a
real embedder. The import therefore happens inside the loader rather than at
module scope -- a top-level ``import torch`` would make the whole package
unusable for anyone who skipped the extra.

Dense only. BGE-M3 also produces sparse and multi-vector representations, and
the library that exposes those (FlagEmbedding) carries a wider and less stable
surface than this needs today. Sparse is WP05-01; wiring in a heavier dependency
now, for a capability nothing consumes yet, is how a project acquires a
dependency it cannot explain.

The identity is the model id and the revision together. Two revisions of one
model produce vectors that are close enough to look interchangeable and are
not, which is the failure worth preventing: nothing errors, retrieval simply
gets worse. Because the identity feeds the index identity, a revision change is
a re-index rather than a slow contamination of the neighbourhood.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, cast

from agent_workbench.ports.embedding import Vector

# What BGE asks to be prepended to a query but not to a passage. Getting this
# backwards costs recall and reports nothing, so the two paths are separate
# methods on the port and separate prefixes here.
QUERY_INSTRUCTION: Final[str] = ""


class EmbeddingBackendUnavailableError(RuntimeError):
    """The optional embedding runtime is not installed in this environment."""


class SentenceEncoder(Protocol):
    """The part of a sentence-transformers model this adapter actually uses.

    Declared here rather than imported, because the package may not be
    installed -- and type-checking must not depend on an optional extra. It
    also states the surface plainly: a change to either method is a change this
    adapter has to notice.

    The dimension accessor is deliberately absent. Its name changed between
    supported versions, so asking for one of them by name in a Protocol would
    make the other version fail to satisfy it. ``reported_dimension`` below
    does the asking.
    """

    def encode(
        self,
        sentences: list[str],
        *,
        batch_size: int,
        normalize_embeddings: bool,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> Sequence[Sequence[float]]: ...


def load_sentence_transformer(
    model_id: str,
    *,
    revision: str,
    device: str | None = None,
) -> SentenceEncoder:
    """Load the model, or say plainly what is missing.

    Imported here rather than at module scope so a checkout without the extra
    still imports this package.
    """

    try:
        # Unresolvable to the type checker by design: CI does not install the
        # extra, and requiring it there would make an optional dependency
        # mandatory for anyone running the gates.
        import sentence_transformers  # pyright: ignore[reportMissingImports]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise EmbeddingBackendUnavailableError(
            "the real embedder needs the 'embedding' extra; "
            "install it with: uv sync --extra embedding"
        ) from exc

    # trust_remote_code stays off: loading a model must not execute code the
    # repository happens to ship alongside the weights.
    return cast(
        "SentenceEncoder",
        sentence_transformers.SentenceTransformer(  # pyright: ignore[reportUnknownMemberType]
            model_id,
            revision=revision,
            device=device,
            trust_remote_code=False,
        ),
    )


def reported_dimension(model: object) -> int | None:
    """Ask the model its output width, under whichever name it answers to.

    sentence-transformers renamed ``get_sentence_embedding_dimension`` to
    ``get_embedding_dimension``; the supported range spans both, and the old
    name warns on the newer versions. Found by actually loading the model --
    the stand-in encoder answers to whichever name it is written with, so no
    amount of testing against it would have shown this.
    """

    for name in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
        accessor = getattr(model, name, None)
        if accessor is None:
            continue
        value = accessor()
        if value is not None:
            return int(cast("int", value))
    return None


@dataclass(frozen=True, slots=True)
class BgeM3Embedder:
    """Dense BGE-M3 vectors, batched, off the event loop."""

    model: SentenceEncoder
    model_id: str
    revision: str
    batch_size: int = 16
    _dimension: int = field(default=0)

    @classmethod
    def load(
        cls,
        *,
        model_id: str,
        revision: str,
        expected_dimension: int,
        batch_size: int = 16,
        device: str | None = None,
        loader: Callable[..., SentenceEncoder] = load_sentence_transformer,
    ) -> BgeM3Embedder:
        """Load the model and refuse it if it does not match the configuration.

        A model whose output width differs from the configured ``vector_size``
        cannot write into the collection that width created. Qdrant would
        reject the upsert eventually; failing here instead names the cause
        while the process is still starting, rather than at the first document.

        ``loader`` is injectable so these checks can be exercised against a
        stand-in. A test that re-implemented them would be asserting its own
        copy, which is how a guard comes to protect something other than the
        thing it names.
        """

        if batch_size < 1:
            raise ValueError("batch_size must be positive")

        model = loader(model_id, revision=revision, device=device)
        reported = reported_dimension(model)
        if reported is None:
            raise ValueError(
                f"{model_id}@{revision} does not report an embedding dimension"
            )
        actual = int(reported)
        if actual != expected_dimension:
            raise ValueError(
                f"{model_id}@{revision} produces {actual}-dimensional vectors, "
                f"but rag.embedding.vector_size is {expected_dimension}"
            )
        return cls(
            model=model,
            model_id=model_id,
            revision=revision,
            batch_size=batch_size,
            _dimension=actual,
        )

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def identity(self) -> str:
        return f"{self.model_id}@{self.revision}"

    async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        if not texts:
            return ()
        return await self._encode(texts)

    async def embed_query(self, text: str) -> Vector:
        encoded = await self._encode((QUERY_INSTRUCTION + text,))
        return encoded[0]

    async def _encode(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        """Run the model in a worker thread.

        ``encode`` is synchronous and compute-bound. Awaiting it inline would
        hold the event loop for the whole batch, which on a shared process
        means every other request waits behind one document's embeddings. The
        bounded executor that this should eventually draw from belongs to the
        coordination work package; a default worker thread is the honest
        interim, and it is still strictly better than blocking the loop.
        """

        def run() -> tuple[Vector, ...]:
            vectors = self.model.encode(
                list(texts),
                batch_size=self.batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return tuple(tuple(float(value) for value in row) for row in vectors)

        return await asyncio.to_thread(run)


__all__ = [
    "QUERY_INSTRUCTION",
    "BgeM3Embedder",
    "EmbeddingBackendUnavailableError",
    "SentenceEncoder",
    "load_sentence_transformer",
    "reported_dimension",
]
