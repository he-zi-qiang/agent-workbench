from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_workbench.adapters.embedding import DeterministicEmbedder
from agent_workbench.apps.ingestion_worker.composition import (
    IngestionBackendUnavailableError,
    build_ingestion_worker_dependencies,
)
from agent_workbench.bootstrap.embedding_factory import EmbeddingUnavailable
from agent_workbench.bootstrap.projections import (
    ArtifactStoreConfig,
    DatabaseConfig,
    EmbeddingConfig,
    IngestionConfig,
    IngestionWorkerRuntimeConfig,
    QdrantConfig,
)


def _config(tmp_path: Path) -> IngestionWorkerRuntimeConfig:
    return IngestionWorkerRuntimeConfig(
        database=DatabaseConfig(
            dsn=SecretStr("postgresql+asyncpg://user:password@localhost/example"),
            application_name="agent-workbench-ingestion-test",
            statement_timeout_ms=30_000,
            pool_size=1,
            max_overflow=0,
        ),
        artifacts=ArtifactStoreConfig(
            backend="local",
            local_root=str(tmp_path),
            max_artifact_bytes=1_048_576,
        ),
        qdrant=QdrantConfig(
            url="http://localhost:6333",
            read_alias="knowledge_active",
            write_collection="knowledge_test",
            api_key=None,
            request_timeout_seconds=10,
            distance="cosine",
            allow_local_bootstrap=True,
        ),
        embedding=EmbeddingConfig(
            model_id="unit",
            revision="fixed",
            vector_size=4,
            batch_size=2,
            device="cpu",
            sparse_enabled=False,
        ),
        ingestion=IngestionConfig(
            chunk_size_tokens=128,
            chunk_overlap_tokens=16,
        ),
        worker_id="ingester_test",
        claim_limit=8,
        poll_seconds=0.1,
        error_backoff_seconds=1,
        lease_seconds=90,
        heartbeat_seconds=20,
    )


def test_composition_writes_the_concrete_collection_not_the_read_alias(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[str, str]:
        dependencies = build_ingestion_worker_dependencies(
            _config(tmp_path),
            embedder=DeterministicEmbedder(dimension=4),
        )
        try:
            service = dependencies.worker.ingestion
            return type(dependencies.worker.outbox).__name__, service.index_identity
        finally:
            await dependencies.dispose()

    outbox_type, identity = asyncio.run(scenario())
    assert outbox_type == "PostgresOutbox"
    assert identity.startswith("deterministic-hash-v1-4+")


def test_production_composition_refuses_a_missing_dense_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agent_workbench.apps.ingestion_worker.composition.build_embedder",
        lambda _config: EmbeddingUnavailable(reason="install embedding extra"),
    )

    with pytest.raises(IngestionBackendUnavailableError, match="embedding extra"):
        build_ingestion_worker_dependencies(_config(tmp_path))


def test_demo_is_explicit_and_does_not_load_model_weights(tmp_path: Path) -> None:
    async def scenario() -> str:
        dependencies = build_ingestion_worker_dependencies(
            _config(tmp_path),
            demo=True,
        )
        try:
            return dependencies.worker.ingestion.embedder.identity
        finally:
            await dependencies.dispose()

    assert asyncio.run(scenario()) == "deterministic-hash-v1-4"
