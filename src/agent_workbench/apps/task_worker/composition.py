"""Assembly of the independently deployed, single-Worker Task process.

The API may submit a Task but must not run it as a background coroutine: a
process restart would otherwise discard the executor that owns recovery.  This
composition root uses the same PostgreSQL engine for the registry, event log
and checkpointer, while artifacts remain the source of the immutable input.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from agent_workbench.adapters.persistence import (
    PostgresApprovalStore,
    PostgresDocumentStore,
    PostgresEventLog,
    PostgresExecutionGuardFactory,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.tools import (
    ExternalSearchTool,
    StaticToolRegistry,
    UnavailableExternalSearch,
)
from agent_workbench.adapters.tools.task_external_research import (
    GatewayExternalEvidence,
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
from agent_workbench.bootstrap.projections import TaskWorkerRuntimeConfig
from agent_workbench.bootstrap.qdrant_startup import verify_qdrant_startup
from agent_workbench.bootstrap.sparse_factory import (
    SparseEncodingUnavailable,
    build_sparse_encoder,
)
from agent_workbench.domain.runs import RunBudget
from agent_workbench.domain.tasks import TaskNodeId
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway
from agent_workbench.workers.task import TaskWorker
from agent_workbench.workflows.approval import TaskApprovalGate
from agent_workbench.workflows.demo_handlers import build_demo_v1_handlers
from agent_workbench.workflows.task_handlers import (
    TaskNodeInvocationProvider,
    TaskResearchHandlers,
    build_task_v1_handlers,
)


class RealTaskHandlersUnavailableError(RuntimeError):
    """Raised rather than running synthetic output in a production worker."""


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
    worker: TaskWorker
    http: httpx.AsyncClient | None = None
    qdrant: AsyncQdrantClient | None = None

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
        """Release the process-owned connection pool after its loop stops."""

        await self.guards.dispose()
        if self.qdrant is not None:
            await self.qdrant.close()
        if self.http is not None:
            await self.http.aclose()
        await self.engine.dispose()


def build_task_worker_dependencies(
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
    http: httpx.AsyncClient | None = None
    qdrant: AsyncQdrantClient | None = None
    if handlers is None and demo:
        handlers = build_demo_v1_handlers()

    engine = create_query_engine(
        config.database.dsn.get_secret_value(),
        application_name=config.database.application_name,
        statement_timeout_ms=config.database.statement_timeout_ms,
        pool_size=config.database.pool_size,
        max_overflow=config.database.max_overflow,
    )
    artifacts = LocalArtifactStore(Path(config.artifacts.local_root))
    events = PostgresEventLog(engine)
    registry = PostgresTaskRegistry(engine, events=events)
    guard_dsn = config.database.guard_dsn or config.database.dsn
    guards = PostgresExecutionGuardFactory(
        guard_dsn.get_secret_value(),
        healthcheck_seconds=float(config.database.guard_healthcheck_seconds),
        application_name=f"{config.database.application_name}-guard",
    )
    # Wired whatever the handlers are. The demo graph answers its own gate and
    # never interrupts, but a Worker that could meet an interrupt without a
    # ledger would park the Task instead of resuming it -- and the ledger costs
    # nothing to hold.
    approvals = PostgresApprovalStore(engine, events=events)
    if handlers is None:
        handlers, http, qdrant = _build_real_handlers(
            config,
            artifacts=artifacts,
            documents=PostgresDocumentStore(engine),
            events=events,
            registry=registry,
        )
        # The one node the handler factory cannot build: it has to interrupt,
        # and interrupting belongs to the workflow framework, so it is assembled
        # here from the framework-neutral gate.
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
        worker=worker,
        http=http,
        qdrant=qdrant,
    )


def _build_real_handlers(
    config: TaskWorkerRuntimeConfig,
    *,
    artifacts: LocalArtifactStore,
    documents: PostgresDocumentStore,
    events: PostgresEventLog,
    registry: PostgresTaskRegistry,
) -> tuple[Mapping[TaskNodeId, NodeHandler], httpx.AsyncClient, AsyncQdrantClient]:
    """Assemble Task evidence and model execution without a demo fallback."""

    if (
        config.model is None
        or config.qdrant is None
        or config.embedding is None
        or config.retrieval is None
        or config.runtime is None
    ):
        raise RealTaskHandlersUnavailableError(
            "real Task handlers require model, qdrant, embedding, retrieval and "
            "runtime configuration; use project_task_worker or --demo"
        )

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
    retrieval = RetrievalService(
        embedder=embedder,
        index=QdrantVectorIndex(qdrant, collection=config.qdrant.read_alias),
        documents=documents,
        sparse_encoder=sparse_encoder,
    )
    evidence = EvidenceStore(artifacts)
    external_tool = ExternalSearchTool(
        ExternalResearchService(
            search=UnavailableExternalSearch(),
            evidence=evidence,
        )
    )
    tool_registry = StaticToolRegistry((external_tool.binding(),))
    gateway = ToolGateway(
        registry=tool_registry,
        policy=EnvelopePolicyEngine(registry=tool_registry),
    )
    policy_identity = (
        f"{config.task.policy_revision}:{config.task.policy_fingerprint[:16]}"
    )
    executor = ClaudeLikeAgentRuntime(
        model=model,
        gateway=gateway,
        policy_identity=policy_identity,
        model_label=main_profile.model_id,
        model_timeout_seconds=config.runtime.model_timeout_seconds,
        max_parallel_read_tools=config.runtime.max_parallel_read_tools,
    )
    invocations = TaskNodeInvocationProvider(
        registry=registry,
        budget=RunBudget(
            max_steps=config.runtime.max_steps,
            max_tool_calls=config.runtime.max_tool_calls,
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
    )
    return handlers, http, qdrant


__all__ = [
    "RealTaskHandlersUnavailableError",
    "TaskWorkerDependencies",
    "build_task_worker_dependencies",
]
