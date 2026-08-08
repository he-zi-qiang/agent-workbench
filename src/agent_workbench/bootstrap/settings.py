"""Typed, immutable configuration for Agent Workbench.

Source priority, from highest to lowest:

1. Explicit init values (tests only)
2. Process environment variables
3. Mounted secret files
4. ``.env`` (development/test only)
5. Optional TOML overlay
6. ``config.default.toml``

The module deliberately turns architectural boundaries into validation rules.
An unsafe combination fails during process bootstrap instead of degrading at
runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import warnings
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    NestedSecretsSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from agent_workbench.bootstrap.network import is_loopback_bind_address
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE

CONTROL_ENV_VARS = {
    "AW_CONFIG_FILE",
    "AW_ENV_FILE",
    "AW_SECRETS_DIR",
}
FORBIDDEN_TOML_PATHS = {
    ("database", "dsn"),
    ("database", "guard_dsn"),
    ("database", "listen_dsn"),
}
PLACEHOLDER_PREFIXES = (
    "not-configured",
    "replace-me",
    "replace-with",
    "fake-",
)
CANONICAL_FAILPOINTS = frozenset(
    {
        "after_claim_commit_before_advisory_lock",
        "after_node_before_checkpoint",
        "inside_checkpoint_put",
        "after_graph_complete_before_registry_commit",
    }
)
SECRET_LIKE_ENV_KEYS = frozenset(
    {
        "AW_DATABASE__DSN",
        "AW_DATABASE__GUARD_DSN",
        "AW_DATABASE__LISTEN_DSN",
        "AW_SECRETS__DEEPSEEK_API_KEY",
        "AW_SECRETS__QDRANT_API_KEY",
        "AW_SECRETS__ARTIFACT_ACCESS_KEY",
        "AW_SECRETS__ARTIFACT_SECRET_KEY",
        "AW_SECRETS__LANGFUSE_PUBLIC_KEY",
        "AW_SECRETS__LANGFUSE_SECRET_KEY",
        "AW_SECRETS__OTEL_EXPORTER_HEADERS",
    }
)
MAX_SINGLE_SECRET_FILE_BYTES = 65_536
MIN_PYDANTIC_SETTINGS_VERSION = (2, 14, 2)
MAX_PYDANTIC_SETTINGS_VERSION = (3, 0, 0)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LOCAL_QDRANT_HOSTS = LOOPBACK_HOSTS | frozenset({"qdrant"})


def _validate_service_endpoint(
    value: str,
    *,
    field_name: str,
    allow_empty: bool = False,
) -> str:
    """Accept a service URL only when credentials cannot be embedded in it."""

    if not value:
        if allow_empty:
            return value
        raise ValueError(f"{field_name} must not be empty")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field_name} contains invalid whitespace")

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid service URL") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError(f"{field_name} must be an HTTP(S) service URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not embed userinfo credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(
            f"{field_name} must not contain query credentials or a fragment"
        )
    return value


class StrictModel(BaseModel):
    """Base class for immutable nested configuration sections."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )


class AppSettings(StrictModel):
    name: str = "agent-workbench"
    environment: Literal["development", "test", "production"] = "development"
    deployment_scope: Literal["local", "remote"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    debug: bool = False
    config_schema_version: Literal["1.6"] = "1.6"
    architecture_baseline: Literal["1.3"] = "1.3"


class ApiSettings(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    request_timeout_seconds: int = Field(default=180, ge=1, le=3600)
    sse_heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    shutdown_grace_seconds: int = Field(default=30, ge=1, le=600)
    max_control_request_body_bytes: int = Field(default=2_097_152, ge=1024)
    document_upload_transport: Literal["artifact_data_plane"] = "artifact_data_plane"

    @field_validator("host")
    @classmethod
    def refuse_reachable_bind_address(cls, value: str) -> str:
        # ADR-012. The only identity resolver that exists reads two request
        # headers, so any interface this binds is an interface on which callers
        # name themselves. The rule is unconditional rather than conditioned on
        # deployment_scope because the scope is a label, and a label is not
        # what decides who resolves identity. When a real identity provider
        # lands, this validator gains a condition; it does not go away.
        if not is_loopback_bind_address(value):
            raise ValueError(
                f"api.host must be a loopback address until the API has a real "
                f"identity provider; {value!r} is reachable from other machines"
            )
        return value


class ChatSettings(StrictModel):
    """Recovery policy for synchronous Chat executions."""

    orphan_grace_seconds: int = Field(default=15, ge=1, le=3600)
    reaper_poll_seconds: int = Field(default=15, ge=1, le=3600)
    reaper_batch_size: int = Field(default=100, ge=1, le=10_000)
    disconnect_poll_seconds: int = Field(default=1, ge=1, le=30)
    orphan_action: Literal["terminal_fail"] = "terminal_fail"
    automatic_retry: Literal[False] = False

    # Which shape answers a turn. `fixed` retrieves once and hands the model the
    # evidence; `agentic` gives it a search tool and lets it decide. The default
    # is the one that can be evaluated: the same question retrieves the same way
    # every time, so a change in an answer is a change in the model or the
    # corpus. Choosing `agentic` buys the capability and spends that property,
    # which is a deployment decision rather than a request parameter.
    #
    # `ungrounded` (ADR-018) is neither: it answers from the model alone, with
    # no retrieval, no citations and no publish fence. Adding it widened a
    # frozen single-set Literal, which is why it took an ADR and a config
    # schema bump rather than one more value -- the same rule that refused
    # `runtime.executor = "fake"` in PR-055. It carries its own durable event
    # so an audit log can never mistake it for a verified answer.
    retrieval_shape: Literal["fixed", "agentic", "ungrounded", "routed"] = "fixed"
    # Only read when the shape is `agentic`. Ceilings rather than targets: the
    # model stops when it has enough, and these stop it when it does not.
    max_agentic_steps: int = Field(default=4, ge=2, le=32)
    max_agentic_searches: int = Field(default=6, ge=1, le=16)
    # Only read when the shape is `routed`. The cross-encoder relevance score
    # the retrieved evidence must reach before the turn is answered *from* that
    # evidence; below it, the turn answers from the model instead.
    #
    # A cross-encoder score, deliberately, and not a retrieval score. RRF is a
    # rank sum -- the top hit of a completely unrelated question still scores
    # near the maximum -- so a gate built on it would send every question down
    # the grounded path. That is not hypothetical: it is what the first version
    # of this shape did, and it made `routed` behave exactly like `fixed`.
    #
    # The default is a starting point, not a calibrated value. BGE reranker
    # scores are unbounded logits and their useful cut depends on the corpus,
    # so a deployment should measure its own before trusting this one.
    routed_relevance_threshold: float = 0.0

    @model_validator(mode="after")
    def validate_agentic_budget(self) -> ChatSettings:
        """Refuse a pair ``RunBudget`` would reject, at startup rather than later.

        A run may spend a tool call on every step, so a search ceiling below the
        step ceiling is a budget that cannot be built. Caught here because the
        alternative is a process that starts, serves fixed turns, and fails only
        once somebody switches the shape.
        """

        if self.max_agentic_searches < self.max_agentic_steps:
            raise ValueError(
                "chat.max_agentic_searches must be >= chat.max_agentic_steps"
            )
        return self


class DatabaseSettings(StrictModel):
    # DSNs are SecretStr because passwords are commonly embedded in them.
    dsn: SecretStr
    guard_dsn: SecretStr
    listen_dsn: SecretStr

    query_pool_mode: Literal["direct", "session", "transaction"] = "direct"
    guard_pool_mode: Literal["direct", "session"] = "direct"
    listen_pool_mode: Literal["direct", "session"] = "direct"

    query_pool_size: int = Field(default=10, ge=1, le=200)
    query_pool_max_overflow: int = Field(default=10, ge=0, le=200)
    guard_connection_budget: int = Field(default=4, ge=1, le=200)
    listener_connections_per_process: Literal[1] = 1
    operational_connection_reserve: int = Field(default=10, ge=1)
    statement_timeout_ms: int = Field(default=30_000, ge=1000)
    application_name: str = "agent-workbench"

    # These are architecture declarations, not freely switchable behavior.
    guard_connection_scope: Literal["task_pinned"] = "task_pinned"
    listen_connection_scope: Literal["process_pinned"] = "process_pinned"
    guard_disconnect_action: Literal["cancel_and_reclaim"] = "cancel_and_reclaim"
    guard_healthcheck_seconds: int = Field(default=5, ge=1, le=60)
    listener_healthcheck_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("dsn", "guard_dsn", "listen_dsn")
    @classmethod
    def validate_postgresql_dsn(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        allowed_prefixes = ("postgresql://", "postgresql+asyncpg://")
        if not raw.startswith(allowed_prefixes):
            raise ValueError("must be a PostgreSQL DSN")
        return value


class CoordinationSettings(StrictModel):
    registry_backend: Literal["postgresql"] = "postgresql"
    claim_strategy: Literal["skip_locked"] = "skip_locked"
    # The registry/checkpointer have no lease epoch or fencing yet. Until
    # those coordination semantics are assembled, one Worker is the only
    # supported execution topology.
    worker_concurrency: int = Field(default=1, ge=1, le=200)
    claim_batch_size: int = Field(default=1, ge=1, le=200)
    claim_poll_interval_ms: int = Field(default=1000, ge=1)
    claim_poll_jitter_ms: int = Field(default=250, ge=0)

    lease_duration_seconds: int = Field(default=90, ge=10)
    heartbeat_interval_seconds: int = Field(default=20, ge=1)
    lease_grace_seconds: int = Field(default=10, ge=0)
    max_missed_heartbeats: int = Field(default=2, ge=0, le=10)
    recovery_poll_seconds: int = Field(default=15, ge=1)

    priority_aging_seconds: int = Field(default=300, ge=0)
    max_attempts: int = Field(default=5, ge=1, le=100)
    retry_base_seconds: int = Field(default=2, ge=1)
    retry_max_seconds: int = Field(default=60, ge=1)

    advisory_lock_enabled: Literal[True] = True
    require_same_physical_session: Literal[True] = True
    fenced_checkpointer_enabled: Literal[True] = True
    tool_execution_ledger_enabled: Literal[True] = True
    strict_fifo_required: Literal[False] = False

    lease_time_source: Literal["postgresql_clock"] = "postgresql_clock"
    fencing_token_strategy: Literal["monotonic_lease_epoch"] = "monotonic_lease_epoch"
    advisory_lock_key_strategy: Literal["stable_signed_int64"] = "stable_signed_int64"
    heartbeat_execution: Literal["independent_task"] = "independent_task"

    @model_validator(mode="after")
    def validate_timing(self) -> CoordinationSettings:
        safety_floor = (
            self.heartbeat_interval_seconds * (self.max_missed_heartbeats + 1)
            + self.lease_grace_seconds
        )
        if self.lease_duration_seconds <= safety_floor:
            raise ValueError(
                "lease_duration_seconds must be greater than "
                "heartbeat_interval_seconds * (max_missed_heartbeats + 1) "
                "+ lease_grace_seconds"
            )
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds must be >= retry_base_seconds")
        return self


class EventStreamSettings(StrictModel):
    store_backend: Literal["postgresql"] = "postgresql"
    wakeup_backend: Literal["postgres_listen_notify"] = "postgres_listen_notify"
    replay_source: Literal["run_events"] = "run_events"
    notify_payload_mode: Literal["cursor_only"] = "cursor_only"
    notify_payload_limit_bytes: int = Field(default=7900, ge=1, lt=8000)
    listener_uses_dedicated_session: Literal[True] = True
    task_ready_channel: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    stream_ready_channel: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    replay_page_size: int = Field(default=500, ge=1, le=10_000)
    subscriber_buffer_events: int = Field(default=256, ge=1)
    catchup_poll_seconds: int = Field(default=10, ge=1)
    model_delta_mode: Literal["ephemeral_sse_coalesced"] = "ephemeral_sse_coalesced"
    live_delta_coalesce_ms: int = Field(default=50, ge=1, le=1000)


class ModelProfileSettings(StrictModel):
    model_id: str = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=8192, ge=1)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_retries: int = Field(default=2, ge=0, le=10)
    tool_calling_required: bool = False
    prompt_cache_enabled: bool = True


class ModelSettings(StrictModel):
    # ModelPort stays provider-neutral, but only a provider with a shipped
    # adapter may be configured: a provider string the process cannot start on
    # is a deployment that fails at the first model call instead of at boot.
    # "fake" exists solely for deterministic tests.
    provider: Literal["deepseek", "fake"] = "deepseek"
    # The API endpoint, not the model. DeepSeek speaks an OpenAI-compatible
    # protocol, so this also covers a compatible gateway or a local server.
    base_url: str = "https://api.deepseek.com"
    main: ModelProfileSettings
    compact: ModelProfileSettings

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return _validate_service_endpoint(value, field_name="model.base_url")


class RuntimeSettings(StrictModel):
    executor: Literal["claude_like"] = "claude_like"
    max_steps: int = Field(default=12, ge=1, le=100)
    max_tool_calls: int = Field(default=32, ge=1, le=500)
    max_parallel_read_tools: int = Field(default=4, ge=1, le=64)
    max_parallel_write_tools: Literal[1] = 1
    model_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    tool_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    cancellation_poll_seconds: int = Field(default=1, ge=1, le=30)
    context_soft_limit_ratio: float = Field(default=0.75, gt=0.0, lt=1.0)
    tool_result_artifact_threshold_bytes: int = Field(default=65_536, ge=1024)
    write_tools_default_enabled: Literal[False] = False
    # ADR-019. Records the prompt and the proposed tool arguments on the run's
    # own event stream, where they are readable only by the principal that owns
    # the Task or Session. Distinct from `observability.record_prompt_body`,
    # which stays pinned False: that one governs export to an OTel collector,
    # which has no tenant boundary. Default off, because turning it on changes
    # what a deployment stores about its users and that is not an upgrade's
    # decision to make.
    record_step_inputs: bool = False

    # No cross-field budget check. `max_tool_calls` below `max_steps` is a
    # legitimate deployment budget under ADR-022 -- "this many tool calls, plus
    # a turn to answer from them" -- and the runtime enforces it by taking the
    # tools off the request once the allowance is spent rather than by ending
    # the run. RunBudget carries the full reasoning.


class LangChainAdapterSettings(StrictModel):
    enabled: bool = True
    allowed_scope: Literal["model_and_tool_adapters"] = "model_and_tool_adapters"
    agent_executor_enabled: Literal[False] = False
    memory_enabled: Literal[False] = False


class WorkflowSettings(StrictModel):
    control_plane: Literal["langgraph"] = "langgraph"
    checkpointer_backend: Literal["postgresql"] = "postgresql"
    checkpointing_enabled: Literal[True] = True
    human_interrupt_enabled: bool = True
    interrupt_boundary: Literal["graph_node"] = "graph_node"
    runtime_loop_owner: Literal["custom_runtime"] = "custom_runtime"
    graph_version: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9._-]+$")
    node_retry_max_attempts: int = Field(default=2, ge=0, le=20)
    node_timeout_seconds: int = Field(default=600, ge=1, le=86_400)


class MultiAgentSettings(StrictModel):
    enabled: bool = True
    backend: Literal["custom_supervisor_workers"] = "custom_supervisor_workers"
    topology: Literal["fixed_langgraph"] = "fixed_langgraph"
    worker_executor: Literal["custom_runtime"] = "custom_runtime"
    static_agent_node_limit: int = Field(default=6, ge=1, le=32)
    max_parallel_agent_invocations: int = Field(default=3, ge=1, le=32)
    max_agent_invocation_attempts_per_task: int = Field(
        default=12,
        ge=1,
        le=100,
    )
    max_tokens_per_agent_invocation: int = Field(default=16_000, ge=256)

    @model_validator(mode="after")
    def validate_agent_budget(self) -> MultiAgentSettings:
        if (
            self.max_parallel_agent_invocations
            > self.max_agent_invocation_attempts_per_task
        ):
            raise ValueError(
                "max_parallel_agent_invocations must be <= "
                "max_agent_invocation_attempts_per_task"
            )
        return self


class LlamaIndexSettings(StrictModel):
    # False until ADR-017 step 2 produces evidence, which it has not. The
    # adapter exists, is contract-tested against both paths on real Qdrant and
    # real PostgreSQL, and is one setting away -- but the equivalence
    # evaluation that step 3 requires before traffic moves came back
    # inconclusive, and inconclusive is not a reason to switch.
    #
    # Not inconclusive because the two paths disagreed: because the measurement
    # cannot resolve them. Tied fused scores come back from Qdrant in an
    # unstable order, so each retriever disagrees with *itself* on 9-10 of 38
    # gold questions -- a noise floor wider than any difference between the
    # two. See docs/status.md 2026-08-03.
    enabled: bool = False
    role: Literal["ingestion_and_retrieval_adapter"] = "ingestion_and_retrieval_adapter"
    agent_executor_enabled: Literal[False] = False
    query_engine_generates_final_answer: Literal[False] = False
    fusion_enabled: Literal[False] = False


class IngestionSettings(StrictModel):
    chunk_size_tokens: int = Field(default=512, ge=64, le=4096)
    chunk_overlap_tokens: int = Field(default=64, ge=0)
    embedding_batch_size: int = Field(default=16, ge=1, le=1024)
    upsert_batch_size: int = Field(default=64, ge=1, le=4096)
    document_versioning_enabled: Literal[True] = True
    outbox_required: Literal[True] = True
    parser_version: str = Field(min_length=1)
    chunker_version: str = Field(min_length=1)
    index_schema_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_chunks(self) -> IngestionSettings:
        if self.chunk_overlap_tokens >= self.chunk_size_tokens:
            raise ValueError("chunk_overlap_tokens must be < chunk_size_tokens")
        return self


class EmbeddingSettings(StrictModel):
    model_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    vector_size: int = Field(default=1024, ge=1)
    max_input_tokens: int = Field(default=8192, ge=1)
    device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    precision: Literal["float32", "float16", "bfloat16"] = "float16"
    dense_enabled: Literal[True] = True
    sparse_enabled: Literal[True] = True
    sparse_vocabulary_size: int = Field(default=250_002, ge=1)
    dense_vector_name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    sparse_vector_name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")

    @field_validator("revision")
    @classmethod
    def normalize_revision(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("revision must not be blank")
        if re.fullmatch(r"[0-9a-fA-F]{40}", cleaned):
            return cleaned.lower()
        return cleaned

    @model_validator(mode="after")
    def validate_vector_names(self) -> EmbeddingSettings:
        if self.dense_vector_name == self.sparse_vector_name:
            raise ValueError("dense and sparse vector names must be different")
        return self


class RetrievalSettings(StrictModel):
    fusion_owner: Literal["qdrant"] = "qdrant"
    fusion_method: Literal["rrf"] = "rrf"
    dense_top_k: int = Field(default=40, ge=1, le=1000)
    sparse_top_k: int = Field(default=40, ge=1, le=1000)
    fused_top_k: int = Field(default=40, ge=1, le=1000)
    rerank_top_k: int = Field(default=8, ge=1, le=1000)
    answer_context_k: int = Field(default=8, ge=1, le=1000)
    metadata_filter_required: Literal[True] = True
    acl_filter_required: Literal[True] = True
    citations_required: Literal[True] = True

    @model_validator(mode="after")
    def validate_candidate_funnel(self) -> RetrievalSettings:
        if self.fused_top_k > self.dense_top_k + self.sparse_top_k:
            raise ValueError("fused_top_k must be <= dense_top_k + sparse_top_k")
        if self.rerank_top_k > self.fused_top_k:
            raise ValueError("rerank_top_k must be <= fused_top_k")
        if self.answer_context_k > self.rerank_top_k:
            raise ValueError("answer_context_k must be <= rerank_top_k")
        return self


class RerankerSettings(StrictModel):
    enabled: Literal[True] = True
    model_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    batch_size: int = Field(default=8, ge=1, le=1024)
    device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    precision: Literal["float32", "float16", "bfloat16"] = "float16"
    timeout_seconds: int = Field(default=15, ge=1, le=600)
    failure_mode: Literal["fail_open_with_fused_results"] = (
        "fail_open_with_fused_results"
    )

    @field_validator("revision")
    @classmethod
    def normalize_revision(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("revision must not be blank")
        if re.fullmatch(r"[0-9a-fA-F]{40}", cleaned):
            return cleaned.lower()
        return cleaned


class RagSettings(StrictModel):
    llama_index: LlamaIndexSettings
    ingestion: IngestionSettings
    embedding: EmbeddingSettings
    retrieval: RetrievalSettings
    reranker: RerankerSettings

    @model_validator(mode="after")
    def validate_index_versions(self) -> RagSettings:
        if self.ingestion.chunk_size_tokens > self.embedding.max_input_tokens:
            raise ValueError(
                "chunk_size_tokens must not exceed embedding max_input_tokens"
            )
        return self


class QdrantSettings(StrictModel):
    url: str = Field(min_length=1)
    read_alias: str = Field(min_length=1)
    write_collection: str = Field(min_length=1)
    collection_schema_version: int = Field(default=1, ge=1)
    request_timeout_seconds: int = Field(default=10, ge=1, le=600)
    prefer_grpc: bool = True
    distance: Literal["cosine"] = "cosine"
    derived_index: Literal[True] = True
    payload_acl_filter_required: Literal[True] = True
    api_key_required: bool = False
    # Creation/alias correction is an explicit local development/test action,
    # never an inference from a missing remote collection.
    allow_local_bootstrap: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_service_endpoint(value, field_name="qdrant.url")

    @model_validator(mode="after")
    def validate_alias_strategy(self) -> QdrantSettings:
        if self.read_alias == self.write_collection:
            raise ValueError(
                "read_alias and write_collection must differ to support "
                "versioned rebuild and atomic alias switch"
            )
        return self


class ArtifactStoreSettings(StrictModel):
    backend: Literal["local", "s3"] = "local"
    local_root: str = "./var/artifacts"
    bucket: str = ""
    endpoint: str = ""
    region: str = ""
    max_artifact_bytes: int = Field(default=104_857_600, ge=1024)
    presigned_url_ttl_seconds: int = Field(default=300, ge=1, le=86_400)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return _validate_service_endpoint(
            value,
            field_name="artifact_store.endpoint",
            allow_empty=True,
        )


class PolicySettings(StrictModel):
    revision: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9._-]+$")
    default_effect: Literal["deny"] = "deny"
    write_tools_require_approval: Literal[True] = True
    network_tools_require_allowlist: Literal[True] = True
    shell_tools_enabled: Literal[False] = False
    path_sandbox_enabled: Literal[True] = True
    tenant_filter_required: Literal[True] = True
    max_tool_argument_bytes: int = Field(default=65_536, ge=1024)
    max_tool_result_bytes: int = Field(default=10_485_760, ge=1024)
    redact_secrets: Literal[True] = True


class ObservabilitySettings(StrictModel):
    otel_enabled: Literal[True] = True
    otel_service_name: str = Field(min_length=1)
    otel_exporter_endpoint: str = Field(min_length=1)
    trace_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    record_prompt_body: Literal[False] = False
    record_tool_result_body: Literal[False] = False
    metrics_enabled: Literal[True] = True
    langfuse_enabled: bool = False

    @field_validator("otel_exporter_endpoint")
    @classmethod
    def validate_otel_exporter_endpoint(cls, value: str) -> str:
        return _validate_service_endpoint(
            value,
            field_name="observability.otel_exporter_endpoint",
        )


class EvaluationJudgeSettings(StrictModel):
    enabled: bool = False
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=0.0)
    calibration_set_path: str = Field(min_length=1)


class EvaluationSettings(StrictModel):
    ragas_enabled: bool = True
    ragas_offline_only: Literal[True] = True
    online_judge_in_ci: Literal[False] = False
    benchmark_isolated_process: Literal[True] = True
    rag_gold_set_path: str = Field(min_length=1)
    task_benchmark_path: str = Field(min_length=1)
    rag_metrics: tuple[str, ...]
    task_metrics: tuple[str, ...]
    multi_agent_metrics: tuple[str, ...]
    judge: EvaluationJudgeSettings


class TestingSettings(StrictModel):
    failpoints_enabled: bool = False
    allow_fault_injection: bool = False
    allowed_failpoints: tuple[str, ...] = ()
    deterministic_concurrency_required: Literal[True] = True
    real_postgresql_required: Literal[True] = True
    sleep_based_race_tests_forbidden: Literal[True] = True
    controllable_clock_enabled: Literal[True] = True

    @field_validator("allowed_failpoints")
    @classmethod
    def validate_failpoint_names(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        unknown = sorted(set(values) - CANONICAL_FAILPOINTS)
        if unknown:
            raise ValueError("unknown failpoint names: " + ", ".join(unknown))
        if len(values) != len(set(values)):
            raise ValueError("allowed_failpoints must not contain duplicates")
        return values


class OptionalLabsSettings(StrictModel):
    runtime_mid_loop_resume: bool = False
    dynamic_agent_spawn: bool = False
    agent_mailbox: bool = False
    crewai_benchmark: bool = False
    mcp_adapter: bool = False
    langfuse_profile: bool = False
    redis_streams_coordination: bool = False
    advanced_compaction: bool = False


class ResearchSettings(StrictModel):
    """External web search (ADR-020).

    Off by default, and the default is load-bearing rather than cautious: it is
    what keeps a deployment that never configured it from widening its Task
    authorization envelope on upgrade. See
    ``projections.task_authorization_envelope``.

    No API key of its own: the search runs on the model provider's side through
    its Anthropic-compatible endpoint, under the same key the provider already
    holds.
    """

    enabled: bool = False
    provider: Literal["deepseek"] = "deepseek"
    #: The provider's Anthropic-compatible endpoint. Separate from
    #: `model.base_url`, which addresses the OpenAI-compatible one the runtime
    #: uses for every other call -- the two are different paths on the same
    #: service and only one of them speaks the Messages protocol.
    base_url: str = Field(default="https://api.deepseek.com/anthropic", min_length=1)
    model_id: str = Field(default="deepseek-chat", min_length=1)
    #: Searches per external_search call. The model may search more than once
    #: for one query, and each search is work the provider bills for.
    max_uses: int = Field(default=5, ge=1, le=20)
    timeout_seconds: int = Field(default=60, ge=1, le=600)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        # Every request to it carries the provider API key, so the same rule the
        # model endpoint follows applies here.
        return _validate_service_endpoint(value, field_name="research.base_url")


class SecretsSettings(StrictModel):
    deepseek_api_key: SecretStr | None = None
    qdrant_api_key: SecretStr | None = None
    artifact_access_key: SecretStr | None = None
    artifact_secret_key: SecretStr | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    otel_exporter_headers: SecretStr | None = None


class Settings(BaseSettings):
    """Root settings object. Create it through :func:`load_settings`."""

    app: AppSettings
    api: ApiSettings
    chat: ChatSettings
    database: DatabaseSettings
    coordination: CoordinationSettings
    event_stream: EventStreamSettings
    model: ModelSettings
    runtime: RuntimeSettings
    langchain_adapter: LangChainAdapterSettings
    workflow: WorkflowSettings
    multi_agent: MultiAgentSettings
    rag: RagSettings
    qdrant: QdrantSettings
    artifact_store: ArtifactStoreSettings
    policy: PolicySettings
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    observability: ObservabilitySettings
    evaluation: EvaluationSettings
    testing: TestingSettings
    optional_labs: OptionalLabsSettings
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)

    # Some pydantic-settings source keys are consumed at runtime before they
    # appear in the package's SettingsConfigDict type surface. Keep the cast
    # local to this third-party typing boundary.
    model_config = cast(
        SettingsConfigDict,
        {
            "env_prefix": "AW_",
            "env_nested_delimiter": "__",
            "env_ignore_empty": True,
            "env_file": None,
            "secrets_dir": None,
            # GHSA-4xgf-cpjx-pc3j affected nested-subdirectory traversal before
            # pydantic-settings 2.14.2. This project uses flat "__" filenames.
            "secrets_nested_subdir": False,
            "secrets_dir_max_size": 1_048_576,
            "secrets_dir_missing": "ok",
            "toml_file": str(DEFAULT_CONFIG_FILE),
            "extra": "forbid",
            "frozen": True,
            "validate_default": True,
            "hide_input_in_errors": True,
            "case_sensitive": False,
        },
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Keep direct Settings(...) construction fail-closed too; load_settings
        # performs the same check even earlier in the supported bootstrap path.
        _assert_safe_pydantic_settings_version()
        # Earlier sources have higher priority.
        return (
            init_settings,
            env_settings,
            NestedSecretsSettingsSource(file_secret_settings),
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, deep_merge=True),
        )

    @model_validator(mode="after")
    def validate_architecture_and_environment(self) -> Settings:
        if self.coordination.worker_concurrency > (
            self.database.guard_connection_budget
        ):
            raise ValueError(
                "worker_concurrency must not exceed guard_connection_budget"
            )
        if self.coordination.claim_batch_size > min(
            self.coordination.worker_concurrency,
            self.database.guard_connection_budget,
        ):
            raise ValueError(
                "claim_batch_size must not exceed worker concurrency or "
                "guard connection budget"
            )
        if (
            self.database.guard_healthcheck_seconds
            > self.coordination.heartbeat_interval_seconds
        ):
            raise ValueError("guard_healthcheck_seconds must be <= heartbeat interval")

        fault_injection_requested = (
            self.testing.failpoints_enabled
            or self.testing.allow_fault_injection
            or bool(self.testing.allowed_failpoints)
        )
        if fault_injection_requested:
            if self.app.environment != "test":
                raise ValueError(
                    "fault injection is allowed only when environment=test"
                )
            if not (
                self.testing.failpoints_enabled
                and self.testing.allow_fault_injection
                and self.testing.allowed_failpoints
            ):
                raise ValueError(
                    "fault injection requires failpoints_enabled, "
                    "allow_fault_injection and a non-empty failpoint list"
                )

        if self.model.provider == "fake" and self.app.environment != "test":
            raise ValueError("the fake model provider is test-only")

        if self.observability.langfuse_enabled != (self.optional_labs.langfuse_profile):
            raise ValueError(
                "langfuse_enabled and optional_labs.langfuse_profile "
                "must be enabled or disabled together"
            )

        if self.optional_labs.crewai_benchmark:
            if self.app.environment != "test":
                raise ValueError("CrewAI benchmark is test-only")
            if not self.evaluation.benchmark_isolated_process:
                raise ValueError(
                    "CrewAI benchmark must run in an isolated benchmark process"
                )

        if (
            self.app.environment == "production"
            and self.app.deployment_scope != "remote"
        ):
            raise ValueError("production requires app.deployment_scope=remote")

        model_endpoint = urlsplit(self.model.base_url)
        if (
            model_endpoint.scheme.lower() != "https"
            and model_endpoint.hostname not in LOOPBACK_HOSTS
        ):
            # Every request to this endpoint carries the provider API key.
            raise ValueError(
                "model.base_url must use HTTPS unless it is a loopback address"
            )

        if self.qdrant.api_key_required and not self._secret_is_configured(
            self.secrets.qdrant_api_key
        ):
            raise ValueError("Qdrant API key is required but not configured")

        # Checked in every environment, not only production: enabled-without-a-key
        # reads as working web search in the config file and fails closed at the
        # first search. That is the "configuration describes a system that does
        # not exist" defect this project keeps removing, so it is a startup error.
        if self.research.enabled and not self._secret_is_configured(
            self.secrets.deepseek_api_key
        ):
            raise ValueError(
                "research.enabled requires a non-placeholder provider API key"
            )

        if self.app.deployment_scope == "remote":
            if not self.qdrant.api_key_required:
                raise ValueError(
                    "remote deployment requires qdrant.api_key_required=true"
                )
            if urlsplit(self.qdrant.url).scheme.lower() != "https":
                raise ValueError("remote deployment requires a Qdrant HTTPS URL")
            if self.qdrant.allow_local_bootstrap:
                raise ValueError(
                    "remote deployment forbids qdrant.allow_local_bootstrap"
                )
        elif urlsplit(self.qdrant.url).hostname not in LOCAL_QDRANT_HOSTS:
            raise ValueError(
                "local deployment scope requires a local/Compose Qdrant host"
            )

        if self.artifact_store.backend == "s3":
            if not self.artifact_store.bucket or not self.artifact_store.endpoint:
                raise ValueError("S3 artifact store requires both bucket and endpoint")
            if not (
                self._secret_is_configured(self.secrets.artifact_access_key)
                and self._secret_is_configured(self.secrets.artifact_secret_key)
            ):
                raise ValueError("S3 artifact store credentials are missing")

        if self.observability.langfuse_enabled and not (
            self._secret_is_configured(self.secrets.langfuse_public_key)
            and self._secret_is_configured(self.secrets.langfuse_secret_key)
        ):
            raise ValueError("Langfuse is enabled but its keys are missing")

        if self.evaluation.judge.enabled:
            if self._looks_like_placeholder(self.evaluation.judge.model_id):
                raise ValueError("enabled evaluation judge requires a pinned model_id")
            if self._looks_like_placeholder(self.evaluation.judge.model_revision):
                raise ValueError(
                    "enabled evaluation judge requires a pinned model_revision"
                )

        if self.app.environment == "production":
            self._validate_production()

        return self

    def _validate_production(self) -> None:
        if self.app.debug:
            raise ValueError("debug must be false in production")

        if self.app.deployment_scope != "remote":
            raise ValueError("production requires app.deployment_scope=remote")

        if self.model.provider == "deepseek" and not self._secret_is_configured(
            self.secrets.deepseek_api_key
        ):
            raise ValueError("the DeepSeek provider requires a non-placeholder API key")

        for profile_name, profile in (
            ("main", self.model.main),
            ("compact", self.model.compact),
        ):
            if self._looks_like_placeholder(profile.model_id):
                raise ValueError(
                    f"production model.{profile_name}.model_id must be pinned"
                )

        for name, revision in (
            ("embedding", self.rag.embedding.revision),
            ("reranker", self.rag.reranker.revision),
        ):
            if re.fullmatch(r"[0-9a-fA-F]{40}", revision.strip()) is None:
                raise ValueError(
                    f"production RAG {name} revision must be a full "
                    "40-character hexadecimal commit SHA"
                )

        enabled_labs = [
            name for name, enabled in self.optional_labs.model_dump().items() if enabled
        ]
        if enabled_labs:
            raise ValueError(
                "Optional Labs are disabled in the v1 production baseline: "
                + ", ".join(sorted(enabled_labs))
            )

    @staticmethod
    def _looks_like_placeholder(value: str) -> bool:
        normalized = value.strip().lower()
        return not normalized or normalized.startswith(PLACEHOLDER_PREFIXES)

    @classmethod
    def _secret_is_configured(cls, value: SecretStr | None) -> bool:
        if value is None:
            return False
        return not cls._looks_like_placeholder(value.get_secret_value())

    def public_config(self) -> dict[str, Any]:
        """Return a logging-safe configuration snapshot.

        DSNs, API keys, tokens, credentials and exporter headers are never
        returned. Their presence is represented only as ``<configured>`` or
        ``<unset>``.
        """

        raw = self.model_dump(mode="json")
        return _redact_mapping(raw)

    def fingerprint(self) -> str:
        """Stable fingerprint of the complete non-secret startup config."""

        payload = json.dumps(
            self.public_config(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def run_semantics_snapshot(self) -> dict[str, Any]:
        """Return only deterministic Task semantics for checkpoint resume.

        Live infrastructure, credentials, coordination timing, observability
        and policy are intentionally excluded. Policy is re-evaluated against
        both the submitted authorization envelope and the current policy.
        """

        public = self.public_config()
        qdrant = public["qdrant"]
        model = cast(dict[str, Any], public["model"]).copy()
        # Where a model is reachable is deployment state. Resuming a task must
        # not restore an old endpoint, and moving one must not change what a
        # running task means.
        model.pop("base_url", None)
        return {
            "config_schema_version": self.app.config_schema_version,
            "architecture_baseline": self.app.architecture_baseline,
            "model": model,
            "runtime": public["runtime"],
            "langchain_adapter": public["langchain_adapter"],
            "workflow": public["workflow"],
            "multi_agent": public["multi_agent"],
            "rag": public["rag"],
            "qdrant_index": {
                "collection_schema_version": qdrant["collection_schema_version"],
                "distance": qdrant["distance"],
            },
        }

    def task_run_semantics_snapshot(
        self,
        *,
        resolved_qdrant_collection: str | None = None,
        resolved_qdrant_index_version: str | None = None,
    ) -> dict[str, Any]:
        """Bind Task semantics to a concrete Qdrant index, never an alias."""

        if resolved_qdrant_collection is None and resolved_qdrant_index_version is None:
            return self.run_semantics_snapshot()
        if resolved_qdrant_collection is None or resolved_qdrant_index_version is None:
            raise ValueError(
                "resolved Qdrant collection and index version must be provided together"
            )

        collection = resolved_qdrant_collection.strip()
        index_version = resolved_qdrant_index_version.strip()
        if not collection or not index_version:
            raise ValueError(
                "resolved Qdrant collection and index version are required"
            )
        if collection == self.qdrant.read_alias:
            raise ValueError(
                "resolved Qdrant collection must be a concrete collection, "
                "not the configured read alias"
            )

        snapshot = self.run_semantics_snapshot()
        snapshot["qdrant_index"].update(
            {
                "resolved_collection_name": collection,
                "resolved_index_version": index_version,
            }
        )
        return snapshot

    def run_semantics_fingerprint(self) -> str:
        payload = json.dumps(
            self.run_semantics_snapshot(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def policy_fingerprint(self) -> str:
        """Canonical fingerprint of the validated, non-secret policy rules."""

        policy_rules = self.policy.model_dump(mode="json")
        policy_rules.pop("revision", None)
        payload = json.dumps(
            policy_rules,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def policy_identity(self) -> str:
        """Pair the operator label with a fingerprint that detects stale labels."""

        return f"{self.policy.revision}:{self.policy_fingerprint()[:16]}"

    def revision(self) -> str:
        """Revision of the complete startup configuration."""

        return (
            f"{self.app.config_schema_version}:"
            f"{self.app.architecture_baseline}:"
            f"{self.fingerprint()[:16]}"
        )

    def run_semantics_revision(self) -> str:
        """Revision of the deployment-independent semantics template."""

        return (
            f"{self.app.config_schema_version}:"
            f"{self.app.architecture_baseline}:"
            f"{self.run_semantics_fingerprint()[:16]}"
        )

    def task_run_semantics_revision(
        self,
        *,
        resolved_qdrant_collection: str | None = None,
        resolved_qdrant_index_version: str | None = None,
    ) -> str:
        """Revision persisted with a Task after resolving the Qdrant alias."""

        snapshot = self.task_run_semantics_snapshot(
            resolved_qdrant_collection=resolved_qdrant_collection,
            resolved_qdrant_index_version=resolved_qdrant_index_version,
        )
        payload = json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        fingerprint = hashlib.sha256(payload).hexdigest()
        return (
            f"{self.app.config_schema_version}:"
            f"{self.app.architecture_baseline}:"
            f"{fingerprint[:16]}"
        )


SENSITIVE_KEYS = {
    "dsn",
    "guard_dsn",
    "listen_dsn",
    "deepseek_api_key",
    "qdrant_api_key",
    "artifact_access_key",
    "artifact_secret_key",
    "langfuse_public_key",
    "langfuse_secret_key",
    "otel_exporter_headers",
    "password",
    "credential",
    "credentials",
}
SENSITIVE_KEY_SUFFIXES = (
    "_dsn",
    "api_key",
    "_password",
    "_credential",
    "_credentials",
)


def _redact_mapping(value: Any, key: str = "") -> Any:
    normalized_key = key.lower()
    if normalized_key in SENSITIVE_KEYS or normalized_key.endswith(
        SENSITIVE_KEY_SUFFIXES
    ):
        configured = value not in (None, "", [], {}, ())
        return "<configured>" if configured else "<unset>"
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        return {
            child_key: _redact_mapping(child_value, child_key)
            for child_key, child_value in mapping.items()
        }
    if isinstance(value, list):
        return [_redact_mapping(item) for item in cast(list[Any], value)]
    return value


def _read_control_env(name: str) -> str | None:
    target = name.upper()
    for key, value in os.environ.items():
        if key.upper() == target:
            return value or None
    return None


def _read_mounted_secret_values(
    secrets_dir: Path | None,
) -> dict[str, str]:
    if secrets_dir is None or not secrets_dir.exists():
        return {}
    if not secrets_dir.is_dir():
        raise ValueError(f"secrets_dir is not a directory: {secrets_dir}")

    root = secrets_dir.resolve()
    candidates: dict[str, Path] = {}
    for entry in secrets_dir.iterdir():
        normalized_name = entry.name.upper()
        if normalized_name not in SECRET_LIKE_ENV_KEYS:
            if normalized_name.startswith("AW_"):
                raise ValueError(
                    "unknown or non-leaf Agent Workbench mounted secret "
                    f"filename: {normalized_name}"
                )
            continue
        if normalized_name in candidates:
            raise ValueError(
                "duplicate mounted secret filename (case-insensitive): "
                + normalized_name
            )
        candidates[normalized_name] = entry

    values: dict[str, str] = {}
    for name, entry in candidates.items():
        try:
            resolved = entry.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise ValueError(
                f"mounted secret path escapes secrets_dir or is invalid: {name}"
            ) from exc
        if not resolved.is_file():
            raise ValueError(f"mounted secret is not a regular file: {name}")
        size = resolved.stat().st_size
        if size > MAX_SINGLE_SECRET_FILE_BYTES:
            raise ValueError(f"mounted secret exceeds per-file size limit: {name}")
        values[name] = resolved.read_text(encoding="utf-8").strip()
    return values


def _reject_conflicting_secret_sources(
    secrets_dir: Path | None,
) -> None:
    mounted = _read_mounted_secret_values(secrets_dir)
    for name, file_value in mounted.items():
        env_value = _read_control_env(name)
        if env_value is None:
            continue
        # NestedSecretsSettingsSource strips file text, whereas EnvSettingsSource
        # preserves the process value. Compare exactly as the two sources will
        # be consumed so whitespace cannot disguise a different effective key.
        if env_value != file_value:
            raise ValueError(
                f"secret source conflict for {name}: process environment "
                "and mounted secret file contain different values"
            )
        warnings.warn(
            f"duplicate secret source for {name}: identical values were "
            "provided by process environment and mounted secret file",
            RuntimeWarning,
            stacklevel=2,
        )


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    path = (Path.cwd() / path).resolve() if not path.is_absolute() else path.resolve()
    return path


def _resolve_config_files(config_file: str | Path | None) -> tuple[Path, ...]:
    files = [DEFAULT_CONFIG_FILE]
    selected = config_file or _read_control_env("AW_CONFIG_FILE")
    if selected:
        overlay = _resolve_path(selected)
        if not overlay.is_file():
            raise FileNotFoundError(f"configuration overlay not found: {overlay}")
        if overlay != DEFAULT_CONFIG_FILE:
            files.append(overlay)
    for path in files:
        if not path.is_file():
            raise FileNotFoundError(f"configuration file not found: {path}")
    return tuple(files)


def _declared_environment(config_files: tuple[Path, ...]) -> str:
    environment = "development"
    for path in config_files:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        environment = data.get("app", {}).get("environment", environment)
    env_override = _read_control_env("AW_APP__ENVIRONMENT")
    return env_override or environment


def _reject_sensitive_toml_fields(config_files: tuple[Path, ...]) -> None:
    for path in config_files:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        if "secrets" in data:
            raise ValueError(
                f"{path} must not contain a [secrets] table; inject secrets "
                "through environment variables or mounted secret files"
            )
        for section, field in FORBIDDEN_TOML_PATHS:
            if field in data.get(section, {}):
                raise ValueError(
                    f"{path} must not contain {section}.{field}; DSNs are "
                    "secret-like values and must be injected"
                )


def _allowed_environment_keys() -> set[str]:
    allowed: set[str] = set(CONTROL_ENV_VARS)

    def visit(model_type: type[BaseModel], path: tuple[str, ...]) -> None:
        for name, field in model_type.model_fields.items():
            child_path = (*path, name)
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                visit(annotation, child_path)
            else:
                allowed.add("AW_" + "__".join(child_path).upper())

    visit(Settings, ())
    return allowed


def _reject_unknown_environment_variables() -> None:
    normalized_keys: dict[str, str] = {}
    duplicate_keys: set[str] = set()
    for key in os.environ:
        normalized = key.upper()
        if not normalized.startswith("AW_"):
            continue
        if normalized in normalized_keys and normalized_keys[normalized] != key:
            duplicate_keys.add(normalized)
        normalized_keys[normalized] = key
    if duplicate_keys:
        raise ValueError(
            "duplicate Agent Workbench environment variables after "
            "case normalization: " + ", ".join(sorted(duplicate_keys))
        )

    allowed = _allowed_environment_keys()
    unknown = sorted(
        key
        for key in os.environ
        if key.upper().startswith("AW_") and key.upper() not in allowed
    )
    if unknown:
        raise ValueError(
            "unknown Agent Workbench environment variables: " + ", ".join(unknown)
        )


def _reject_unknown_dotenv_variables(env_file: Path | None) -> None:
    if env_file is None or not env_file.is_file():
        return

    allowed = _allowed_environment_keys()
    normalized_keys: dict[str, str] = {}
    duplicate_keys: set[str] = set()
    unknown_keys: list[str] = []
    for key in dotenv_values(env_file):
        normalized = key.upper()
        if not normalized.startswith("AW_"):
            continue
        if normalized in normalized_keys and normalized_keys[normalized] != key:
            duplicate_keys.add(normalized)
        normalized_keys[normalized] = key
        if normalized not in allowed:
            unknown_keys.append(key)

    if duplicate_keys:
        raise ValueError(
            "duplicate Agent Workbench dotenv variables after case "
            "normalization: " + ", ".join(sorted(duplicate_keys))
        )
    if unknown_keys:
        raise ValueError(
            "unknown or non-leaf Agent Workbench dotenv variables: "
            + ", ".join(sorted(unknown_keys))
        )


def _parse_release_version(raw_version: str) -> tuple[int, int, int]:
    # Pre-releases such as 2.14.2rc1 may predate the security fix and are not
    # accepted merely because their numeric release tuple looks sufficient.
    match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)(?:\.post\d+)?",
        raw_version,
    )
    if match is None:
        raise RuntimeError("cannot parse installed pydantic-settings release version")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _assert_safe_pydantic_settings_version() -> None:
    try:
        raw_version = distribution_version("pydantic-settings")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "pydantic-settings distribution metadata is unavailable"
        ) from exc

    installed = _parse_release_version(raw_version)
    if not (MIN_PYDANTIC_SETTINGS_VERSION <= installed < MAX_PYDANTIC_SETTINGS_VERSION):
        raise RuntimeError(
            "unsafe or unsupported pydantic-settings version: require "
            ">=2.14.2,<3 (excludes CVE-2026-58203 affected releases)"
        )


def load_settings(
    *,
    config_file: str | Path | None = None,
    env_file: str | Path | None = None,
    secrets_dir: str | Path | None = None,
) -> Settings:
    """Load and validate settings once during process bootstrap.

    ``AW_CONFIG_FILE`` is evaluated here rather than at module import time, so
    tests and CLI entry points can select an overlay before calling this
    function. Production deliberately refuses dotenv input.
    """

    _assert_safe_pydantic_settings_version()
    _reject_unknown_environment_variables()
    config_files = _resolve_config_files(config_file)
    _reject_sensitive_toml_fields(config_files)
    environment = _declared_environment(config_files)

    configured_env_file = env_file or _read_control_env("AW_ENV_FILE")
    if environment == "production" and configured_env_file:
        raise ValueError("production must not load a dotenv file")
    if environment == "production":
        selected_env_file: Path | None = None
    else:
        selected_env_file = _resolve_path(configured_env_file or ".env")
    _reject_unknown_dotenv_variables(selected_env_file)

    configured_secrets_dir = secrets_dir or _read_control_env("AW_SECRETS_DIR")
    selected_secrets_dir = (
        _resolve_path(configured_secrets_dir) if configured_secrets_dir else None
    )
    _reject_conflicting_secret_sources(selected_secrets_dir)

    runtime_model_config = dict(Settings.model_config)
    runtime_model_config["toml_file"] = [str(path) for path in config_files]

    class LoadedSettings(Settings):
        model_config = runtime_model_config

    loaded = LoadedSettings(
        _env_file=selected_env_file,  # pyright: ignore[reportCallIssue]
        _secrets_dir=selected_secrets_dir,  # pyright: ignore[reportCallIssue]
    )
    if (
        loaded.app.environment == "production"
        and selected_env_file is not None
        and selected_env_file.is_file()
    ):
        raise ValueError("production must not load a dotenv file")
    return loaded


if __name__ == "__main__":
    loaded = load_settings()
    output = {
        "startup_revision": loaded.revision(),
        "run_semantics_revision": loaded.run_semantics_revision(),
        "settings": loaded.public_config(),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True))
