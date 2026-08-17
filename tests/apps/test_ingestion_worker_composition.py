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
    GraphExtractionConfig,
    IngestionConfig,
    IngestionWorkerRuntimeConfig,
    ModelConfig,
    ModelProfileConfig,
    QdrantConfig,
)


def _model(*, api_key: str | None) -> ModelConfig:
    profile = ModelProfileConfig(
        model_id="deepseek-chat",
        temperature=0.0,
        max_output_tokens=1024,
        timeout_seconds=45.0,
        max_retries=1,
        tool_calling_required=False,
        thinking="unsupported",
        reasoning_effort="high",
        prices=None,
    )
    return ModelConfig(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        api_key=SecretStr(api_key) if api_key is not None else None,
        profiles={"main": profile, "compact": profile},
    )


def _config(
    tmp_path: Path,
    *,
    graph: GraphExtractionConfig | None = None,
    model: ModelConfig | None = None,
) -> IngestionWorkerRuntimeConfig:
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
        graph=graph
        or GraphExtractionConfig(
            enabled=False,
            prompt_version="v1",
            extraction_profile="compact",
            timeout_seconds=45.0,
        ),
        model=model,
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
        lambda _config, **_: EmbeddingUnavailable(reason="install embedding extra"),
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


# --- the second pass, and the three states it can be configured in -----------


def _enabled_graph() -> GraphExtractionConfig:
    return GraphExtractionConfig(
        enabled=True,
        prompt_version="v1",
        extraction_profile="compact",
        timeout_seconds=45.0,
    )


def test_a_worker_without_the_graph_builds_no_extractor(tmp_path: Path) -> None:
    """The default, and the control for both tests below."""

    async def scenario() -> tuple[object, object]:
        dependencies = build_ingestion_worker_dependencies(
            _config(tmp_path),
            embedder=DeterministicEmbedder(dimension=4),
        )
        try:
            return (
                dependencies.worker.enrichment,
                dependencies.worker.principal_for,
            )
        finally:
            await dependencies.dispose()

    enrichment, principal_for = asyncio.run(scenario())
    assert enrichment is None
    # Paired: a worker that could attribute an extraction it never runs would
    # be carrying half a feature.
    assert principal_for is None


def test_the_graph_switched_on_without_a_key_refuses_to_assemble(
    tmp_path: Path,
) -> None:
    """Refusing beats degrading here, and the direction is deliberate.

    A missing arm at query time is a degradation nobody asked for; a worker
    told to extract and unable to would silently never build the graph it was
    configured to build, and nothing downstream could tell that apart from a
    corpus that yielded nothing.
    """

    with pytest.raises(IngestionBackendUnavailableError, match="API key"):
        build_ingestion_worker_dependencies(
            _config(
                tmp_path,
                graph=_enabled_graph(),
                model=_model(api_key=None),
            ),
            embedder=DeterministicEmbedder(dimension=4),
        )


def test_the_graph_switched_on_with_a_key_builds_the_second_pass(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[object, object, str]:
        dependencies = build_ingestion_worker_dependencies(
            _config(
                tmp_path,
                graph=_enabled_graph(),
                model=_model(api_key="sk-test"),
            ),
            embedder=DeterministicEmbedder(dimension=4),
        )
        try:
            enrichment = dependencies.worker.enrichment
            assert enrichment is not None
            return (
                enrichment,
                dependencies.worker.principal_for,
                enrichment.graph_identity,
            )
        finally:
            # Closes the HTTP client this process opened for the model.
            await dependencies.dispose()

    enrichment, principal_for, identity = asyncio.run(scenario())
    assert enrichment is not None
    assert principal_for is not None
    # Every part that changes what was extracted is in the identity.
    assert "deepseek-chat" in identity
    assert "v1" in identity
    assert "deterministic-hash-v1-4" in identity
