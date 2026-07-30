"""Composition root for the durable PostgreSQL-to-Qdrant ingestion process."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.embedding import DeterministicEmbedder
from agent_workbench.adapters.ingestion import (
    ApproximateTokenCounter,
    TextDocumentParser,
)
from agent_workbench.adapters.persistence import (
    PostgresExecutionGuardFactory,
    PostgresOutbox,
    create_query_engine,
)
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.ingestion import IngestionService
from agent_workbench.bootstrap.embedding_factory import (
    EmbeddingUnavailable,
    build_embedder,
)
from agent_workbench.bootstrap.projections import IngestionWorkerRuntimeConfig
from agent_workbench.bootstrap.qdrant_startup import verify_qdrant_startup
from agent_workbench.bootstrap.sparse_factory import (
    SparseEncodingUnavailable,
    build_sparse_encoder,
)
from agent_workbench.ports.embedding import EmbeddingPort
from agent_workbench.ports.sparse import SparseEncoderPort
from agent_workbench.workers.ingestion import IngestionWorker


class IngestionBackendUnavailableError(RuntimeError):
    """The process cannot honestly build the configured derived index."""


@dataclass(frozen=True, slots=True)
class IngestionWorkerDependencies:
    """Long-lived resources owned by exactly one ingestion process."""

    config: IngestionWorkerRuntimeConfig
    engine: AsyncEngine
    qdrant: AsyncQdrantClient
    artifacts: LocalArtifactStore
    guards: PostgresExecutionGuardFactory
    worker: IngestionWorker

    async def startup(self) -> None:
        """Fail closed before claiming work from the durable outbox."""

        await verify_qdrant_startup(
            self.qdrant,
            qdrant=self.config.qdrant,
            embedding=self.config.embedding,
        )

    async def dispose(self) -> None:
        """Release both external clients on every process exit path."""

        await self.qdrant.close()
        await self.guards.dispose()
        await self.engine.dispose()


def build_ingestion_worker_dependencies(
    config: IngestionWorkerRuntimeConfig,
    *,
    embedder: EmbeddingPort | None = None,
    sparse_encoder: SparseEncoderPort | None = None,
    demo: bool = False,
) -> IngestionWorkerDependencies:
    """Assemble a real hybrid writer, or an explicitly requested local demo.

    Production never substitutes a deterministic embedding model.  If either
    trained BGE component is unavailable, startup refuses before an outbox
    claim can be acknowledged.
    """

    if config.artifacts.backend != "local":
        raise ValueError(
            f"the {config.artifacts.backend} artifact backend has no ingestion "
            "Worker adapter"
        )
    if demo and (embedder is not None or sparse_encoder is not None):
        raise ValueError("demo cannot be combined with injected encoders")

    if demo:
        embedder = DeterministicEmbedder(dimension=config.embedding.vector_size)
        sparse_encoder = None
    else:
        if embedder is None:
            built_dense = build_embedder(config.embedding)
            if isinstance(built_dense, EmbeddingUnavailable):
                raise IngestionBackendUnavailableError(built_dense.reason)
            embedder = built_dense
        if sparse_encoder is None and config.embedding.sparse_enabled:
            built_sparse = build_sparse_encoder(config.embedding)
            if isinstance(built_sparse, SparseEncodingUnavailable):
                raise IngestionBackendUnavailableError(built_sparse.reason)
            sparse_encoder = built_sparse

    engine = create_query_engine(
        config.database.dsn.get_secret_value(),
        application_name=config.database.application_name,
        statement_timeout_ms=config.database.statement_timeout_ms,
        pool_size=config.database.pool_size,
        max_overflow=config.database.max_overflow,
    )
    qdrant = AsyncQdrantClient(
        url=config.qdrant.url,
        api_key=(
            config.qdrant.api_key.get_secret_value()
            if config.qdrant.api_key is not None
            else None
        ),
        timeout=config.qdrant.request_timeout_seconds,
    )
    artifacts = LocalArtifactStore(Path(config.artifacts.local_root))
    guard_dsn = config.database.guard_dsn or config.database.dsn
    guards = PostgresExecutionGuardFactory(
        guard_dsn.get_secret_value(),
        healthcheck_seconds=float(config.database.guard_healthcheck_seconds),
        application_name=f"{config.database.application_name}-document-guard",
    )
    index = QdrantVectorIndex(
        qdrant,
        collection=config.qdrant.write_collection,
    )
    ingestion = IngestionService(
        parser=TextDocumentParser(),
        chunker=Chunker(
            size_tokens=config.ingestion.chunk_size_tokens,
            overlap_tokens=config.ingestion.chunk_overlap_tokens,
            counter=ApproximateTokenCounter(),
        ),
        embedder=embedder,
        sparse_encoder=sparse_encoder,
        index=index,
    )
    worker = IngestionWorker(
        engine=engine,
        outbox=PostgresOutbox(engine),
        ingestion=ingestion,
        artifacts=artifacts,
        worker_id=config.worker_id,
        guards=guards,
        lease_seconds=float(config.lease_seconds),
        heartbeat_seconds=float(config.heartbeat_seconds),
    )
    return IngestionWorkerDependencies(
        config=config,
        engine=engine,
        qdrant=qdrant,
        artifacts=artifacts,
        guards=guards,
        worker=worker,
    )


__all__ = [
    "IngestionBackendUnavailableError",
    "IngestionWorkerDependencies",
    "build_ingestion_worker_dependencies",
]
