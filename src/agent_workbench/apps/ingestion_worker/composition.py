"""Composition root for the durable PostgreSQL-to-Qdrant ingestion process."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.concurrency import BlockingCallRunner
from agent_workbench.adapters.embedding import DeterministicEmbedder
from agent_workbench.adapters.encoder import aclose_encoders
from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.ingestion import (
    ApproximateTokenCounter,
    TextDocumentParser,
)
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.persistence import (
    PostgresExecutionGuardFactory,
    PostgresOutbox,
    PostgresWorkerPresenceStore,
    create_query_engine,
)
from agent_workbench.adapters.persistence.knowledge_graph import (
    PostgresKnowledgeGraphStore,
)
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.graph_enrichment import GraphEnrichmentService
from agent_workbench.application.graph_extraction import (
    GraphExtractionService,
    graph_identity,
)
from agent_workbench.application.ingestion import IngestionService
from agent_workbench.application.worker_presence import WorkerPresenceBeacon
from agent_workbench.apps.ingestion_worker.identity import restore_document_owner
from agent_workbench.bootstrap.deployment import deployment_label
from agent_workbench.bootstrap.embedding_factory import (
    EmbeddingUnavailable,
    build_embedder,
)
from agent_workbench.bootstrap.model_factory import build_model
from agent_workbench.bootstrap.projections import IngestionWorkerRuntimeConfig
from agent_workbench.bootstrap.qdrant_startup import verify_qdrant_startup
from agent_workbench.bootstrap.sparse_factory import (
    SparseEncodingUnavailable,
    build_sparse_encoder,
)
from agent_workbench.ports.embedding import EmbeddingPort
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.sparse import SparseEncoderPort
from agent_workbench.runtime.agent_runtime import ClaudeLikeAgentRuntime
from agent_workbench.runtime.tool_gateway import ToolGateway
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
    # Present only when the second pass is configured; owned here because the
    # process that opened it is the one that must close it.
    model_http: httpx.AsyncClient | None = None
    #: What this process assembled to encode with. Remote encoders hold a
    #: connection pool to `agent-encoder` (ADR-0106) and are closed here; the
    #: in-process ones hold nothing and are skipped.
    encoders: tuple[object, ...] = ()

    #: This process saying it is here, on a timer (ADR-0110); see the Task
    #: Worker's container for why it is assembled here and not in ``serve``.
    presence: WorkerPresenceBeacon | None = None

    async def startup(self) -> None:
        """Fail closed before claiming work from the durable outbox."""

        await verify_qdrant_startup(
            self.qdrant,
            qdrant=self.config.qdrant,
            embedding=self.config.embedding,
        )

    async def dispose(self) -> None:
        """Release every external client on every process exit path."""

        await self.qdrant.close()
        if self.model_http is not None:
            await self.model_http.aclose()
        await aclose_encoders(*self.encoders)
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

    # ADR-042. One pool per process: every blocking adapter in this process
    # draws from it, so the bound is a ceiling rather than three private ones.
    blocking = BlockingCallRunner(
        slots=config.blocking_calls.slots,
        queue_timeout_seconds=config.blocking_calls.queue_timeout_seconds,
    )

    if demo:
        embedder = DeterministicEmbedder(dimension=config.embedding.vector_size)
        sparse_encoder = None
    else:
        if embedder is None:
            built_dense = build_embedder(config.embedding, runner=blocking)
            if isinstance(built_dense, EmbeddingUnavailable):
                raise IngestionBackendUnavailableError(built_dense.reason)
            embedder = built_dense
        if sparse_encoder is None and config.embedding.sparse_enabled:
            built_sparse = build_sparse_encoder(config.embedding, runner=blocking)
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
    artifacts = LocalArtifactStore(Path(config.artifacts.local_root), runner=blocking)
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
    enrichment, http = _graph_enrichment(config, ingestion=ingestion, engine=engine)
    worker = IngestionWorker(
        engine=engine,
        outbox=PostgresOutbox(engine),
        ingestion=ingestion,
        artifacts=artifacts,
        worker_id=config.worker_id,
        enrichment=enrichment,
        # Paired with enrichment by the worker's own construction check, and
        # paired here too: identity is decided at this boundary and nowhere
        # else (ADR-012).
        principal_for=(
            (
                lambda tenant_id, owner_id: restore_document_owner(
                    tenant_id=tenant_id, owner_id=owner_id
                )
            )
            if enrichment is not None
            else None
        ),
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
        model_http=http,
        encoders=(embedder, sparse_encoder),
        presence=WorkerPresenceBeacon(
            PostgresWorkerPresenceStore(engine),
            worker_id=config.worker_id,
            kind="ingestion",
            deployment=deployment_label(),
            capabilities={
                "demo": demo,
                "sparse": bool(config.embedding.sparse_enabled),
                "collection": str(config.qdrant.write_collection),
            },
            interval_seconds=float(config.heartbeat_seconds),
        ),
    )


__all__ = [
    "IngestionBackendUnavailableError",
    "IngestionWorkerDependencies",
    "build_ingestion_worker_dependencies",
]


def _graph_enrichment(
    config: IngestionWorkerRuntimeConfig,
    *,
    ingestion: IngestionService,
    engine: AsyncEngine,
) -> tuple[GraphEnrichmentService | None, httpx.AsyncClient | None]:
    """The second pass, or an honest absence (ADR-037 §2.6).

    Refuses rather than degrades when the graph is switched on without the
    means to run it. That is the opposite of the retriever's behaviour, and
    deliberately: a missing arm at query time is a degradation nobody asked
    for, while a worker configured to extract and unable to is a deployment
    that will silently never build the graph it was told to build.
    """

    if not config.graph.enabled:
        return None, None
    if config.model is None or config.model.api_key is None:
        raise IngestionBackendUnavailableError(
            "rag.graph.enabled requires a model and an API key in this process"
        )

    http = httpx.AsyncClient(timeout=config.graph.timeout_seconds)
    model = build_model(config.model, client=http)
    empty = StaticToolRegistry([])
    runtime = ClaudeLikeAgentRuntime(
        model=model,
        # Toolless by construction, not by an envelope that could widen: the
        # extractor reads one passage and returns JSON.
        gateway=ToolGateway(
            registry=empty, policy=EnvelopePolicyEngine(registry=empty)
        ),
        policy_identity=f"ingestion-graph:{config.graph.prompt_version}",
        model_label=config.model.profiles[config.graph.extraction_profile].model_id,
    )
    extraction = GraphExtractionService(
        executor=runtime,
        timeout_seconds=config.graph.timeout_seconds,
        sink_for=lambda stream_id: ScopedEventSink(
            log=InMemoryEventLog(),
            scope=EventScope(stream_id=stream_id, run_id=stream_id),
        ),
    )
    return (
        GraphEnrichmentService(
            ingestion=ingestion,
            extraction=extraction,
            store=PostgresKnowledgeGraphStore(engine),
            graph_identity=graph_identity(
                extraction_model=config.model.profiles[
                    config.graph.extraction_profile
                ].model_id,
                prompt_version=config.graph.prompt_version,
                embedder_identity=ingestion.embedder.identity,
            ),
        ),
        http,
    )
