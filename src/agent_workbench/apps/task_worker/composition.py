"""Assembly of the independently deployed, single-Worker Task process.

The API may submit a Task but must not run it as a background coroutine: a
process restart would otherwise discard the executor that owns recovery.  This
composition root uses the same PostgreSQL engine for the registry, event log
and checkpointer, while artifacts remain the source of the immutable input.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.langgraph import (
    LangGraphTaskWorkflow,
    PostgresCheckpointSaver,
    build_approval_node,
)
from agent_workbench.adapters.langgraph.workflow import GRAPH_BUILDERS, NodeHandler
from agent_workbench.adapters.mcp.client import connect_mcp_client
from agent_workbench.adapters.mcp.registry_source import discover_bindings
from agent_workbench.adapters.persistence import (
    PostgresApprovalStore,
    PostgresDocumentStore,
    PostgresEventLog,
    PostgresExecutionGuardFactory,
    PostgresTaskRegistry,
    PostgresToolExecutionLedger,
    create_query_engine,
)
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.research import DeepSeekWebSearch
from agent_workbench.adapters.tools import (
    ExportArtifactTool,
    ExternalSearchTool,
    StaticToolRegistry,
    UnavailableExternalSearch,
)
from agent_workbench.adapters.tools.sandbox import SandboxRunTool
from agent_workbench.adapters.tools.task_export import GatewayReportExport
from agent_workbench.adapters.tools.task_external_research import (
    GatewayExternalEvidence,
)
from agent_workbench.adapters.tools.workspace import (
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceWriteTool,
)
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.retrieval import RetrievalService
from agent_workbench.application.task_inputs import TaskInputStore
from agent_workbench.application.task_research import (
    EvidenceStore,
    ExternalResearchService,
    InternalResearchService,
)
from agent_workbench.apps.task_worker.identity import restore_submitted_principal
from agent_workbench.bootstrap.embedding_factory import (
    EmbeddingUnavailable,
    build_embedder,
)
from agent_workbench.bootstrap.projections import (
    ResearchConfig,
    TaskWorkerRuntimeConfig,
)
from agent_workbench.bootstrap.qdrant_startup import verify_qdrant_startup
from agent_workbench.bootstrap.retrieval_factory import build_candidate_retriever
from agent_workbench.bootstrap.sparse_factory import (
    SparseEncodingUnavailable,
    build_sparse_encoder,
)
from agent_workbench.domain.runs import RunBudget
from agent_workbench.domain.sandbox import SANDBOX_REMOTE_TOOL
from agent_workbench.domain.tasks import TaskNodeId
from agent_workbench.domain.tools import ToolName
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.research import ExternalSearchPort
from agent_workbench.ports.tools import ToolBinding
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway
from agent_workbench.workers.task import TaskWorker
from agent_workbench.workflows.agent_profiles import assert_within_static_limit
from agent_workbench.workflows.approval import TaskApprovalGate
from agent_workbench.workflows.demo_handlers import build_demo_v1_handlers
from agent_workbench.workflows.execution_scope import TaskExecutionScope
from agent_workbench.workflows.task_handlers import (
    BoundedParallelExecutor,
    TaskExportHandlers,
    TaskNodeInvocationProvider,
    TaskResearchHandlers,
    build_task_v1_handlers,
)
from agent_workbench.workflows.workspace_scope import WorkspaceScope


class RealTaskHandlersUnavailableError(RuntimeError):
    """Raised rather than running synthetic output in a production worker."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RealHandlers:
    """What real assembly produced, past the point of returning a 7-tuple."""

    handlers: Mapping[TaskNodeId, NodeHandler]
    http: httpx.AsyncClient
    qdrant: AsyncQdrantClient
    research_http: httpx.AsyncClient | None
    mcp_tool_names: tuple[ToolName, ...]
    sandbox_tool_names: tuple[ToolName, ...]
    tool_names: tuple[ToolName, ...]


@dataclass(frozen=True, slots=True)
class TaskWorkerDependencies:
    """Long-lived resources owned by exactly one Task Worker process."""

    config: TaskWorkerRuntimeConfig
    engine: AsyncEngine
    artifacts: LocalArtifactStore
    events: PostgresEventLog
    registry: PostgresTaskRegistry
    approvals: PostgresApprovalStore
    guards: PostgresExecutionGuardFactory
    # What this process will actually run at each node. Exposed for the same
    # reason the stores are: which handler set a deployment assembled is a
    # deploy-time fact, and the difference between the interrupting approval
    # node and no approval node at all is not visible anywhere else.
    handlers: Mapping[TaskNodeId, NodeHandler]
    # The one claim channel this process has. The Worker enters it around a
    # graph invocation and the real handlers read it back, so a deployment can
    # be inspected for the thing that would otherwise only be visible as two
    # constructor arguments that happen to agree.
    scope: TaskExecutionScope
    worker: TaskWorker
    resources: AsyncExitStack
    http: httpx.AsyncClient | None = None
    # A second client, not a reuse of `http`: that one carries the model
    # profile's timeout and talks to the OpenAI-compatible endpoint, while
    # search talks to the Anthropic-compatible one and is allowed to be slower.
    research_http: httpx.AsyncClient | None = None
    qdrant: AsyncQdrantClient | None = None
    mcp_tool_names: tuple[ToolName, ...] = ()
    #: Empty when this deployment configured no sandbox, and also when it
    #: configured one whose container runtime did not answer (ADR-029 §3.6).
    #: The writer profile is widened from this rather than from configuration,
    #: so the two cases are the same to everything downstream.
    sandbox_tool_names: tuple[ToolName, ...] = ()
    # Every tool this process registered, exposed for the same reason
    # `handlers` is: which tools a deployment assembled is a deploy-time fact,
    # and the difference between a registry that satisfies the agent profiles
    # and one that is missing three of them is otherwise invisible until a
    # Task reaches the node that asks for them.
    tool_names: tuple[ToolName, ...] = ()

    async def startup(self) -> None:
        """Validate the live read alias before claiming durable Task work."""

        if self.qdrant is not None:
            assert self.config.qdrant is not None
            assert self.config.embedding is not None
            await verify_qdrant_startup(
                self.qdrant,
                qdrant=self.config.qdrant,
                embedding=self.config.embedding,
            )

    async def dispose(self) -> None:
        """Release every process-owned client and pool after the loop stops."""

        await self.resources.aclose()


async def build_task_worker_dependencies(
    config: TaskWorkerRuntimeConfig,
    *,
    handlers: Mapping[TaskNodeId, NodeHandler] | None = None,
    demo: bool = False,
) -> TaskWorkerDependencies:
    """Build one Worker with production handlers or explicit local demo ones.

    Real assembly owns the complete model, retrieval and policy path.  It is
    intentionally eager: accepting work while an embedding runtime or model
    key is absent turns a deploy-time error into a per-Task incident.
    """

    if config.artifacts.backend != "local":
        raise ValueError(
            f"the {config.artifacts.backend} artifact backend has no Task Worker "
            "adapter"
        )
    if handlers is not None and demo:
        raise ValueError("pass either real handlers or demo=True, not both")
    resources = AsyncExitStack()
    http: httpx.AsyncClient | None = None
    # A second client, not a reuse of `http`: that one carries the model
    # profile's timeout and talks to the OpenAI-compatible endpoint, while
    # search talks to the Anthropic-compatible one and is allowed to be slower.
    research_http: httpx.AsyncClient | None = None
    qdrant: AsyncQdrantClient | None = None
    if handlers is None and demo:
        handlers = build_demo_v1_handlers()

    try:
        scope = TaskExecutionScope()
        engine = create_query_engine(
            config.database.dsn.get_secret_value(),
            application_name=config.database.application_name,
            statement_timeout_ms=config.database.statement_timeout_ms,
            pool_size=config.database.pool_size,
            max_overflow=config.database.max_overflow,
        )
        # Register every process resource as soon as it exists. If model, MCP
        # or gateway assembly fails later, the same stack unwinds the partial
        # build; callers never need a half-constructed dependency object to
        # clean it up.
        resources.push_async_callback(engine.dispose)
        artifacts = LocalArtifactStore(Path(config.artifacts.local_root))
        events = PostgresEventLog(engine)
        registry = PostgresTaskRegistry(engine, events=events)
        guard_dsn = config.database.guard_dsn or config.database.dsn
        guards = PostgresExecutionGuardFactory(
            guard_dsn.get_secret_value(),
            healthcheck_seconds=float(config.database.guard_healthcheck_seconds),
            application_name=f"{config.database.application_name}-guard",
        )
        resources.push_async_callback(guards.dispose)
        assembled: _RealHandlers | None = None
        # Wired whatever the handlers are. The demo graph answers its own gate and
        # never interrupts, but a Worker that could meet an interrupt without a
        # ledger would park the Task instead of resuming it -- and the ledger costs
        # nothing to hold.
        approvals = PostgresApprovalStore(engine, events=events)
        if handlers is None:
            assembled = await _build_real_handlers(
                config,
                artifacts=artifacts,
                documents=PostgresDocumentStore(engine),
                events=events,
                registry=registry,
                ledger=PostgresToolExecutionLedger(engine),
                scope=scope,
                resources=resources,
            )
            handlers = assembled.handlers
            http = assembled.http
            qdrant = assembled.qdrant
            research_http = assembled.research_http
            # The one node the handler factory cannot build: it has to interrupt,
            # and interrupting belongs to the workflow framework, so it is
            # assembled here from the framework-neutral gate.
            real: dict[TaskNodeId, NodeHandler] = dict(handlers)
            real["approval"] = build_approval_node(
                TaskApprovalGate(approvals=approvals, registry=registry)
            )
            handlers = real
        inputs = TaskInputStore(artifacts)
        workflow = LangGraphTaskWorkflow(
            handlers=handlers,
            checkpointer=PostgresCheckpointSaver(engine, require_fence=True),
        )
        worker = TaskWorker(
            registry=registry,
            # The same object the handlers read. Two scopes would be one Worker
            # publishing a claim nobody receives, and every node refusing.
            scope=scope,
            guards=guards,
            approvals=approvals,
            workflow=workflow,
            load_state=inputs.load_state,
            buildable_versions=tuple(GRAPH_BUILDERS),
            worker_id=config.worker_id,
            lease_seconds=config.task.lease_seconds,
            heartbeat_seconds=config.task.heartbeat_seconds,
            max_attempts=config.task.max_attempts,
            retry_base_seconds=config.task.retry_base_seconds,
            retry_max_seconds=config.task.retry_max_seconds,
        )
        return TaskWorkerDependencies(
            config=config,
            engine=engine,
            artifacts=artifacts,
            events=events,
            registry=registry,
            approvals=approvals,
            guards=guards,
            handlers=handlers,
            scope=scope,
            worker=worker,
            resources=resources,
            http=http,
            research_http=research_http,
            qdrant=qdrant,
            mcp_tool_names=() if assembled is None else assembled.mcp_tool_names,
            sandbox_tool_names=(
                () if assembled is None else assembled.sandbox_tool_names
            ),
            tool_names=() if assembled is None else assembled.tool_names,
        )
    except BaseException:
        await resources.aclose()
        raise


def _build_external_search(
    research: ResearchConfig | None,
    *,
    resources: AsyncExitStack,
) -> tuple[ExternalSearchPort, httpx.AsyncClient | None]:
    """The configured search provider, or the fail-closed placeholder.

    Returning ``UnavailableExternalSearch`` rather than raising is deliberate:
    a Worker whose deployment never enabled search still has to start, and a
    Task that reaches ``research_external`` records "no provider" rather than
    failing. That is also the state the authorization envelope agrees with --
    with search off the tool would be denied before it ran anyway (ADR-020).

    The client comes back with it so the dependency owner can close it, the
    same way the model's client is handled.
    """

    if research is None:
        return UnavailableExternalSearch(), None
    client = httpx.AsyncClient(timeout=research.timeout_seconds)
    resources.push_async_callback(client.aclose)
    return (
        DeepSeekWebSearch(
            http=client,
            api_key=research.api_key.get_secret_value(),
            model=research.model_id,
            base_url=research.base_url,
            max_uses=research.max_uses,
        ),
        client,
    )


async def _build_real_handlers(
    config: TaskWorkerRuntimeConfig,
    *,
    artifacts: LocalArtifactStore,
    documents: PostgresDocumentStore,
    events: PostgresEventLog,
    registry: PostgresTaskRegistry,
    ledger: PostgresToolExecutionLedger,
    scope: TaskExecutionScope,
    resources: AsyncExitStack,
) -> _RealHandlers:
    """Assemble Task evidence and model execution without a demo fallback."""

    if (
        config.model is None
        or config.qdrant is None
        or config.embedding is None
        or config.retrieval is None
        or config.runtime is None
        or config.multi_agent is None
    ):
        raise RealTaskHandlersUnavailableError(
            "real Task handlers require model, qdrant, embedding, retrieval, "
            "runtime and multi-agent configuration; use project_task_worker "
            "or --demo"
        )

    # Before anything is built. The limit describes the compiled graph's shape,
    # so a graph that outgrew what this deployment budgeted for must stop the
    # process rather than be discovered one expensive Task at a time.
    assert_within_static_limit(config.multi_agent.static_agent_node_limit)

    main_profile = config.model.profiles.get("main")
    if main_profile is None:
        raise RealTaskHandlersUnavailableError(
            "real Task handlers require a configured main model profile"
        )
    if config.model.provider != "deepseek":
        raise RealTaskHandlersUnavailableError(
            f"no Task Worker model adapter exists for {config.model.provider!r}"
        )
    if (
        config.model.api_key is None
        or not config.model.api_key.get_secret_value().strip()
    ):
        raise RealTaskHandlersUnavailableError(
            "Task Worker requires secrets.deepseek_api_key for real model calls"
        )

    embedder = build_embedder(config.embedding)
    if isinstance(embedder, EmbeddingUnavailable):
        raise RealTaskHandlersUnavailableError(
            f"Task Worker requires an embedding runtime: {embedder.reason}"
        )
    built_sparse = build_sparse_encoder(config.embedding)
    sparse_encoder = (
        None if isinstance(built_sparse, SparseEncodingUnavailable) else built_sparse
    )

    # build_model only validates configuration while constructing its adapter;
    # no provider connection is opened here. The client is a process resource
    # and is returned to the dependency owner for deterministic shutdown.
    http = httpx.AsyncClient(timeout=main_profile.timeout_seconds)
    resources.push_async_callback(http.aclose)
    from agent_workbench.bootstrap.model_factory import build_model

    model = build_model(config.model, client=http)

    qdrant = AsyncQdrantClient(
        url=config.qdrant.url,
        api_key=(
            config.qdrant.api_key.get_secret_value()
            if config.qdrant.api_key is not None
            else None
        ),
        timeout=config.qdrant.request_timeout_seconds,
    )
    resources.push_async_callback(qdrant.close)
    retrieval = RetrievalService(
        candidate_retriever=build_candidate_retriever(
            llama_index_enabled=config.retrieval.llama_index_enabled,
            embedder=embedder,
            index=QdrantVectorIndex(qdrant, collection=config.qdrant.read_alias),
            sparse_encoder=sparse_encoder,
        ),
        documents=documents,
    )
    evidence = EvidenceStore(artifacts)
    external_search, research_http = _build_external_search(
        config.research,
        resources=resources,
    )
    external_tool = ExternalSearchTool(
        ExternalResearchService(
            search=external_search,
            evidence=evidence,
        )
    )
    export_tool = ExportArtifactTool(artifacts=artifacts)
    # The working set (ADR-028). The writer profile and the authorization
    # envelope both name these three, and `ToolGateway.advertise` raises for a
    # requested tool the process does not register -- so a Worker assembled
    # without them does not lose a capability, it fails at `synthesize`.
    workspace_scope = WorkspaceScope()
    workspace_bindings = (
        WorkspaceListTool(workspace_scope).binding(),
        WorkspaceReadTool(workspace_scope).binding(),
        WorkspaceWriteTool(workspace_scope).binding(),
    )
    mcp_bindings = await _build_mcp_bindings(
        config,
        artifacts=artifacts,
        resources=resources,
    )
    sandbox_binding = await _build_sandbox_binding(
        config,
        scope=workspace_scope,
        resources=resources,
    )
    sandbox_bindings = () if sandbox_binding is None else (sandbox_binding,)
    tool_registry = StaticToolRegistry(
        (
            external_tool.binding(),
            export_tool.binding(),
            *workspace_bindings,
            *mcp_bindings,
            *sandbox_bindings,
        )
    )
    gateway = ToolGateway(
        registry=tool_registry,
        policy=EnvelopePolicyEngine(registry=tool_registry),
        # Required, not optional: the gateway refuses to assemble around a
        # ledgered tool with nowhere to record it, and export_artifact is one.
        ledger=ledger,
        record_step_inputs=config.runtime.record_step_inputs,
    )
    policy_identity = (
        f"{config.task.policy_revision}:{config.task.policy_fingerprint[:16]}"
    )
    executor = BoundedParallelExecutor(
        ClaudeLikeAgentRuntime(
            model=model,
            gateway=gateway,
            policy_identity=policy_identity,
            model_label=main_profile.model_id,
            model_timeout_seconds=config.runtime.model_timeout_seconds,
            max_parallel_read_tools=config.runtime.max_parallel_read_tools,
            record_step_inputs=config.runtime.record_step_inputs,
        ),
        max_parallel=config.multi_agent.max_parallel_agent_invocations,
    )
    invocations = TaskNodeInvocationProvider(
        registry=registry,
        budget=RunBudget(
            max_steps=config.runtime.max_steps,
            max_tool_calls=config.runtime.max_tool_calls,
            # One agent's ceiling, not the Task's. Without it a single
            # invocation could spend everything the Task was allowed.
            max_total_tokens=config.multi_agent.max_tokens_per_agent_invocation,
        ),
        sink_for=lambda context: ScopedEventSink(
            events,
            EventScope(
                stream_id=context.stream_id,
                run_id=context.trace.agent_run_id,
                task_id=context.trace.task_id,
                graph_node_id=context.trace.graph_node_id,
            ),
        ),
        cancellation_for=lambda _: NullCancellationToken(),
        principal_for=restore_submitted_principal,
        scope=scope,
    )
    handlers = build_task_v1_handlers(
        executor=executor,
        artifacts=artifacts,
        invocations=invocations,
        research=TaskResearchHandlers(
            internal=InternalResearchService(retrieval=retrieval, evidence=evidence),
            evidence=evidence,
            external=GatewayExternalEvidence(gateway),
            policy_identity=policy_identity,
        ),
        export=TaskExportHandlers(
            export=GatewayReportExport(gateway=gateway, ledger=ledger),
            policy_identity=policy_identity,
        ),
        mcp_tool_names=tuple(binding.spec.name for binding in mcp_bindings),
        sandbox_tool_names=tuple(binding.spec.name for binding in sandbox_bindings),
        workspace_scope=workspace_scope,
    )
    return _RealHandlers(
        handlers=handlers,
        http=http,
        qdrant=qdrant,
        research_http=research_http,
        mcp_tool_names=tuple(binding.spec.name for binding in mcp_bindings),
        sandbox_tool_names=tuple(binding.spec.name for binding in sandbox_bindings),
        tool_names=tuple(spec.name for spec in tool_registry.specs()),
    )


async def _build_sandbox_binding(
    config: TaskWorkerRuntimeConfig,
    *,
    scope: WorkspaceScope,
    resources: AsyncExitStack,
) -> ToolBinding | None:
    """The sandbox tool, or nothing, after one probe (ADR-029 §3.6).

    The probe is a real ``run_python`` call rather than a connection or a health
    read, and that is the point: what has to be true is that this Worker can
    start a container and get a result back, and a socket that accepts is not
    evidence of either. It costs one container start at Worker boot.

    Every failure is fail-soft, on the same terms ADR-025 set for a server that
    will not answer: log it, register nothing, start anyway. A deployment
    without a container runtime is a deployment with one fewer capability, not
    one that cannot run Tasks -- and the Task envelope is what keeps that
    honest, since a profile is only ever widened by what was registered here.
    """

    if config.sandbox is None:
        return None

    async with AsyncExitStack() as candidate_resources:
        try:
            async with asyncio.timeout(config.sandbox.timeout_seconds):
                client = await candidate_resources.enter_async_context(
                    connect_mcp_client(
                        config.sandbox.endpoint,
                        timeout_seconds=config.sandbox.timeout_seconds,
                    )
                )
                probe = await client.call_tool(
                    SANDBOX_REMOTE_TOOL,
                    {"script": "pass"},
                )
        except Exception as error:
            logger.warning(
                "sandbox_probe_failed",
                extra={
                    "sandbox_endpoint": config.sandbox.endpoint,
                    "sandbox_error_type": type(error).__name__,
                },
            )
            return None
        if probe.is_error:
            # The server answered and the runtime did not. Same outcome, and a
            # distinct log line because the fix is a different one.
            logger.warning(
                "sandbox_runtime_unavailable",
                extra={"sandbox_endpoint": config.sandbox.endpoint},
            )
            return None
        owned = candidate_resources.pop_all()
        resources.push_async_callback(owned.aclose)
        return SandboxRunTool(scope=scope, client=client).binding()
    return None  # pragma: no cover - AsyncExitStack does not suppress


async def _build_mcp_bindings(
    config: TaskWorkerRuntimeConfig,
    *,
    artifacts: LocalArtifactStore,
    resources: AsyncExitStack,
) -> tuple[ToolBinding, ...]:
    if config.mcp is None:
        return ()

    bindings: list[ToolBinding] = []
    for server in config.mcp.servers:
        if not server.retryable_effects:
            logger.warning(
                "mcp_server_skipped_nonretryable",
                extra={"mcp_server_alias": server.alias},
            )
            continue
        # A candidate connection is owned locally until discovery yields at
        # least one usable binding. Empty/failed directories close immediately
        # instead of occupying a socket for the entire Worker lifetime.
        async with AsyncExitStack() as candidate_resources:
            try:
                # The SDK's read timeout governs established requests, but its
                # connection/discovery negotiation can otherwise outlive this
                # server's configured startup budget. Cancellation unwinds the
                # async context manager, including a half-open SDK Client.
                async with asyncio.timeout(server.timeout_seconds):
                    client = await candidate_resources.enter_async_context(
                        connect_mcp_client(
                            server.endpoint,
                            timeout_seconds=server.timeout_seconds,
                        )
                    )
            except Exception as error:
                logger.warning(
                    "mcp_connection_failed",
                    extra={
                        "mcp_server_alias": server.alias,
                        "mcp_error_type": type(error).__name__,
                    },
                )
                continue
            discovered = await discover_bindings(
                alias=server.alias,
                allowed_remote_tools=server.remote_tools,
                timeout_seconds=server.timeout_seconds,
                client=client,
                artifacts=artifacts,
                artifact_threshold_bytes=config.mcp.artifact_threshold_bytes,
                max_result_bytes=config.mcp.max_result_bytes,
                max_artifact_bytes=config.mcp.max_artifact_bytes,
            )
            if not discovered:
                continue
            owned = candidate_resources.pop_all()
            resources.push_async_callback(owned.aclose)
            bindings.extend(discovered)
    return tuple(bindings)


__all__ = [
    "RealTaskHandlersUnavailableError",
    "TaskWorkerDependencies",
    "build_task_worker_dependencies",
]
