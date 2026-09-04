"""The three ports, each a thin shell over :class:`EncoderClient` (ADR-0106).

Every check the in-process adapters make at load time is made here at connect
time, against what the server reports rather than against what a loader
returned: a dense width that disagrees with ``rag.embedding.vector_size``, a
sparse width that is not the tokenizer's vocabulary, an identity that names a
different model than the configuration does. Each is a refusal rather than an
absence, for the reason ``bootstrap/embedding_factory.py`` gives -- it is a
configuration that disagrees with itself, and starting anyway would build or
read a collection the wrong model filled.

The identity check has no in-process counterpart and is worth a sentence. An
in-process adapter's identity is *composed* from the configuration, so it
cannot disagree with it. A remote one is *reported* by another process that
read its own configuration, and two processes reading two files is how a lean
API comes to write query vectors from one model into an index the ingestion
worker filled from another. The check makes that a startup refusal that names
both.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from agent_workbench.adapters.encoder.client import (
    DESCRIBE_TIMEOUT_SECONDS,
    EncoderClient,
    EncoderServiceUnavailableError,
    describe_service,
)
from agent_workbench.bootstrap.projections import EmbeddingConfig, RerankerConfig
from agent_workbench.ports.embedding import Vector
from agent_workbench.ports.reranker import RerankerContractError
from agent_workbench.ports.sparse import SparseVector


@dataclass(frozen=True, slots=True)
class RemoteEmbedder:
    """Dense vectors from the encoder service."""

    client: EncoderClient
    _identity: str
    _dimension: int

    @classmethod
    def connect(
        cls,
        config: EmbeddingConfig,
        *,
        timeout_seconds: float = DESCRIBE_TIMEOUT_SECONDS,
    ) -> RemoteEmbedder:
        described = describe_service(
            config.service_url, timeout_seconds=timeout_seconds
        )
        if described.dense is None:
            raise EncoderServiceUnavailableError(
                f"the encoder service at {config.service_url} loaded no dense model"
            )
        expected = f"{config.model_id}@{config.revision}"
        if described.dense.identity != expected:
            raise ValueError(
                f"the encoder service at {config.service_url} serves "
                f"{described.dense.identity}, but this process is configured "
                f"for {expected}; two processes disagreeing about the model "
                "would fill and query an index with different vectors"
            )
        if described.dense.dimension != config.vector_size:
            raise ValueError(
                f"{described.dense.identity} produces "
                f"{described.dense.dimension}-dimensional vectors, but "
                f"rag.embedding.vector_size is {config.vector_size}"
            )
        return cls(
            client=EncoderClient(config.service_url),
            _identity=described.dense.identity,
            _dimension=described.dense.dimension,
        )

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def identity(self) -> str:
        return self._identity

    async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        if not texts:
            return ()
        return await self.client.embed("document", texts)

    async def embed_query(self, text: str) -> Vector:
        return (await self.client.embed("query", (text,)))[0]

    async def aclose(self) -> None:
        await self.client.aclose()


@dataclass(frozen=True, slots=True)
class RemoteSparseEncoder:
    """Lexical weights from the encoder service."""

    client: EncoderClient
    _identity: str
    _vocabulary_size: int

    @classmethod
    def connect(
        cls,
        config: EmbeddingConfig,
        *,
        timeout_seconds: float = DESCRIBE_TIMEOUT_SECONDS,
    ) -> RemoteSparseEncoder:
        described = describe_service(
            config.service_url, timeout_seconds=timeout_seconds
        )
        if described.sparse is None:
            raise EncoderServiceUnavailableError(
                f"the encoder service at {config.service_url} loaded no sparse "
                "model, so this process cannot produce lexical weights"
            )
        expected = f"{config.model_id}@{config.revision}-sparse"
        if described.sparse.identity != expected:
            raise ValueError(
                f"the encoder service at {config.service_url} serves "
                f"{described.sparse.identity}, but this process is configured "
                f"for {expected}"
            )
        if described.sparse.vocabulary_size != config.sparse_vocabulary_size:
            # The whole guard `BgeM3SparseEncoder.load` makes, restated for a
            # width that arrived over the wire: a sparse head of another size
            # produces vectors Qdrant accepts and fusion consumes while
            # matching no terms (ADR-013).
            raise ValueError(
                f"{described.sparse.identity} indexes "
                f"{described.sparse.vocabulary_size} terms, but rag.embedding "
                f"expects {config.sparse_vocabulary_size}; a width that is not "
                "the tokenizer's is not lexical weights (ADR-013)"
            )
        return cls(
            client=EncoderClient(config.service_url),
            _identity=described.sparse.identity,
            _vocabulary_size=described.sparse.vocabulary_size,
        )

    @property
    def vocabulary_size(self) -> int:
        return self._vocabulary_size

    @property
    def identity(self) -> str:
        return self._identity

    async def encode_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[SparseVector, ...]:
        if not texts:
            return ()
        return await self.client.sparse("document", texts)

    async def encode_query(self, text: str) -> SparseVector:
        return (await self.client.sparse("query", (text,)))[0]

    async def aclose(self) -> None:
        await self.client.aclose()


@dataclass(frozen=True, slots=True)
class RemoteReranker:
    """Cross-encoder scores from the encoder service."""

    client: EncoderClient
    _identity: str

    @classmethod
    def connect(
        cls,
        config: RerankerConfig,
        *,
        timeout_seconds: float = DESCRIBE_TIMEOUT_SECONDS,
    ) -> RemoteReranker:
        described = describe_service(
            config.service_url, timeout_seconds=timeout_seconds
        )
        if described.reranker is None:
            raise EncoderServiceUnavailableError(
                f"the encoder service at {config.service_url} loaded no reranker"
            )
        expected = f"{config.model_id}@{config.revision}"
        if described.reranker.identity != expected:
            raise ValueError(
                f"the encoder service at {config.service_url} serves "
                f"{described.reranker.identity}, but this process is configured "
                f"for {expected}"
            )
        return cls(
            # The configured timeout, not the encode one: `RetrievalService`
            # already bounds a rerank with `rag.reranker.timeout_seconds` and
            # a transport that waited longer would be a timeout nobody set.
            client=EncoderClient(
                config.service_url, timeout_seconds=config.timeout_seconds
            ),
            _identity=described.reranker.identity,
        )

    @property
    def identity(self) -> str:
        return self._identity

    async def rerank(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        if not passages:
            return ()
        scores = await self.client.rerank(query, passages)
        if len(scores) != len(passages):
            raise RerankerContractError(
                f"{self.identity} returned {len(scores)} scores "
                f"for {len(passages)} passages"
            )
        return scores

    async def aclose(self) -> None:
        await self.client.aclose()


async def aclose_encoders(*encoders: object) -> None:
    """Close whichever of these hold a connection pool; ignore the rest.

    Called from the three composition roots' shutdown paths with whatever they
    assembled -- an in-process adapter, a remote one, or ``None`` -- so that a
    root need not branch on which kind it got. An in-process adapter has no
    ``aclose`` and nothing to close.
    """

    for encoder in encoders:
        close = getattr(encoder, "aclose", None)
        if callable(close):
            await cast("Callable[[], Awaitable[None]]", close)()


__all__ = [
    "RemoteEmbedder",
    "RemoteReranker",
    "RemoteSparseEncoder",
    "aclose_encoders",
]
