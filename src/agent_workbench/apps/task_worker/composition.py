"""Assembly of the independently deployed, single-Worker Task process.

The API may submit a Task but must not run it as a background coroutine: a
process restart would otherwise discard the executor that owns recovery.  This
composition root uses the same PostgreSQL engine for the registry, event log
and checkpointer, while artifacts remain the source of the immutable input.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.concurrency import BlockingCallRunner
from agent_workbench.adapters.delegation import EventDelegationChannel
from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.langgraph import (
    LangGraphTaskWorkflow,
    PostgresCheckpointSaver,
    build_approval_node,
)
from agent_workbench.adapters.langgraph.workflow import (
    GRAPH_DEFINITIONS,
    GraphDefinition,
    NodeHandler,
)
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
from agent_workbench.adapters.tools.delegate import DelegateTool
from agent_workbench.adapters.tools.mcp_workspace import bind_results_into_workspace
from agent_workbench.adapters.tools.sandbox import SandboxRunTool
from agent_workbench.adapters.tools.task_export import GatewayReportExport
from agent_workbench.adapters.tools.task_external_research import (
    GatewayExternalEvidence,
)
from agent_workbench.adapters.tools.workspace import (
    WorkspaceEditTool,
    WorkspaceGrepTool,
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceWriteTool,
)
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.delegation import (
    DeferredExecutor,
    DelegationScope,
    DelegationScopingExecutor,
)
from agent_workbench.application.retrieval import RetrievalService
from agent_workbench.application.sub_agents import DEFAULT_SUB_AGENTS
from agent_workbench.application.task_inputs import TaskInputStore
from agent_workbench.application.task_research import (
    EvidenceStore,
    ExternalResearchService,
    InternalResearchService,
)
from agent_workbench.application.workspace_scope import WorkspaceScope
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
from agent_workbench.domain.runs import AgentRunRequest, RunBudget
from agent_workbench.domain.sandbox import SANDBOX_REMOTE_TOOL
from agent_workbench.domain.tasks import TaskNodeId
from agent_workbench.domain.tools import ToolName
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.delegation import DelegationChannel
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.research import ExternalSearchPort
from agent_workbench.ports.task_workflow import GraphVersion
from agent_workbench.ports.tools import ToolBinding, ToolRegistry
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolExecutor, ToolGateway
from agent_workbench.workers.task import TaskWorker
from agent_workbench.workflows.agent_profiles import (
    AGENT_ROSTERS,
    AgentProfile,
    DynamicToolSource,
    assert_within_static_limit,
    profile_with_dynamic_tools,
)
from agent_workbench.workflows.approval import TaskApprovalGate
from agent_workbench.workflows.demo_handlers import build_demo_handlers
from agent_workbench.workflows.execution_scope import TaskExecutionScope
from agent_workbench.workflows.general_graph import GRAPH_VERSION_V2
from agent_workbench.workflows.task_handlers import (
    BoundedParallelExecutor,
    BudgetedAgentExecutor,
    TaskExportHandlers,
    TaskNodeInvocationProvider,
    TaskResearchHandlers,
    build_task_handlers,
)


class RealTaskHandlersUnavailableError(RuntimeError):
    """Raised rather than running synthetic output in a production worker."""


class LedgeredToolInAgentProfileError(RuntimeError):
    """A profile names a tool whose effects are recorded in the ledger.

    Raised at assembly, and for the same reason ``AgentProfileLimitError`` is:
    the offending fact is a statement about this deployment's wiring, not about
    any one Task, so the process that carries it should not start. See
    ``_assert_no_profile_offers_a_ledgered_tool``.
    """


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RealHandlers:
    """What real assembly produced, past the point of returning a 7-tuple."""

    handlers: Mapping[TaskNodeId, NodeHandler]
    http: httpx.AsyncClient
    #: Absent when this Worker assembled no retrieval, which is now allowed.
    qdrant: AsyncQdrantClient | None
    research_http: httpx.AsyncClient | None
    mcp_tool_names: tuple[ToolName, ...]
    dynamic_tools: dict[DynamicToolSource, tuple[ToolName, ...]]
    tool_names: tuple[ToolName, ...]
    #: Which graphs this Worker will run. Every graph normally; only v2 when
    #: there is no retrieval to ground v1's research nodes with. Restricting
    #: the registry rather than the claim is deliberate -- an unregistered
    #: version already raises `WorkflowGraphVersionMismatchError`, so a v1 Task
    #: that reaches such a Worker fails loudly with the version named, instead
    #: of running research nodes that would fall back to plain model calls and
    #: return their output as though it were retrieved evidence.
    graphs: Mapping[GraphVersion, GraphDefinition] = field(
        default_factory=lambda: GRAPH_DEFINITIONS
    )
    #: Why this Worker grounds nothing, or None when it does. Carried out of
    #: assembly rather than left behind in a log line for the same reason
    #: `graphs` is: the difference between a Worker that chose not to retrieve
    #: and one that could not is a deploy-time fact, and a process serving half
    #: of what it was configured for has to be able to say which half.
    grounding_unavailable: str | None = None


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
    #: What each audience actually got, keyed by the value the profiles declare.
    #: An audience is absent when this deployment configured nothing for it, and
    #: also when it configured something that did not answer at startup -- an
    #: MCP server that was down, a sandbox with no container runtime. The
    #: profiles are widened from this rather than from configuration, so the two
    #: cases are the same to everything downstream.
    dynamic_tools: Mapping[DynamicToolSource, tuple[ToolName, ...]] = field(
        default_factory=lambda: cast(
            "dict[DynamicToolSource, tuple[ToolName, ...]]", {}
        )
    )
    # Every tool this process registered, exposed for the same reason
    # `handlers` is: which tools a deployment assembled is a deploy-time fact,
    # and the difference between a registry that satisfies the agent profiles
    # and one that is missing three of them is otherwise invisible until a
    # Task reaches the node that asks for them.
    tool_names: tuple[ToolName, ...] = ()
    #: Why this process registered no retrieval, or None when it did. Named
    #: after `ApiDependencies.rag_unavailable` on purpose: both processes lose
    #: retrieval for the same reason -- no embedding runtime on this machine --
    #: and one vocabulary means an operator who has read either one has read
    #: the other. `None` for the demo and adapter-injection paths, which
    #: assemble no retrieval and were never asked to.
    grounding_unavailable: str | None = None

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
        handlers = build_demo_handlers()

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
        # ADR-042. One pool per process: every blocking adapter in this process
        # draws from it, so the bound is a ceiling rather than three private ones.
        blocking = BlockingCallRunner(
            slots=config.blocking_calls.slots,
            queue_timeout_seconds=config.blocking_calls.queue_timeout_seconds,
        )
        resources.callback(blocking.close)
        artifacts = LocalArtifactStore(
            Path(config.artifacts.local_root), runner=blocking
        )
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
                blocking=blocking,
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
        inputs = TaskInputStore(
            artifacts,
            export_requires_approval=config.task.export_requires_approval,
        )
        # What this process will build. Normally every graph; only v2 when the
        # real assembly found no retrieval to ground v1's research nodes with.
        # The same mapping feeds `buildable_versions` below, so what the Worker
        # advertises and what it can actually compile cannot disagree.
        graphs = GRAPH_DEFINITIONS if assembled is None else assembled.graphs
        if config.task.graph_version not in graphs:
            # A note, not a refusal, and deliberately not a filter either. The
            # value is the *submission* default -- the API hands it to
            # `TaskService`, and the API is a different process that may well
            # be able to build what this one cannot. What makes it worth saying
            # is that the disagreement is otherwise invisible from both sides:
            # every submission that names no shape parks as
            # `waiting_migration`, which looks like a queue that stopped
            # draining rather than like a configuration to change.
            logger.warning(
                "task_worker_default_graph_not_buildable",
                extra={
                    "configured_graph_version": config.task.graph_version,
                    "buildable_graph_versions": sorted(graphs),
                },
            )
        workflow = LangGraphTaskWorkflow(
            handlers=handlers,
            checkpointer=PostgresCheckpointSaver(engine, require_fence=True),
            graphs=graphs,
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
            buildable_versions=tuple(graphs),
            worker_id=config.worker_id,
            lease_seconds=config.task.lease_seconds,
            heartbeat_seconds=config.task.heartbeat_seconds,
            # ADR-041. Derived here rather than configured: how late a heartbeat
            # may be before this Worker stops claiming to be alive is a property
            # of the heartbeat interval, not something a deployment should be
            # able to tune past ``lease_seconds`` -- which would turn the
            # self-check off without any validator noticing.
            abort_lag_seconds=config.task.heartbeat_seconds,
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
            dynamic_tools={} if assembled is None else assembled.dynamic_tools,
            tool_names=() if assembled is None else assembled.tool_names,
            grounding_unavailable=(
                None if assembled is None else assembled.grounding_unavailable
            ),
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
    blocking: BlockingCallRunner,
    documents: PostgresDocumentStore,
    events: PostgresEventLog,
    registry: PostgresTaskRegistry,
    ledger: PostgresToolExecutionLedger,
    scope: TaskExecutionScope,
    resources: AsyncExitStack,
) -> _RealHandlers:
    """Assemble Task evidence and model execution without a demo fallback.

    Retrieval is optional here, and the three sections that configure it are
    optional with it. v2's general graph has no research branch at all -- it
    runs one working node with the workspace and MCP tools -- so requiring an
    embedding runtime, a Qdrant client and a retrieval funnel to run a Task
    that names no knowledge base was charging every deployment for a capability
    half of them never reach.

    What is *not* done is running v1 without retrieval. Its research nodes fall
    back to plain artifact-producing model calls when handed no research
    handlers, which would write model output into `evidence_refs` and let the
    report cite it as retrieved evidence. So a Worker with no retrieval
    registers only v2, and a v1 Task that reaches it parks for migration with
    the version named -- see `graphs` on `_RealHandlers`.

    What *reaches* that shape is worth naming, because it is not what the
    paragraph above reads like. Outside hand-built configurations it is never
    "this deployment configured no retrieval": `project_task_worker` has no way
    to express that and fills all three sections in. It is an embedding runtime
    that would not load -- the optional extra is not installed, or its weights
    are not on this machine -- which is precisely the deployment that has to
    keep running ordinary Work.
    """

    if config.model is None or config.runtime is None or config.multi_agent is None:
        raise RealTaskHandlersUnavailableError(
            "real Task handlers require model, runtime and multi-agent "
            "configuration; use project_task_worker or --demo"
        )
    grounds_tasks = (
        config.qdrant is not None
        and config.embedding is not None
        and config.retrieval is not None
    )

    # Before anything is built, and over every graph's roster rather than one.
    # The limit describes a compiled graph's shape, so a graph that outgrew
    # what this deployment budgeted for must stop the process rather than be
    # discovered one expensive Task at a time.
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

    embedder = (
        build_embedder(config.embedding, runner=blocking)
        if grounds_tasks and config.embedding is not None
        else EmbeddingUnavailable(reason="this Worker configured no retrieval")
    )
    grounding_unavailable: str | None = None
    if grounds_tasks and isinstance(embedder, EmbeddingUnavailable):
        # This used to raise, on the grounds that a deployment which asked for
        # grounded Tasks must not be started silently downgraded. The argument
        # was right about the "silently"; its premise never held, because no
        # deployment can decline. `project_task_worker` builds `qdrant`,
        # `embedding` and `retrieval` unconditionally -- `settings.rag` and
        # `settings.qdrant` are required sections that no configuration file
        # can withhold -- so `grounds_tasks` is true for every projected
        # Worker, and the refusal fired on the one case it was not written
        # for: a machine with no embedding extra and no weights, where it
        # stopped a Task that names no knowledge base and touches no index
        # from running at all. That made the v2-only Worker this function's
        # docstring describes an unreachable branch.
        #
        # So the downgrade happens and the "silently" is what goes. This line
        # and `grounding_unavailable` are its two exits -- the same pair the
        # API process was given when a missing embedder was decided to cost
        # retrieval rather than the whole service (`ApiDependencies`).
        #
        # The sentence is composed here rather than forwarded whole:
        # `build_embedder`'s missing-weights reason ends in "this process
        # serves everything except chat", which is true of the API and
        # misleading in a Worker, whose loss is v1 and not chat.
        grounding_unavailable = embedder.reason
        logger.warning(
            "task_worker_grounding_unavailable",
            extra={
                "grounding_error": embedder.reason,
                "registered_graphs": [GRAPH_VERSION_V2],
            },
        )
    grounds_tasks = grounds_tasks and not isinstance(embedder, EmbeddingUnavailable)

    # build_model only validates configuration while constructing its adapter;
    # no provider connection is opened here. The client is a process resource
    # and is returned to the dependency owner for deterministic shutdown.
    http = httpx.AsyncClient(timeout=main_profile.timeout_seconds)
    resources.push_async_callback(http.aclose)
    from agent_workbench.bootstrap.model_factory import build_model

    model = build_model(config.model, client=http)

    qdrant: AsyncQdrantClient | None = None
    retrieval: RetrievalService | None = None
    # The embedding conditions are already implied by `grounds_tasks`: it is only
    # still true here because the refusal above did not fire, which took both. They
    # are restated because `grounds_tasks` is a bool, and a bool carries no
    # narrowing -- without them the checker sees `EmbeddingConfig | None` and
    # `EmbeddingPort | EmbeddingUnavailable` reaching parameters that accept
    # neither. Restating beats asserting: an `assert` would state the same fact as
    # a runtime claim, and this one is already decided.
    if (
        grounds_tasks
        and config.embedding is not None
        and not isinstance(embedder, EmbeddingUnavailable)
        and config.qdrant is not None
        and config.retrieval is not None
    ):
        built_sparse = build_sparse_encoder(config.embedding, runner=blocking)
        sparse_encoder = (
            None
            if isinstance(built_sparse, SparseEncodingUnavailable)
            else built_sparse
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
        WorkspaceEditTool(workspace_scope).binding(),
        WorkspaceGrepTool(workspace_scope).binding(),
    )
    discovered_mcp = await _build_mcp_bindings(
        config,
        artifacts=artifacts,
        resources=resources,
        workspace_scope=workspace_scope,
    )
    sandbox_binding = await _build_sandbox_binding(
        config,
        scope=workspace_scope,
        resources=resources,
    )
    sandbox_bindings = () if sandbox_binding is None else (sandbox_binding,)
    mcp_bindings = tuple(binding for binding, _ in discovered_mcp)
    # One mapping, three audiences. The sandbox is not a special case here: it
    # is a catalog keyed by the value the writer profile declares, exactly like
    # the two an MCP server can serve.
    dynamic_tools = _by_audience(discovered_mcp)
    if sandbox_bindings:
        dynamic_tools["sandbox"] = tuple(
            binding.spec.name for binding in sandbox_bindings
        )
    # ADR-082. Assembled before the registry and bound after the runtime,
    # because the cycle is real: the tool that starts a run has to be in the
    # registry the run's gateway reads, and the runtime it calls is built from
    # that gateway. `DeferredExecutor` is the one place that knot is tied, and
    # it fails loudly rather than silently if a process forgets to tie it.
    delegation_scope = DelegationScope()
    delegation_executor = DeferredExecutor()
    # Narrowed to what this Worker registered, never to what the deployment
    # configured. `permitted_child_tools` intersects with the Task's envelope,
    # and an envelope is frozen from configuration -- so it can name a tool this
    # process failed to assemble, and `advertise` raises for those. Same rule as
    # `profile_with_dynamic_tools`, one layer down.
    sub_agents = (
        DEFAULT_SUB_AGENTS.narrowed_to(
            [
                external_tool.binding().spec.name,
                *(binding.spec.name for binding in workspace_bindings),
                *(binding.spec.name for binding in mcp_bindings),
                *(binding.spec.name for binding in sandbox_bindings),
            ]
        )
        if config.multi_agent.delegation_enabled
        else DEFAULT_SUB_AGENTS
    )
    delegate_bindings = (
        (
            DelegateTool(
                executor=delegation_executor,
                catalogue=sub_agents,
                scope=delegation_scope,
            ).binding(),
        )
        if config.multi_agent.delegation_enabled
        else ()
    )
    if delegate_bindings:
        # A fourth audience on the same footing (ADR-082). Declared here rather
        # than on a profile's static tool list so that a deployment with
        # delegation off leaves every profile exactly as it was.
        dynamic_tools["delegation"] = tuple(
            binding.spec.name for binding in delegate_bindings
        )
    tool_registry = StaticToolRegistry(
        (
            external_tool.binding(),
            export_tool.binding(),
            *workspace_bindings,
            *mcp_bindings,
            *sandbox_bindings,
            *delegate_bindings,
        )
    )
    # The one place both halves of the question are in scope (ADR-075 §4).
    _assert_no_profile_offers_a_ledgered_tool(
        tool_registry, dynamic_tools=dynamic_tools, rosters=AGENT_ROSTERS
    )
    gateway = ToolGateway(
        registry=tool_registry,
        policy=EnvelopePolicyEngine(registry=tool_registry),
        # Required, not optional: the gateway refuses to assemble around a
        # ledgered tool with nowhere to record it, and export_artifact is one.
        ledger=ledger,
        record_step_inputs=config.runtime.record_step_inputs,
        # Without this the deployment's own tool ceiling reaches nothing: the
        # gateway would build a default executor that knows only what each tool
        # declares, which is how `runtime.tool_timeout_seconds` came to be a
        # setting no code read.
        executor=ToolExecutor(
            deployment_ceiling_seconds=config.runtime.tool_timeout_seconds
        ),
    )
    policy_identity = (
        f"{config.task.policy_revision}:{config.task.policy_fingerprint[:16]}"
    )
    # ADR-081 makes one call per run under `[model.compact]`, and the adapter
    # dispatches on the profile name -- so that call is recorded under the
    # model it actually reached, not under this process's main label. The two
    # differ in every profile that matters: `config.code-local.toml` and
    # `config.demo-local.toml` both run `deepseek-v4-flash` as main against
    # `deepseek-chat` as compact.
    compact_profile = config.model.profiles.get("compact")
    compact_model_label = (
        compact_profile.model_id if compact_profile is not None else None
    )
    # Named, because two stacks are built around the same loop (ADR-082): the
    # one the graph's nodes run through and the one delegated runs run through.
    # One runtime, two pools -- a second runtime would be a second set of
    # prices, a second context window and a second thing to keep in step.
    #
    # And therefore one compact label as well. A delegated run compacts through
    # the same `[model.compact]` profile its parent does, so recording it under
    # a different label would put two names on one model's calls.
    runtime = ClaudeLikeAgentRuntime(
        model=model,
        gateway=gateway,
        policy_identity=policy_identity,
        model_label=main_profile.model_id,
        compact_model_label=compact_model_label,
        model_timeout_seconds=config.runtime.model_timeout_seconds,
        max_parallel_read_tools=config.runtime.max_parallel_read_tools,
        record_step_inputs=config.runtime.record_step_inputs,
        prices=main_profile.prices,
        # ADR-0080. From the main profile, because that is the profile this
        # runtime's calls are made under -- the same reason `prices` and
        # `model_label` come from it.
        context_window_tokens=main_profile.context_window_tokens,
        context_soft_limit_ratio=config.runtime.context_soft_limit_ratio,
        compaction_enabled=config.runtime.context_compaction_enabled,
    )

    def _delegation_channel(request: AgentRunRequest) -> DelegationChannel:
        """Where a run announces the children it starts: on its own scope."""

        return EventDelegationChannel(
            log=events,
            parent_scope=EventScope(
                stream_id=request.stream_id,
                run_id=request.trace.agent_run_id,
                task_id=request.trace.task_id,
                graph_node_id=request.trace.graph_node_id,
            ),
        )

    # Read out of the projection here rather than inside `_scoped`: the guard
    # that proves `config.multi_agent` is present is at the top of this
    # function, and it does not narrow inside a nested one.
    delegation_on = config.multi_agent.delegation_enabled
    delegation_depth = config.multi_agent.max_delegation_depth
    delegation_children = config.multi_agent.max_children_per_run

    def _scoped(inner: AgentExecutor) -> AgentExecutor:
        """Wrap an executor so every run it performs may delegate.

        Applied to both stacks. On the parent stack it gives a graph node a
        context to delegate from; on the child stack it is what makes a
        delegated run's own depth one greater than its parent's, since the
        ContextVar still holds the parent's context at that moment.
        """

        if not delegation_on:
            return inner
        return DelegationScopingExecutor(
            inner,
            scope=delegation_scope,
            channel_for=_delegation_channel,
            max_depth=delegation_depth,
            max_children=delegation_children,
        )

    executor = BoundedParallelExecutor(
        runtime,
        max_parallel=config.multi_agent.max_parallel_agent_invocations,
    )
    # ADR-040. Outside the parallelism bound on purpose: the Registry round trip
    # that charges the Task happens before a concurrency slot is taken, rather
    # than while one is held. Records only -- nothing refuses on the count yet.
    executor = BudgetedAgentExecutor(executor, registry=registry, scope=scope)
    executor = _scoped(executor)
    if config.multi_agent.delegation_enabled:
        # The child stack. Same runtime, same charging, **its own semaphore**.
        # Sharing the parent's pool deadlocks: `BoundedParallelExecutor` holds
        # its slot for the whole invocation, so a parent waiting inside a tool
        # call holds one until its child finishes, and the child is queued for a
        # slot that only the parent's return can free.
        delegation_executor.bind(
            _scoped(
                BudgetedAgentExecutor(
                    BoundedParallelExecutor(
                        runtime,
                        max_parallel=(
                            config.multi_agent.max_parallel_child_invocations
                        ),
                    ),
                    registry=registry,
                    scope=scope,
                )
            )
        )
    invocations = TaskNodeInvocationProvider(
        registry=registry,
        budget=RunBudget(
            max_steps=config.runtime.max_steps,
            max_tool_calls=config.runtime.max_tool_calls,
            # One agent's ceiling, not the Task's. Without it a single
            # invocation could spend everything the Task was allowed.
            max_total_tokens=config.multi_agent.max_tokens_per_agent_invocation,
            # Both absent unless this deployment configured them (ADR-030).
            # The deadline is not set here -- it is an instant, and one built
            # at composition would be shared by every invocation for the life
            # of the process. The provider stamps it per attempt.
            max_cost_micro_usd=(
                config.multi_agent.max_cost_micro_usd_per_agent_invocation
            ),
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
        max_seconds_per_invocation=(
            config.multi_agent.max_seconds_per_agent_invocation
        ),
    )
    # Every node either graph runs, in one mapping. The adapter compiles each
    # graph by looking its own nodes up here, so a Worker that assembled only
    # one graph's handlers would not fail on the other -- it would run it with
    # pass-throughs (ADR-031 §3: the cross-cutting thing has to hold on both).
    handlers = build_task_handlers(
        executor=executor,
        artifacts=artifacts,
        invocations=invocations,
        research=(
            TaskResearchHandlers(
                internal=InternalResearchService(
                    retrieval=retrieval, evidence=evidence
                ),
                evidence=evidence,
                external=GatewayExternalEvidence(gateway),
                policy_identity=policy_identity,
            )
            if retrieval is not None
            else None
        ),
        export=TaskExportHandlers(
            export=GatewayReportExport(gateway=gateway, ledger=ledger),
            policy_identity=policy_identity,
        ),
        dynamic_tools=dynamic_tools,
        workspace_scope=workspace_scope,
    )
    # v2 only when nothing can ground v1. Not a claim-time filter and not a
    # failure: an unregistered version parks the Task for migration, so it
    # waits for a Worker that can run it rather than being run wrong here.
    graphs = (
        GRAPH_DEFINITIONS
        if retrieval is not None
        else {
            version: definition
            for version, definition in GRAPH_DEFINITIONS.items()
            if version == GRAPH_VERSION_V2
        }
    )
    return _RealHandlers(
        handlers=handlers,
        http=http,
        qdrant=qdrant,
        research_http=research_http,
        mcp_tool_names=tuple(binding.spec.name for binding in mcp_bindings),
        dynamic_tools=dynamic_tools,
        tool_names=tuple(spec.name for spec in tool_registry.specs()),
        graphs=graphs,
        grounding_unavailable=grounding_unavailable,
    )


def _assert_no_profile_offers_a_ledgered_tool(
    registry: ToolRegistry,
    *,
    dynamic_tools: Mapping[DynamicToolSource, Sequence[ToolName]],
    rosters: Sequence[tuple[str, tuple[AgentProfile, ...]]],
) -> None:
    """Refuse to start a Worker that would offer a ledgered tool to a model.

    ADR-075 §2 is a rule about *position*: a ledgered effect is issued by a
    node that meant it -- the way ``export_artifact`` is -- and is never put in
    front of a model as a choice, because nothing in a run distinguishes "the
    same intent, replayed" from "a new intent that happens to look identical",
    which is what the ledger's key would have to answer for.

    ``ToolGateway.advertise`` already refuses such a tool, and keeps doing so:
    it is the check that closes the path, and it sees names this one cannot
    (a profile widened by something other than assembly, a caller that builds
    its own request). But it runs once per agent run, and what it raises the
    runtime turns into a failed run. That is the wrong shape for a *wiring*
    mistake: the deployment stays up and fails one node forever, which reads
    like a broken tool rather than a profile that should never have been
    written. The precedent for the right shape is one file over, in
    ``ToolGateway.__init__``, which refuses to assemble around a ledgered tool
    with no ledger to record it in -- a process that cannot honour the protocol
    should not start.

    The gateway could not make this check for want of one of its two halves:
    profiles live in ``workflows/agent_profiles.py`` and nothing hands them to
    it. This function is here because here is the first place both the
    assembled registry and the rosters are in scope.

    Two details that are not incidental:

    * the profiles are the **widened** ones, not the declarations. A profile's
      static ``tool_names`` is only half of what a run is offered; the other
      half arrives from the dynamic catalogs this Worker just assembled, and
      that half is the one a future MCP server could quietly change. Checking
      the declarations alone would pass a Worker that then advertised a
      ledgered MCP tool to its writer;
    * every roster is walked, not only the graphs this Worker will register.
      ``graphs`` narrows to v2 when nothing can ground v1, and that narrowing
      turns on whether an embedding runtime loaded on this machine -- so a
      check that followed it would let the same wiring mistake start on one
      box and stop on the next. ``rosters`` is a required argument rather than
      a bound default for that reason: which rosters this refuses over is the
      caller's statement, and a default evaluated at import would also be one
      no test could replace.

    Nothing in this repository trips it today, and that is by construction, not
    luck: ADR-025 §2.6 pins every MCP binding to ``idempotency="safe"``, which
    ``ToolBinding`` refuses to combine with an operation key, and the only
    ledgered tool in the repository is issued by ``export`` and named in no
    profile.
    A guardrail that fires on nothing is what it looks like to have arrived
    before the mistake did.
    """

    offenders = sorted(
        f"{version}/{profile.name} -> {name}"
        for version, profiles in rosters
        for profile in profiles
        for name in profile_with_dynamic_tools(profile, dynamic_tools).tool_names
        if (binding := registry.get(name)) is not None
        and binding.operation_key is not None
    )
    if offenders:
        raise LedgeredToolInAgentProfileError(
            "these agent profiles name tools that record external effects, "
            "which are issued by a graph node and never offered to a model: "
            + ", ".join(offenders)
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


def _by_audience(
    discovered: tuple[tuple[ToolBinding, DynamicToolSource], ...],
) -> dict[DynamicToolSource, tuple[ToolName, ...]]:
    """Which names each audience actually got, from what was registered.

    Built from the bindings rather than from the configuration: a server that
    was unreachable at startup contributes nothing here, and the profile is
    widened from this. Widening it from configuration instead would name a tool
    the gateway cannot resolve, which is a failing node rather than a missing
    capability.
    """

    catalogs: dict[DynamicToolSource, list[ToolName]] = {}
    for binding, audience in discovered:
        catalogs.setdefault(audience, []).append(binding.spec.name)
    return {audience: tuple(names) for audience, names in catalogs.items()}


async def _build_mcp_bindings(
    config: TaskWorkerRuntimeConfig,
    *,
    artifacts: LocalArtifactStore,
    resources: AsyncExitStack,
    workspace_scope: WorkspaceScope,
) -> tuple[tuple[ToolBinding, DynamicToolSource], ...]:
    """Every discovered binding, paired with the audience its server declared.

    Paired rather than grouped here so the caller can build both the flat
    registry and the per-audience catalog from one traversal, and so a server
    that yielded nothing cannot silently contribute an empty audience.

    Every one of them is wrapped so a file it returns is bound into the working
    set. Wrapped here rather than per server, because the reason has nothing to
    do with which server it is: an artifact no manifest names is invisible to
    the node that judges the work, whatever produced it.
    """

    if config.mcp is None:
        return ()

    bindings: list[tuple[ToolBinding, DynamicToolSource]] = []
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
            bindings.extend(
                (bind_results_into_workspace(binding, workspace_scope), server.audience)
                for binding in discovered
            )
    return tuple(bindings)


__all__ = [
    "RealTaskHandlersUnavailableError",
    "TaskWorkerDependencies",
    "build_task_worker_dependencies",
]
