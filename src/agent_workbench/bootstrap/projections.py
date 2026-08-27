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
from dataclasses import dataclass, field
from typing import Literal

from pydantic import SecretStr

from agent_workbench.adapters.mcp.naming import SkipReason, tool_name_for
from agent_workbench.adapters.tools.export_artifact import (
    TOOL_NAME as EXPORT_ARTIFACT_TOOL,
)
from agent_workbench.adapters.tools.external_search import (
    TOOL_NAME as EXTERNAL_SEARCH_TOOL,
)
from agent_workbench.bootstrap.settings import ModelPricingSettings, Settings
from agent_workbench.domain.identifiers import new_id
from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.pricing import ModelPrices
from agent_workbench.domain.sandbox import SANDBOX_RUN_TOOL
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolName
from agent_workbench.domain.workspace import (
    WORKSPACE_EDIT_TOOL,
    WORKSPACE_GREP_TOOL,
    WORKSPACE_LIST_TOOL,
    WORKSPACE_READ_TOOL,
    WORKSPACE_WRITE_TOOL,
)
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
#: The workspace tools ride in every variant (ADR-028). They are the one
#: addition that does not widen anything outward: each binds a name inside this
#: Task's own versioned artifact store, `max_tool_risk` already reaches "write",
#: and a replay produces another version rather than a second outside effect.
#: Alphabetical, because ``AuthorizationEnvelope`` sorts what it is given.
#: Keeping this in the order the envelope will end up in is what lets a reader
#: compare the two without re-sorting one of them in their head.
WORKSPACE_TOOLS: tuple[ToolName, ...] = (
    WORKSPACE_EDIT_TOOL,
    WORKSPACE_GREP_TOOL,
    WORKSPACE_LIST_TOOL,
    WORKSPACE_READ_TOOL,
    WORKSPACE_WRITE_TOOL,
)

TASK_V1_AUTHORIZATION_ENVELOPE = AuthorizationEnvelope(
    allowed_tools=(EXPORT_ARTIFACT_TOOL, *WORKSPACE_TOOLS),
    max_tool_risk="write",
    approval_required_risks=(),
)

#: The same ceiling with external search added, for deployments that configured
#: a provider (ADR-020).
#:
#: Both changes are required together: ``external_search`` has to be in the
#: allowlist *and* ``max_tool_risk`` has to reach "external", because
#: ``risk_within`` ranks external above write. Raising only one of them produces
#: an envelope that still denies the tool, which reads as a bug rather than as
#: a policy.
#:
#: ``approval_required_risks`` stays empty for the reason above: the human gate
#: is the graph's approval node, and a tool-boundary gate the gateway can only
#: answer with "no" is not a gate.
TASK_V1_AUTHORIZATION_ENVELOPE_WITH_SEARCH = AuthorizationEnvelope(
    allowed_tools=(EXPORT_ARTIFACT_TOOL, EXTERNAL_SEARCH_TOOL, *WORKSPACE_TOOLS),
    max_tool_risk="external",
    approval_required_risks=(),
)


def task_authorization_envelope(
    *,
    external_search: bool,
    mcp_tools: tuple[ToolName, ...] = (),
    sandbox: bool = False,
) -> AuthorizationEnvelope:
    """Pick the ceiling this deployment submits Tasks under.

    Chosen from configuration rather than fixed, because the envelope is stored
    with the Task and re-applied on every resume: a deployment that never turned
    external search on must not have its historical Tasks widened by an upgrade.

    ``sandbox`` widens on both axes at once for the same reason ``external_search``
    does. ``sandbox_run`` declares ``risk="external"`` (ADR-029 §3.5), and
    ``risk_within`` ranks external above write, so an envelope that allowlisted
    the name without raising the ceiling would still refuse the call.
    """

    if not mcp_tools and not sandbox:
        return (
            TASK_V1_AUTHORIZATION_ENVELOPE_WITH_SEARCH
            if external_search
            else TASK_V1_AUTHORIZATION_ENVELOPE
        )
    # The workspace tools are in every variant, so this branch has to carry
    # them too. Spelling them once here rather than deriving the tuple from the
    # constants above keeps the two branches readable, and the test that pins
    # both shapes is what stops them drifting apart.
    tools: tuple[ToolName, ...] = (
        (EXPORT_ARTIFACT_TOOL, EXTERNAL_SEARCH_TOOL, *mcp_tools, *WORKSPACE_TOOLS)
        if external_search
        else (EXPORT_ARTIFACT_TOOL, *mcp_tools, *WORKSPACE_TOOLS)
    )
    if sandbox:
        tools = (*tools, SANDBOX_RUN_TOOL)
    return AuthorizationEnvelope(
        allowed_tools=tools,
        max_tool_risk="external",
        approval_required_risks=(),
    )


def configured_mcp_tool_names(settings: Settings) -> tuple[ToolName, ...]:
    """The concrete MCP names a new Task may capture from this deployment.

    Only servers whose effects were explicitly declared retryable are eligible.
    Settings already validated every configured remote name; the guard remains
    here so a future alternate Settings constructor cannot turn a refusal into
    a silently missing permission.
    """

    resolved: list[ToolName] = []
    for server in settings.mcp.servers:
        if not server.retryable_effects:
            continue
        for remote_name in server.tools:
            local = tool_name_for(server.alias, remote_name)
            if isinstance(local, SkipReason):  # pragma: no cover - Settings gate
                raise ValueError(
                    f"configured MCP tool {remote_name!r} cannot be named: "
                    f"{local.reason}"
                )
            resolved.append(local)
    collisions = sorted({name for name in resolved if resolved.count(name) > 1})
    if collisions:
        raise ValueError(
            "configured MCP tools collide after local name normalization: "
            + ", ".join(collisions)
        )
    return tuple(sorted(resolved))


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
    #: The profile's thinking stance (ADR-061), carried verbatim from
    #: settings: `unsupported` sends no parameter, the other two send it
    #: explicitly both ways.
    thinking: Literal["unsupported", "disabled", "enabled"]
    reasoning_effort: Literal["low", "high", "max"]
    #: What this profile's model charges, when the deployment said. ``None``
    #: is not "free": it is a process that cannot enforce a cost ceiling, and
    #: the runtime refuses one rather than accepting a ceiling it will never
    #: reach.
    prices: ModelPrices | None
    #: How many tokens one request to this model may hold, when the deployment
    #: said (ADR-0080). ``None`` is not "unlimited": it is a process that
    #: cannot tell a long turn from a short one, and whose over-long turns die
    #: as a provider 400 with nothing naming the cause.
    context_window_tokens: int | None = None


def _project_prices(pricing: ModelPricingSettings | None) -> ModelPrices | None:
    """Carry a configured price list across, or carry the absence of one.

    Absence stays absence. Substituting zeroed rates here would turn "this
    deployment did not say" into "this deployment says its model is free", and
    the runtime could no longer tell the two apart -- which is exactly the
    distinction that decides whether a cost ceiling may be accepted.
    """

    if pricing is None:
        return None
    return ModelPrices(
        input_micro_usd_per_mtok=pricing.input_micro_usd_per_mtok,
        output_micro_usd_per_mtok=pricing.output_micro_usd_per_mtok,
        cache_read_micro_usd_per_mtok=pricing.cache_read_micro_usd_per_mtok,
        cache_write_micro_usd_per_mtok=pricing.cache_write_micro_usd_per_mtok,
    )


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
    #: Whether candidates come from the LlamaIndex retriever (ADR-017) or from
    #: the reference path it is replacing. The only one of ``rag.llama_index``'s
    #: five fields projected here, because it is the only one that selects
    #: anything.
    #:
    #: ``role``, ``agent_executor_enabled``, ``query_engine_generates_final_answer``
    #: and ``fusion_enabled`` stay in settings unprojected on purpose. All four
    #: are single-valued ``Literal``s, so a process-side check on them could
    #: only ever compare a constant against itself -- it would read like
    #: enforcement while being unable to fail. What actually holds them is
    #: structural: the architecture guard refuses an adapter that imports
    #: LlamaIndex's agent or query-engine machinery at all, and the retriever's
    #: contract test pins the index's own ordering, which is what a second
    #: fusion would have to disturb.
    llama_index_enabled: bool


@dataclass(frozen=True, slots=True)
class EventStreamConfig:
    """How a subscriber catches up, and how much it may pull at once."""

    replay_page_size: int
    catchup_poll_seconds: int
    #: What one live subscriber may hold, and how many of them one stream may
    #: have. Transient events are never stored, so these are the only two
    #: numbers standing between a slow reader and this process's memory.
    subscriber_buffer_events: int
    max_live_subscribers_per_stream: int
    #: How long a burst of token deltas is allowed to keep arriving before it
    #: is sent as one frame.
    live_delta_coalesce_ms: int


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

    retrieval_shape: Literal["fixed", "agentic", "ungrounded", "routed"]
    max_agentic_steps: int
    max_agentic_searches: int
    routed_relevance_threshold: float


@dataclass(frozen=True, slots=True)
class TriageConfig:
    """Whether this API proposes a submission's shape, and how long it may try."""

    enabled: bool
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class EvaluationRunsConfig:
    """What the API process needs to start an evaluation, if it may.

    Only the run half. The metric names, gold-set paths and judge settings in
    `EvaluationSettings` belong to the offline runners, and a process that never
    computes a metric has no business carrying the list of them.
    """

    runs_enabled: bool
    reports_root: str
    run_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class CodeConfig:
    """What the API process needs to run coding sessions.

    Projected rather than passed through, like every other section: the
    settings type stops at this boundary, so nothing downstream can widen a
    ceiling by reaching back for the file it came from.

    The two frozen premises do not appear here. ``execution_locality`` and
    ``coordination`` are checked at startup and then have nothing left to say:
    they describe the only arrangement this code implements, so carrying them
    forward would invite a reader to branch on a value that cannot vary.
    """

    enabled: bool
    #: Whether this session may call ``sandbox_run`` (ADR-057). Unlike the two
    #: frozen premises above, this one *does* appear here: it decides which
    #: tool list the envelope is built from, which is a branch downstream has
    #: to take rather than a constant it can assume.
    sandbox_enabled: bool
    #: Whether each ``sandbox_run`` call waits for a human (ADR-058). Carried
    #: because it decides the envelope's armed risks and which prompt variant
    #: a turn is given -- both downstream branches.
    external_requires_approval: bool
    #: Whether this session may call ``project_run`` (ADR-077).
    #:
    #: Projected from ``policy.shell_tools_enabled``, which is in ``[policy]``
    #: and not in ``[code]`` -- ``policy_fingerprint`` hashes that section, so
    #: flipping it changes the ``policy_identity`` every run records. It is
    #: renamed on the way through because a projection is named for what the
    #: consumer branches on: downstream the question is "does this session get
    #: the tool", and "shell tools" is the deployment's word for it, not the
    #: session's.
    host_commands_enabled: bool
    #: Whether this session may call ``web_search`` (ADR-0085).
    #:
    #: Already the conjunction of ``policy.search_tools_enabled`` and a
    #: configured ``[research]`` provider; nothing downstream re-checks either
    #: half. Renamed on the way through for the reason the field above is:
    #: downstream the question is "does this turn get the tool", and
    #: "search tools" is the deployment's word rather than the session's.
    web_search_enabled: bool
    turn_timeout_seconds: int
    approval_timeout_seconds: int
    max_steps: int
    max_tool_calls: int
    max_total_tokens: int | None
    max_cost_micro_usd: int | None
    max_concurrent_turns: int


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
    #: Whether the graph pauses before export. Projected from
    #: `workflow.export_requires_approval` and read once per Task at load, so
    #: the answer is frozen into the checkpoint rather than re-read on resume.
    #: Defaulted True so every existing construction of this projection --
    #: including the tests that build one by hand -- keeps ADR-031's shape.
    export_requires_approval: bool = True


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfig:
    """The bounded custom model/tool loop a Task Worker owns."""

    max_steps: int
    max_tool_calls: int
    model_timeout_seconds: float
    max_parallel_read_tools: int
    # ADR-019. Projected rather than read from settings inside the runtime,
    # because the runtime is framework-neutral and does not import settings.
    record_step_inputs: bool = False
    #: How much of a declared context window one run may fill before it stops
    #: itself (ADR-0080). Only meaningful beside a profile that declared
    #: `context_window_tokens`; with no window there is nothing to take a
    #: fraction of, and no ceiling.
    context_soft_limit_ratio: float = 0.75
    #: ADR-081. Whether a run over that ratio shortens itself instead of
    #: stopping. Only reachable beside a declared window.
    context_compaction_enabled: bool = False
    #: Deployment-wide ceiling on one tool call, or None for "only what each
    #: tool declares". May shorten a call, never lengthen it.
    tool_timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class MultiAgentConfig:
    """The ceilings on the fixed graph's agents.

    Three fields, and deliberately not the fourth.
    ``max_agent_invocation_attempts_per_task`` counts attempts *across* retries
    and reclaims, so it needs a durable per-Task counter rather than a number
    passed into a process; projecting it here would put it one import away from
    looking enforced. It stays in settings, unprojected, until the repository
    that can honour it exists.
    """

    #: How many agent nodes the compiled graph may declare. Checked at assembly:
    #: it describes the graph's shape, and a graph that exceeds it should stop
    #: the process rather than surprise one Task at a time.
    static_agent_node_limit: int
    #: How many agent invocations this Worker runs at once.
    max_parallel_agent_invocations: int
    #: The token ceiling for one invocation, which is what stops a single agent
    #: from spending a Task's whole allowance.
    max_tokens_per_agent_invocation: int
    max_cost_micro_usd_per_agent_invocation: int | None
    max_seconds_per_agent_invocation: int | None


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    """What it takes to build the external-search provider (ADR-020).

    Absent when the deployment did not enable it, which is the same condition
    that keeps `external_search` out of the authorization envelope -- so a
    Worker never holds a provider the envelope would refuse to let it use.
    """

    provider: Literal["deepseek"]
    base_url: str
    model_id: str
    max_uses: int
    timeout_seconds: int
    #: The model provider's own key. Search runs on the provider's side through
    #: its Anthropic-compatible endpoint, so there is no second credential.
    api_key: SecretStr


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """One already-validated MCP endpoint and its explicit tool allowlist."""

    alias: str
    endpoint: str
    retryable_effects: bool
    timeout_seconds: int
    remote_tools: tuple[str, ...]
    #: Which agent receives this server's tools (ADR-027 §3.3). The Task
    #: envelope still lists every configured name regardless: the envelope is
    #: the Task's ceiling, and this is which agent may reach up to it.
    audience: Literal["research", "synthesis"] = "synthesis"


@dataclass(frozen=True, slots=True)
class MCPConfig:
    """The MCP limits and endpoints owned by one Task Worker process."""

    servers: tuple[MCPServerConfig, ...]
    artifact_threshold_bytes: int
    max_result_bytes: int
    max_artifact_bytes: int


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Which sandbox process this Worker will look for at startup (ADR-029).

    Absent when the deployment did not enable one, which is the same condition
    that keeps ``sandbox_run`` out of the authorization envelope -- so a Worker
    never probes for a sandbox the envelope would refuse to let it use.
    """

    endpoint: str
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class BlockingCallRunnerConfig:
    """The bound on synchronous adapter work, for whichever process runs it.

    ADR-042. Projected rather than read from settings inside the adapters, for
    the same reason as everything else here: an adapter that imported settings
    would be a second place deciding what a deployment means.

    Defaults match ``config.default.toml`` so every hand-built projection in
    the tests keeps working; the shipped configuration is still the only thing
    a deployment reads.
    """

    slots: int = 2
    queue_timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class TaskWorkerRuntimeConfig:
    """The minimum configuration of one Worker process.

    ``worker_concurrency`` was pinned to ``Literal[1]`` here, on the stated
    grounds that "those coordination mechanisms have not been assembled yet".
    They since were, and the pin outlived the reason (ADR-024): claims are
    ``FOR UPDATE SKIP LOCKED``, the lease carries a monotonic epoch, every
    Registry write is fenced on owner-epoch-expiry, and the checkpointer is
    composed with ``require_fence=True``. What the pin now describes is one
    ``while`` loop, not a missing guarantee.

    It stays a *process-local* lane count. Multi-*process* claiming is a
    different question -- it is the one the epoch fencing exists for, and it
    remains untested here -- so this projection says how many Tasks one process
    may run at once and claims nothing about running two of these processes.
    """

    database: DatabaseConfig
    artifacts: ArtifactStoreConfig
    task: TaskConfig
    worker_id: str
    worker_concurrency: int
    # Demo and adapter-injection tests intentionally do not need heavy model
    # runtimes. A normal projected Worker always carries all of these -- there
    # is no configuration that withholds the retrieval three, because
    # `settings.rag` and `settings.qdrant` are required sections -- so `None`
    # here means a hand-built config and nothing else.
    #
    # Which is why real assembly does not treat "retrieval configured" as
    # "retrieval available": it refuses to substitute *synthetic* retrieval,
    # but an embedding runtime that will not load on this machine downgrades
    # the Worker to v2 rather than stopping it. Reading that refusal off these
    # fields made it fire on every deployment without the embedding extra.
    model: ModelConfig | None = None
    qdrant: QdrantConfig | None = None
    embedding: EmbeddingConfig | None = None
    retrieval: RetrievalConfig | None = None
    runtime: AgentRuntimeConfig | None = None
    multi_agent: MultiAgentConfig | None = None
    research: ResearchConfig | None = None
    mcp: MCPConfig | None = None
    sandbox: SandboxConfig | None = None
    #: ADR-042. How many blocking adapter calls may hold threads at once.
    blocking_calls: BlockingCallRunnerConfig = field(
        default_factory=BlockingCallRunnerConfig
    )


@dataclass(frozen=True, slots=True)
class GraphExtractionConfig:
    """What the second pass needs, and whether it runs at all (ADR-037).

    ``enabled`` is what makes the model optional here. An ingestion worker has
    never needed a model or an API key; requiring one so that a disabled
    feature could be configured would break every deployment that does not
    build a graph. The factory refuses only when ``enabled`` is true and the
    key is absent -- a deployment that asks for extraction without giving it
    credentials has asked for something that cannot happen.
    """

    enabled: bool
    prompt_version: str
    extraction_profile: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class IngestionWorkerRuntimeConfig:
    """The narrow configuration owned by the derived-index writer."""

    database: DatabaseConfig
    artifacts: ArtifactStoreConfig
    qdrant: QdrantConfig
    embedding: EmbeddingConfig
    ingestion: IngestionConfig
    graph: GraphExtractionConfig
    # Absent unless the graph is enabled, and the projection says so rather
    # than inventing an empty one: a process configured without a model is a
    # real state, and refusing to assemble is the factory's job.
    model: ModelConfig | None
    worker_id: str
    claim_limit: int
    poll_seconds: float
    error_backoff_seconds: float
    lease_seconds: int
    heartbeat_seconds: int
    #: ADR-042. How many blocking adapter calls may hold threads at once.
    blocking_calls: BlockingCallRunnerConfig = field(
        default_factory=BlockingCallRunnerConfig
    )


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
    triage: TriageConfig
    code: CodeConfig
    evaluation: EvaluationRunsConfig
    task: TaskConfig
    observability: ObservabilityConfig
    # ADR-019. The API runs the chat loop, so it needs the same switch the Task
    # Worker gets through AgentRuntimeConfig. Flat rather than nested under
    # `chat`, because it is one setting governing every runtime this deployment
    # builds, and two names for it would drift.
    record_step_inputs: bool = False
    #: ADR-0080. Flat here for the reason `record_step_inputs` is: it governs
    #: every runtime this process builds -- chat, the two no-tool shapes, code
    #: -- and two names for one setting drift. What it is a fraction *of* comes
    #: from `model.<profile>.context_window_tokens`, so on a deployment that
    #: declared no window this value decides nothing at all.
    context_soft_limit_ratio: float = 0.75
    #: ADR-081, flat here for the reason above it is: it governs every runtime
    #: this process builds, and two names for one setting drift.
    context_compaction_enabled: bool = False
    # ADR-021. The same provider the Task Worker researches with, reached from
    # the API because chat's fallback may search too. `None` is the shipped
    # default and means the chat model is offered no web tool at all -- not a
    # tool that fails, an absence, so a deployment that configured nothing
    # cannot spend money by accident.
    research: ResearchConfig | None = None
    #: Where this process looks for the sandbox, when a coding session may call
    #: it (ADR-057). `None` whenever `code.sandbox_enabled` is false, so the two
    #: cannot describe different intentions -- this is the address, that is the
    #: decision, and a deployment only ever sets both or neither.
    sandbox: SandboxConfig | None = None
    #: ADR-042. How many blocking adapter calls may hold threads at once.
    blocking_calls: BlockingCallRunnerConfig = field(
        default_factory=BlockingCallRunnerConfig
    )


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
        default_authorization_envelope=task_authorization_envelope(
            external_search=settings.research.enabled,
            mcp_tools=configured_mcp_tool_names(settings),
            sandbox=settings.sandbox.enabled,
        ),
        export_requires_approval=settings.workflow.export_requires_approval,
    )


def project_task_worker(
    settings: Settings, *, worker_id: str | None = None
) -> TaskWorkerRuntimeConfig:
    """Project one Worker process and the lane count it may execute with.

    The ``!= 1`` refusal that used to live here is gone (ADR-024). The ceiling
    that replaces it is not a new one: ``Settings`` already refuses
    ``worker_concurrency > guard_connection_budget``, and that is the bound
    that matters, because each concurrent Task pins its own guard connection.
    Re-raising here would be a second copy of that rule, and the copy is the
    one that keeps running after somebody edits the first.
    """

    return TaskWorkerRuntimeConfig(
        blocking_calls=BlockingCallRunnerConfig(
            slots=settings.coordination.blocking_call_slots,
            queue_timeout_seconds=(
                settings.coordination.blocking_call_queue_timeout_seconds
            ),
        ),
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
        worker_concurrency=settings.coordination.worker_concurrency,
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
                    thinking=profile.thinking,
                    reasoning_effort=profile.reasoning_effort,
                    prices=_project_prices(profile.pricing),
                    context_window_tokens=profile.context_window_tokens,
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
            llama_index_enabled=settings.rag.llama_index.enabled,
        ),
        runtime=AgentRuntimeConfig(
            max_steps=settings.runtime.max_steps,
            max_tool_calls=settings.runtime.max_tool_calls,
            model_timeout_seconds=float(settings.runtime.model_timeout_seconds),
            max_parallel_read_tools=settings.runtime.max_parallel_read_tools,
            record_step_inputs=settings.runtime.record_step_inputs,
            context_soft_limit_ratio=settings.runtime.context_soft_limit_ratio,
            context_compaction_enabled=settings.runtime.context_compaction_enabled,
            tool_timeout_seconds=(
                None
                if settings.runtime.tool_timeout_seconds is None
                else float(settings.runtime.tool_timeout_seconds)
            ),
        ),
        multi_agent=MultiAgentConfig(
            static_agent_node_limit=settings.multi_agent.static_agent_node_limit,
            max_parallel_agent_invocations=(
                settings.multi_agent.max_parallel_agent_invocations
            ),
            max_tokens_per_agent_invocation=(
                settings.multi_agent.max_tokens_per_agent_invocation
            ),
            max_cost_micro_usd_per_agent_invocation=(
                settings.multi_agent.max_cost_micro_usd_per_agent_invocation
            ),
            max_seconds_per_agent_invocation=(
                settings.multi_agent.max_seconds_per_agent_invocation
            ),
        ),
        research=_project_research(settings),
        mcp=(
            MCPConfig(
                servers=tuple(
                    MCPServerConfig(
                        alias=server.alias,
                        endpoint=server.endpoint,
                        retryable_effects=server.retryable_effects,
                        timeout_seconds=server.timeout_seconds,
                        remote_tools=server.tools,
                        audience=server.audience,
                    )
                    for server in settings.mcp.servers
                ),
                artifact_threshold_bytes=(
                    settings.runtime.tool_result_artifact_threshold_bytes
                ),
                max_result_bytes=settings.policy.max_tool_result_bytes,
                max_artifact_bytes=settings.artifact_store.max_artifact_bytes,
            )
            if settings.mcp.servers
            else None
        ),
        sandbox=(
            SandboxConfig(
                endpoint=settings.sandbox.endpoint,
                timeout_seconds=settings.sandbox.timeout_seconds,
            )
            if settings.sandbox.enabled
            else None
        ),
    )


def _project_research(settings: Settings) -> ResearchConfig | None:
    """The search provider, or nothing when this deployment did not enable one.

    Settings has already refused `research.enabled` without a key, so the
    assertion here is about the projection being total, not a second check.
    """

    if not settings.research.enabled or settings.secrets.deepseek_api_key is None:
        return None
    return ResearchConfig(
        provider=settings.research.provider,
        base_url=settings.research.base_url,
        model_id=settings.research.model_id,
        max_uses=settings.research.max_uses,
        timeout_seconds=settings.research.timeout_seconds,
        api_key=settings.secrets.deepseek_api_key,
    )


def project_ingestion_worker(
    settings: Settings, *, worker_id: str | None = None
) -> IngestionWorkerRuntimeConfig:
    """Project the process that turns the durable outbox into a hybrid index."""

    return IngestionWorkerRuntimeConfig(
        blocking_calls=BlockingCallRunnerConfig(
            slots=settings.coordination.blocking_call_slots,
            queue_timeout_seconds=(
                settings.coordination.blocking_call_queue_timeout_seconds
            ),
        ),
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
        graph=GraphExtractionConfig(
            enabled=settings.rag.graph.enabled,
            prompt_version=settings.rag.graph.prompt_version,
            extraction_profile=settings.rag.graph.extraction_profile,
            # One passage, one completion. Generous because the second pass is
            # not on anybody's request path, and bounded because it runs once
            # per chunk and a hung call would hold a lease.
            timeout_seconds=float(
                (
                    settings.model.compact
                    if settings.rag.graph.extraction_profile == "compact"
                    else settings.model.main
                ).timeout_seconds
            ),
        ),
        model=(
            ModelConfig(
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
                        thinking=profile.thinking,
                        reasoning_effort=profile.reasoning_effort,
                        prices=_project_prices(profile.pricing),
                        context_window_tokens=profile.context_window_tokens,
                    )
                    for name, profile in (
                        ("main", settings.model.main),
                        ("compact", settings.model.compact),
                    )
                },
            )
            if settings.rag.graph.enabled
            else None
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
        blocking_calls=BlockingCallRunnerConfig(
            slots=settings.coordination.blocking_call_slots,
            queue_timeout_seconds=(
                settings.coordination.blocking_call_queue_timeout_seconds
            ),
        ),
        observability=project_observability(settings),
        deployment_scope=settings.app.deployment_scope,
        log_level=settings.app.log_level,
        host=settings.api.host,
        port=settings.api.port,
        request_timeout_seconds=settings.api.request_timeout_seconds,
        shutdown_grace_seconds=settings.api.shutdown_grace_seconds,
        sse_heartbeat_seconds=settings.api.sse_heartbeat_seconds,
        max_control_request_body_bytes=settings.api.max_control_request_body_bytes,
        record_step_inputs=settings.runtime.record_step_inputs,
        context_soft_limit_ratio=settings.runtime.context_soft_limit_ratio,
        context_compaction_enabled=settings.runtime.context_compaction_enabled,
        research=_project_research(settings),
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
                    thinking=profile.thinking,
                    reasoning_effort=profile.reasoning_effort,
                    prices=_project_prices(profile.pricing),
                    context_window_tokens=profile.context_window_tokens,
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
            subscriber_buffer_events=settings.event_stream.subscriber_buffer_events,
            max_live_subscribers_per_stream=(
                settings.event_stream.max_live_subscribers_per_stream
            ),
            live_delta_coalesce_ms=settings.event_stream.live_delta_coalesce_ms,
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
            llama_index_enabled=settings.rag.llama_index.enabled,
        ),
        chat=ChatConfig(
            retrieval_shape=settings.chat.retrieval_shape,
            max_agentic_steps=settings.chat.max_agentic_steps,
            max_agentic_searches=settings.chat.max_agentic_searches,
            routed_relevance_threshold=settings.chat.routed_relevance_threshold,
        ),
        chat_recovery=ChatRecoveryConfig(
            orphan_grace_seconds=settings.chat.orphan_grace_seconds,
            reaper_poll_seconds=settings.chat.reaper_poll_seconds,
            reaper_batch_size=settings.chat.reaper_batch_size,
            disconnect_poll_seconds=settings.chat.disconnect_poll_seconds,
        ),
        triage=TriageConfig(
            enabled=settings.triage.enabled,
            timeout_seconds=settings.triage.timeout_seconds,
        ),
        code=CodeConfig(
            enabled=settings.code.enabled,
            # Both, because either alone is a half-configured deployment: the
            # code section asks for the tool and the sandbox section says where
            # the server is. Requiring both here means a profile that turns one
            # on without the other gets the tool-less arrangement rather than a
            # process that starts and then cannot honour what it offered.
            sandbox_enabled=settings.code.sandbox_enabled and settings.sandbox.enabled,
            external_requires_approval=settings.code.external_requires_approval,
            # Only the policy flag, with no `code` twin to and it with. The
            # sandbox needs two because a server has to be running somewhere;
            # a command needs nothing but a directory, and the directory is the
            # session's project. One switch, in the plane that fingerprints it.
            host_commands_enabled=settings.policy.shell_tools_enabled,
            # And-ed with the provider for the same reason `sandbox_enabled`
            # above is and-ed with its server: the policy flag asks for the
            # tool and `[research]` says who answers it. A deployment that
            # turns the flag on and configures no provider gets the tool-less
            # arrangement, rather than a process that starts and then cannot
            # honour what it offered -- and here the failure would be worse
            # than a missing tool. `code_risk_ceiling` raises on a name it has
            # no spec for, so the offered-but-unbuildable case does not degrade
            # to a refusal, it ends every turn in a `ValueError` (ADR-0085).
            web_search_enabled=(
                settings.policy.search_tools_enabled
                and _project_research(settings) is not None
            ),
            turn_timeout_seconds=settings.code.turn_timeout_seconds,
            approval_timeout_seconds=settings.code.approval_timeout_seconds,
            max_steps=settings.code.max_steps,
            max_tool_calls=settings.code.max_tool_calls,
            max_total_tokens=settings.code.max_total_tokens,
            max_cost_micro_usd=settings.code.max_cost_micro_usd,
            max_concurrent_turns=settings.code.max_concurrent_turns,
        ),
        sandbox=(
            SandboxConfig(
                endpoint=settings.sandbox.endpoint,
                timeout_seconds=settings.sandbox.timeout_seconds,
            )
            # Gated on the code section too, so this field is present exactly
            # when something in this process would use it. A sandbox address
            # carried by a process whose coding sessions cannot call it is an
            # invitation to connect for no reason.
            if settings.code.sandbox_enabled and settings.sandbox.enabled
            else None
        ),
        evaluation=EvaluationRunsConfig(
            runs_enabled=settings.evaluation.runs_enabled,
            reports_root=settings.evaluation.reports_root,
            run_timeout_seconds=settings.evaluation.run_timeout_seconds,
        ),
        task=project_task(settings),
    )


__all__ = [
    "AgentRuntimeConfig",
    "ApiRuntimeConfig",
    "ArtifactStoreConfig",
    "ChatConfig",
    "ChatRecoveryConfig",
    "CodeConfig",
    "DatabaseConfig",
    "EmbeddingConfig",
    "EventStreamConfig",
    "IngestionConfig",
    "IngestionWorkerRuntimeConfig",
    "MCPConfig",
    "MCPServerConfig",
    "ModelConfig",
    "ModelProfileConfig",
    "MultiAgentConfig",
    "QdrantConfig",
    "RerankerConfig",
    "RetrievalConfig",
    "SandboxConfig",
    "TaskConfig",
    "TaskWorkerRuntimeConfig",
    "TriageConfig",
    "configured_mcp_tool_names",
    "project_api",
    "project_ingestion_worker",
    "project_task",
    "project_task_worker",
]
