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

from agent_workbench.bootstrap.settings import Settings


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """What it takes to build the ordinary query engine."""

    dsn: SecretStr
    application_name: str
    statement_timeout_ms: int
    pool_size: int
    max_overflow: int


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
    write_collection: str
    api_key: SecretStr | None
    request_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """Which model turns text into vectors, and how wide they are."""

    model_id: str
    revision: str
    vector_size: int
    batch_size: int
    device: str


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
    retrieval: RetrievalConfig
    chat_recovery: ChatRecoveryConfig


def project_api(settings: Settings) -> ApiRuntimeConfig:
    """Project validated settings onto what the API process consumes."""

    return ApiRuntimeConfig(
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
        qdrant=QdrantConfig(
            url=settings.qdrant.url,
            write_collection=settings.qdrant.write_collection,
            api_key=settings.secrets.qdrant_api_key,
            request_timeout_seconds=settings.qdrant.request_timeout_seconds,
        ),
        embedding=EmbeddingConfig(
            model_id=settings.rag.embedding.model_id,
            revision=settings.rag.embedding.revision,
            vector_size=settings.rag.embedding.vector_size,
            batch_size=settings.rag.ingestion.embedding_batch_size,
            device=settings.rag.embedding.device,
        ),
        retrieval=RetrievalConfig(
            chunk_size_tokens=settings.rag.ingestion.chunk_size_tokens,
            chunk_overlap_tokens=settings.rag.ingestion.chunk_overlap_tokens,
            answer_context_k=settings.rag.retrieval.answer_context_k,
        ),
        chat_recovery=ChatRecoveryConfig(
            orphan_grace_seconds=settings.chat.orphan_grace_seconds,
            reaper_poll_seconds=settings.chat.reaper_poll_seconds,
            reaper_batch_size=settings.chat.reaper_batch_size,
            disconnect_poll_seconds=settings.chat.disconnect_poll_seconds,
        ),
    )


__all__ = [
    "ApiRuntimeConfig",
    "ArtifactStoreConfig",
    "ChatRecoveryConfig",
    "DatabaseConfig",
    "EmbeddingConfig",
    "EventStreamConfig",
    "ModelConfig",
    "ModelProfileConfig",
    "QdrantConfig",
    "RetrievalConfig",
    "project_api",
]
