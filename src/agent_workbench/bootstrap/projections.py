"""Narrow configuration objects, projected from validated settings.

A process is handed what it needs and nothing else. The API does not receive
the retrieval funnel, the coordination timings or the evaluation metrics, so no
route can come to depend on them and no change to them can alter how the API
behaves.

This is also where the settings type stops travelling. Everything past this
point takes a small frozen object, which is what lets the architecture guard
say plainly that raw configuration loading lives in exactly one package.

Secrets stay wrapped. A DSN is unwrapped where the client is constructed, not
where the configuration is assembled, so a stray repr of a config object cannot
print one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr

from agent_workbench.adapters.tools.export_artifact import (
    TOOL_NAME as EXPORT_ARTIFACT_TOOL,
)
from agent_workbench.bootstrap.settings import Settings
from agent_workbench.domain.identifiers import new_id
from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.schema import JsonObject
from agent_workbench.ports.task_workflow import GraphVersion

#: The permission ceiling every v1 Task is submitted under.
#:
#: It names one tool. The fixed graph has exactly one node that writes, the
#: envelope is stored with the Task and re-applied on every resume, and a
#: ceiling wide enough for a tool the graph does not have is a ceiling that
#: silently authorises the next tool somebody registers.
#:
#: ``approval_required_risks`` is empty rather than the deny-shaped default, and
#: that is the substantive decision here -- see ADR-015. v1 puts the human at
#: the *graph* boundary: the approval node interrupts, a person decides, and the
#: ledger records it. Leaving ``write`` in this tuple would put a second gate at
#: the *tool* boundary, which nothing in v1 can satisfy: the gateway's answer to
#: a tool needing approval is to refuse it. The result would be an approved Task
#: that cannot export -- a gate that only ever says no is not a gate.
TASK_V1_AUTHORIZATION_ENVELOPE = AuthorizationEnvelope(
    allowed_tools=(EXPORT_ARTIFACT_TOOL,),
    max_tool_risk="write",
    approval_required_risks=(),
)


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """What it takes to build the ordinary query engine."""

    dsn: SecretStr
    application_name: str
    statement_timeout_ms: int
    pool_size: int
    max_overflow: int
    # The task guard owns an entirely separate NullPool engine even when this
    # DSN intentionally falls back to ``dsn``. Sharing a string is allowed;
    # sharing a pooled connection is not, because advisory locks are session
    # scoped.
    guard_dsn: SecretStr | None = None
    guard_healthcheck_seconds: int = 5


@dataclass(frozen=True, slots=True)
class ArtifactStoreConfig:
    """Where artifacts live and how large one may be."""

    backend: Literal["local", "s3"]
    local_root: str
    max_artifact_bytes: int


@dataclass(frozen=True, slots=True)
class ModelProfileConfig:
    """One named profile, and how calls made under it should behave."""

    model_id: str
    temperature: float
    max_output_tokens: int | None
    timeout_seconds: float
    max_retries: int
    tool_calling_required: bool


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """The provider, and the profiles a run may ask for by name."""

    provider: str
    base_url: str
    # Absent is a real state, and the projection says so rather than papering
    # over it: a process may be configured without a key, and refusing to
    # assemble is the factory's job, not this one's.
    api_key: SecretStr | None
    profiles: Mapping[str, ModelProfileConfig]


@dataclass(frozen=True, slots=True)
class QdrantConfig:
    """Where the vector index lives, and what it is called."""

    url: str
    # Chat reads through this stable alias; ingestion writes the concrete
    # versioned collection below. They intentionally never share a name.
    read_alias: str
    write_collection: str
    api_key: SecretStr | None
    request_timeout_seconds: int
    distance: Literal["cosine"]
    allow_local_bootstrap: bool


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """Which model turns text into vectors, and how wide they are."""

    model_id: str
    revision: str
    vector_size: int
    batch_size: int
    device: str
    sparse_enabled: bool = True
    sparse_vocabulary_size: int = 250_002


@dataclass(frozen=True, slots=True)
class IngestionConfig:
    """The deterministic document boundaries and one outbox drain batch."""

    chunk_size_tokens: int
    chunk_overlap_tokens: int


@dataclass(frozen=True, slots=True)
class RerankerConfig:
    """Which cross-encoder reorders candidates, and how long it may take.

    The timeout is projected as a float because it bounds an ``asyncio.timeout``
    rather than a configuration display; keeping the settings integer would put
    the conversion at the call site, where it is easy to forget once.
    """

    model_id: str
    revision: str
    batch_size: int
    device: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """How much is asked for, and how much survives to the answer."""

    chunk_size_tokens: int
    chunk_overlap_tokens: int
    answer_context_k: int


@dataclass(frozen=True, slots=True)
class EventStreamConfig:
    """How a subscriber catches up, and how much it may pull at once."""

    replay_page_size: int
    catchup_poll_seconds: int


@dataclass(frozen=True, slots=True)
class ChatRecoveryConfig:
    """How the API bounds and reaps an uncheckpointed Chat execution."""

    orphan_grace_seconds: int
    reaper_poll_seconds: int
    reaper_batch_size: int
    disconnect_poll_seconds: int


@dataclass(frozen=True, slots=True)
class ChatConfig:
    """Which shape answers a chat turn, and what bounds it if it may loop."""

    retrieval_shape: Literal["fixed", "agentic"]
    max_agentic_steps: int
    max_agentic_searches: int


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    """Where this process sends what it records.

    Projected like every other config: the settings type stops at this
    boundary, so nothing past it can reinterpret an endpoint or a sample ratio.
    """

    service_name: str
    exporter_endpoint: str
    trace_sample_ratio: float
    metrics_enabled: bool


@dataclass(frozen=True, slots=True)
class TaskConfig:
    """The deployment decisions attached to a newly submitted Task.

    The authorization envelope names the fixed graph's own write tool and
    nothing else.  An interface must later narrow it further to the caller's
    actual grants; neither this projection nor the Worker is allowed to widen
    it merely because it can run a Task, and the principal's scopes are checked
    separately, so naming a tool here does not by itself let anyone reach it.

    ``run_semantics_snapshot`` is the deterministic template. A future Task
    submission factory resolves the Qdrant read alias to a concrete index
    before persisting it, but keeping the template here makes that dependency
    explicit without giving the API raw Settings.
    """

    graph_version: GraphVersion
    claim_poll_seconds: float
    lease_seconds: int
    heartbeat_seconds: int
    max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int
    run_semantics_snapshot: JsonObject
    run_semantics_revision: str
    policy_revision: str
    policy_fingerprint: str
    default_authorization_envelope: AuthorizationEnvelope


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfig:
    """The bounded custom model/tool loop a Task Worker owns."""

    max_steps: int
    max_tool_calls: int
    model_timeout_seconds: float
    max_parallel_read_tools: int


@dataclass(frozen=True, slots=True)
class TaskWorkerRuntimeConfig:
    """The minimum configuration of the current single-Worker process.

    This is deliberately not a lease/fencing or multi-worker projection. Those
    coordination mechanisms have not been assembled yet, so advertising a
    larger worker count here would be a false capability claim.
    """

    database: DatabaseConfig
    artifacts: ArtifactStoreConfig
    task: TaskConfig
    worker_id: str
    worker_concurrency: Literal[1]
    # Demo and adapter-injection tests intentionally do not need heavy model
    # runtimes. A normal projected Worker always carries these; real assembly
    # refuses their absence rather than substituting synthetic retrieval.
    model: ModelConfig | None = None
    qdrant: QdrantConfig | None = None
    embedding: EmbeddingConfig | None = None
    retrieval: RetrievalConfig | None = None
    runtime: AgentRuntimeConfig | None = None


@dataclass(frozen=True, slots=True)
class IngestionWorkerRuntimeConfig:
    """The narrow configuration owned by the derived-index writer."""

    database: DatabaseConfig
    artifacts: ArtifactStoreConfig
    qdrant: QdrantConfig
    embedding: EmbeddingConfig
    ingestion: IngestionConfig
    worker_id: str
    claim_limit: int
    poll_seconds: float
    error_backoff_seconds: float
    lease_seconds: int
    heartbeat_seconds: int


@dataclass(frozen=True, slots=True)
class ApiRuntimeConfig:
    """Everything the API process needs, and nothing else."""

    deployment_scope: Literal["local", "remote"]
    log_level: str
    host: str
    port: int
    request_timeout_seconds: int
    shutdown_grace_seconds: int
    sse_heartbeat_seconds: int
    max_control_request_body_bytes: int
    database: DatabaseConfig
    artifacts: ArtifactStoreConfig
    model: ModelConfig
    event_stream: EventStreamConfig
    qdrant: QdrantConfig
    embedding: EmbeddingConfig
    reranker: RerankerConfig
    retrieval: RetrievalConfig
    chat_recovery: ChatRecoveryConfig
    chat: ChatConfig
    task: TaskConfig
    observability: ObservabilityConfig


def project_observability(settings: Settings) -> ObservabilityConfig:
    """What this process records with, from the one loader every process uses."""

    return ObservabilityConfig(
        service_name=settings.observability.otel_service_name,
        exporter_endpoint=settings.observability.otel_exporter_endpoint,
        trace_sample_ratio=settings.observability.trace_sample_ratio,
        metrics_enabled=settings.observability.metrics_enabled,
    )


def project_task(settings: Settings) -> TaskConfig:
    """Project the Task submission decisions without assembling a process."""

    return TaskConfig(
        graph_version=settings.workflow.graph_version,
        claim_poll_seconds=settings.coordination.claim_poll_interval_ms / 1000,
        lease_seconds=settings.coordination.lease_duration_seconds,
        heartbeat_seconds=settings.coordination.heartbeat_interval_seconds,
        max_attempts=settings.coordination.max_attempts,
        retry_base_seconds=settings.coordination.retry_base_seconds,
        retry_max_seconds=settings.coordination.retry_max_seconds,
        run_semantics_snapshot=settings.task_run_semantics_snapshot(),
        run_semantics_revision=settings.task_run_semantics_revision(),
        policy_revision=settings.policy.revision,
        policy_fingerprint=settings.policy_fingerprint(),
        default_authorization_envelope=TASK_V1_AUTHORIZATION_ENVELOPE,
    )


def project_task_worker(
    settings: Settings, *, worker_id: str | None = None
) -> TaskWorkerRuntimeConfig:
    """Project one current Worker, rejecting unsupported concurrency early."""

    if settings.coordination.worker_concurrency != 1:
        raise ValueError(
            "Task Worker currently supports exactly one worker; "
            "lease/fencing-based multi-worker coordination is not assembled"
        )
    return TaskWorkerRuntimeConfig(
        database=DatabaseConfig(
            dsn=settings.database.dsn,
            application_name=settings.database.application_name,
            statement_timeout_ms=settings.database.statement_timeout_ms,
            pool_size=settings.database.query_pool_size,
            max_overflow=settings.database.query_pool_max_overflow,
            guard_dsn=settings.database.guard_dsn,
            guard_healthcheck_seconds=settings.database.guard_healthcheck_seconds,
        ),
        artifacts=ArtifactStoreConfig(
            backend=settings.artifact_store.backend,
            local_root=settings.artifact_store.local_root,
            max_artifact_bytes=settings.artifact_store.max_artifact_bytes,
        ),
        task=project_task(settings),
        worker_id=worker_id or new_id("worker"),
        worker_concurrency=1,
        model=ModelConfig(
            provider=settings.model.provider,
            base_url=settings.model.base_url,
            api_key=settings.secrets.deepseek_api_key,
            profiles={
                name: ModelProfileConfig(
                    model_id=profile.model_id,
                    temperature=profile.temperature,
                    max_output_tokens=profile.max_output_tokens,
                    timeout_seconds=float(profile.timeout_seconds),
                    max_retries=profile.max_retries,
                    tool_calling_required=profile.tool_calling_required,
                )
                for name, profile in (
                    ("main", settings.model.main),
                    ("compact", settings.model.compact),
                )
            },
        ),
        qdrant=_project_qdrant(settings),
        embedding=_project_embedding(settings),
        retrieval=RetrievalConfig(
            chunk_size_tokens=settings.rag.ingestion.chunk_size_tokens,
            chunk_overlap_tokens=settings.rag.ingestion.chunk_overlap_tokens,
            answer_context_k=settings.rag.retrieval.answer_context_k,
        ),
        runtime=AgentRuntimeConfig(
            max_steps=settings.runtime.max_steps,
            max_tool_calls=settings.runtime.max_tool_calls,
            model_timeout_seconds=float(settings.runtime.model_timeout_seconds),
            max_parallel_read_tools=settings.runtime.max_parallel_read_tools,
        ),
    )


def project_ingestion_worker(
    settings: Settings, *, worker_id: str | None = None
) -> IngestionWorkerRuntimeConfig:
    """Project the process that turns the durable outbox into a hybrid index."""

    return IngestionWorkerRuntimeConfig(
        database=DatabaseConfig(
            dsn=settings.database.dsn,
            application_name=f"{settings.database.application_name}-ingestion",
            statement_timeout_ms=settings.database.statement_timeout_ms,
            pool_size=settings.database.query_pool_size,
            max_overflow=settings.database.query_pool_max_overflow,
        ),
        artifacts=ArtifactStoreConfig(
            backend=settings.artifact_store.backend,
            local_root=settings.artifact_store.local_root,
            max_artifact_bytes=settings.artifact_store.max_artifact_bytes,
        ),
        qdrant=_project_qdrant(settings),
        embedding=_project_embedding(settings),
        ingestion=IngestionConfig(
            chunk_size_tokens=settings.rag.ingestion.chunk_size_tokens,
            chunk_overlap_tokens=settings.rag.ingestion.chunk_overlap_tokens,
        ),
        worker_id=worker_id or new_id("ingester"),
        # One document can spend most of a lease in a model. Claiming a large
        # batch would make the tail expire before processing even starts; the
        # model does its own configured batching inside this one document.
        claim_limit=1,
        poll_seconds=settings.coordination.claim_poll_interval_ms / 1000,
        error_backoff_seconds=float(settings.coordination.retry_base_seconds),
        lease_seconds=settings.coordination.lease_duration_seconds,
        heartbeat_seconds=settings.coordination.heartbeat_interval_seconds,
    )


def _project_qdrant(settings: Settings) -> QdrantConfig:
    return QdrantConfig(
        url=settings.qdrant.url,
        read_alias=settings.qdrant.read_alias,
        write_collection=settings.qdrant.write_collection,
        api_key=settings.secrets.qdrant_api_key,
        request_timeout_seconds=settings.qdrant.request_timeout_seconds,
        distance=settings.qdrant.distance,
        allow_local_bootstrap=settings.qdrant.allow_local_bootstrap,
    )


def _project_embedding(settings: Settings) -> EmbeddingConfig:
    return EmbeddingConfig(
        model_id=settings.rag.embedding.model_id,
        revision=settings.rag.embedding.revision,
        vector_size=settings.rag.embedding.vector_size,
        batch_size=settings.rag.ingestion.embedding_batch_size,
        device=settings.rag.embedding.device,
        sparse_enabled=settings.rag.embedding.sparse_enabled,
        sparse_vocabulary_size=settings.rag.embedding.sparse_vocabulary_size,
    )


def project_api(settings: Settings) -> ApiRuntimeConfig:
    """Project validated settings onto what the API process consumes."""

    return ApiRuntimeConfig(
        observability=project_observability(settings),
        deployment_scope=settings.app.deployment_scope,
        log_level=settings.app.log_level,
        host=settings.api.host,
        port=settings.api.port,
        request_timeout_seconds=settings.api.request_timeout_seconds,
        shutdown_grace_seconds=settings.api.shutdown_grace_seconds,
        sse_heartbeat_seconds=settings.api.sse_heartbeat_seconds,
        max_control_request_body_bytes=settings.api.max_control_request_body_bytes,
        database=DatabaseConfig(
            dsn=settings.database.dsn,
            application_name=settings.database.application_name,
            statement_timeout_ms=settings.database.statement_timeout_ms,
            pool_size=settings.database.query_pool_size,
            max_overflow=settings.database.query_pool_max_overflow,
        ),
        artifacts=ArtifactStoreConfig(
            backend=settings.artifact_store.backend,
            local_root=settings.artifact_store.local_root,
            max_artifact_bytes=settings.artifact_store.max_artifact_bytes,
        ),
        model=ModelConfig(
            provider=settings.model.provider,
            base_url=settings.model.base_url,
            api_key=settings.secrets.deepseek_api_key,
            profiles={
                name: ModelProfileConfig(
                    model_id=profile.model_id,
                    temperature=profile.temperature,
                    max_output_tokens=profile.max_output_tokens,
                    timeout_seconds=float(profile.timeout_seconds),
                    max_retries=profile.max_retries,
                    tool_calling_required=profile.tool_calling_required,
                )
                for name, profile in (
                    ("main", settings.model.main),
                    ("compact", settings.model.compact),
                )
            },
        ),
        event_stream=EventStreamConfig(
            replay_page_size=settings.event_stream.replay_page_size,
            catchup_poll_seconds=settings.event_stream.catchup_poll_seconds,
        ),
        qdrant=_project_qdrant(settings),
        embedding=_project_embedding(settings),
        reranker=RerankerConfig(
            model_id=settings.rag.reranker.model_id,
            revision=settings.rag.reranker.revision,
            batch_size=settings.rag.reranker.batch_size,
            device=settings.rag.reranker.device,
            timeout_seconds=float(settings.rag.reranker.timeout_seconds),
        ),
        retrieval=RetrievalConfig(
            chunk_size_tokens=settings.rag.ingestion.chunk_size_tokens,
            chunk_overlap_tokens=settings.rag.ingestion.chunk_overlap_tokens,
            answer_context_k=settings.rag.retrieval.answer_context_k,
        ),
        chat=ChatConfig(
            retrieval_shape=settings.chat.retrieval_shape,
            max_agentic_steps=settings.chat.max_agentic_steps,
            max_agentic_searches=settings.chat.max_agentic_searches,
        ),
        chat_recovery=ChatRecoveryConfig(
            orphan_grace_seconds=settings.chat.orphan_grace_seconds,
            reaper_poll_seconds=settings.chat.reaper_poll_seconds,
            reaper_batch_size=settings.chat.reaper_batch_size,
            disconnect_poll_seconds=settings.chat.disconnect_poll_seconds,
        ),
        task=project_task(settings),
    )


__all__ = [
    "AgentRuntimeConfig",
    "ApiRuntimeConfig",
    "ArtifactStoreConfig",
    "ChatConfig",
    "ChatRecoveryConfig",
    "DatabaseConfig",
    "EmbeddingConfig",
    "EventStreamConfig",
    "IngestionConfig",
    "IngestionWorkerRuntimeConfig",
    "ModelConfig",
    "ModelProfileConfig",
    "QdrantConfig",
    "RerankerConfig",
    "RetrievalConfig",
    "TaskConfig",
    "TaskWorkerRuntimeConfig",
    "project_api",
    "project_ingestion_worker",
    "project_task",
    "project_task_worker",
]
