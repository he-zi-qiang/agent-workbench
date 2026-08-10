"""Assembling the API's dependencies from validated settings, once.

Everything the routes use is built here, at startup, from a Settings object
that has already been validated. Routes receive finished objects; they never
read configuration, and they never construct an engine or a store of their own.
That is what keeps "which database does this endpoint talk to" a question with
one answer.

The deployment scope decides whether this process may serve at all. A scope of
``remote`` with a development identity resolver would be an authenticated-
looking service that authenticates nothing, so it refuses to assemble instead.

Chat is the one capability that may be absent. Its embedder needs an optional
runtime of several gigabytes, and requiring that for uploads, artifacts and
health checks would be charging every deployment for a feature it may not use.
So the process assembles what it can, records why anything is missing, and the
application registers no chat route rather than one that cannot answer.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.persistence import (
    PostgresApprovalStore,
    PostgresChatExpirationCoordinator,
    PostgresChatReleaseCoordinator,
    PostgresConversationStore,
    PostgresDocumentStore,
    PostgresEventLog,
    PostgresKnowledgeBaseStore,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.research import DeepSeekWebSearch
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.adapters.tools.knowledge_search import (
    TOOL_NAME as KNOWLEDGE_SEARCH,
)
from agent_workbench.adapters.tools.knowledge_search import KnowledgeSearchTool
from agent_workbench.adapters.tools.web_search import (
    TOOL_NAME as WEB_SEARCH_TOOL_NAME,
)
from agent_workbench.adapters.tools.web_search import (
    WebSearchTool,
)
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.approvals import ApprovalService
from agent_workbench.application.chat import REFUSAL, ChatService
from agent_workbench.application.chat_execution import (
    WEB_DIRECT_SYSTEM_PROMPT,
    WEB_FALLBACK_SYSTEM_PROMPT,
    AgenticExecution,
    AnswerModeSelector,
    FixedTwoStepExecution,
    RetrievalJournal,
    RoutedExecution,
    TurnExecution,
    UngroundedExecution,
    WebSearchJournal,
)
from agent_workbench.application.chat_recovery import (
    ChatPendingReleaseRecovery,
    ChatTurnReaper,
)
from agent_workbench.application.knowledge_bases import KnowledgeBaseService
from agent_workbench.application.retrieval import RetrievalService
from agent_workbench.application.task_inputs import TaskInputService, TaskInputStore
from agent_workbench.application.tasks import SubmittedSemantics, TaskService
from agent_workbench.application.uploads import UploadService
from agent_workbench.apps.api.identity import HeaderPrincipalResolver
from agent_workbench.bootstrap.embedding_factory import (
    EmbeddingUnavailable,
    build_embedder,
)
from agent_workbench.bootstrap.encoder_warmup import warm_encoders
from agent_workbench.bootstrap.model_factory import (
    ModelNotConfiguredError,
    build_model,
)
from agent_workbench.bootstrap.network import is_loopback_bind_address
from agent_workbench.bootstrap.projections import ApiRuntimeConfig, ResearchConfig
from agent_workbench.bootstrap.qdrant_startup import verify_qdrant_startup
from agent_workbench.bootstrap.reranker_factory import (
    RerankerUnavailable,
    build_reranker,
)
from agent_workbench.bootstrap.retrieval_factory import build_candidate_retriever
from agent_workbench.bootstrap.sparse_factory import (
    SparseEncodingUnavailable,
    build_sparse_encoder,
)
from agent_workbench.bootstrap.telemetry_factory import (
    AssembledTelemetry,
    build_telemetry,
)
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.documents import DocumentStore
from agent_workbench.ports.event_log import EventLogPort, EventScope
from agent_workbench.ports.telemetry import Telemetry
from agent_workbench.ports.tools import ToolBinding
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway


class InsecureDeploymentError(RuntimeError):
    """A deployment asked to serve remotely without a real identity provider."""


class RerankerRequiredError(RuntimeError):
    """A shape that decides by relevance was configured without a relevance model."""


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    """Everything the routes need, assembled once at startup."""

    config: ApiRuntimeConfig
    engine: AsyncEngine
    documents: DocumentStore
    artifacts: ArtifactStore
    uploads: UploadService
    knowledge_bases: KnowledgeBaseService
    principals: HeaderPrincipalResolver
    # Absent when the optional embedding runtime is not installed. The reason
    # is kept beside it so startup can say so once, in words, instead of
    # leaving a route to fail per request.
    chat: ChatService | None
    chat_reaper: ChatTurnReaper | None
    chat_pending_recovery: ChatPendingReleaseRecovery | None
    chat_unavailable: str | None
    # Chat still serves without one, so this is a quality note rather than a
    # missing capability. Recorded because an unreranked process is
    # indistinguishable from a reranked one at the endpoint, and an ablation
    # written against a silently unreranked process would credit the difference
    # to the model.
    reranker_unavailable: str | None
    # Dense Chat remains useful when the optional lexical projection is
    # missing, but the process records the downgrade so an evaluation cannot
    # accidentally label the run "hybrid".
    sparse_unavailable: str | None
    http: httpx.AsyncClient | None
    # The long-lived client and its read-alias index are process resources,
    # not per-request construction details. Lifespan validates them before
    # routes are served and dispose closes the same client on every exit path.
    qdrant: AsyncQdrantClient | None
    vector_index: QdrantVectorIndex | None
    # Held so startup can warm them. Absent when this process serves no chat,
    # which is also when nothing would retrieve.
    encoders: tuple[object, ...]
    # Present whenever this process can retrieve, which is no longer the same
    # question as whether it can chat.
    retrieval: RetrievalService | None
    events: EventLogPort
    # Assembled once per process. Records nothing when no collector is
    # configured, and never lets one break a request either way.
    telemetry: AssembledTelemetry
    task_service: TaskService
    task_inputs: TaskInputService
    # The human half of a Task. Assembled unconditionally, like the Task
    # service: an API that can open a Task must be able to answer the
    # approval that Task stops on, or the Task has no way forward.
    approvals: ApprovalService

    @property
    def max_control_request_body_bytes(self) -> int:
        return self.config.max_control_request_body_bytes

    @property
    def max_artifact_bytes(self) -> int:
        return self.config.artifacts.max_artifact_bytes

    @property
    def serves_chat(self) -> bool:
        return self.chat is not None

    @property
    def serves_search(self) -> bool:
        """Retrieval is servable without a model; chat is not."""

        return self.retrieval is not None

    def sink_for(self, *, stream_id: str, run_id: str) -> ScopedEventSink:
        """The sink one run writes into.

        A stream per session and a run per turn: a subscriber follows the
        session and resumes from wherever it left off, while each turn stays
        identifiable inside it.
        """

        return ScopedEventSink(
            log=self.events,
            scope=EventScope(stream_id=stream_id, run_id=run_id),
        )

    async def dispose(self) -> None:
        # Flushed first: a batch span processor holds the tail of every run,
        # and that tail is the part somebody is usually looking for.
        await self.telemetry.dispose()
        if self.chat is not None:
            await self.chat.drain_cleanup(
                timeout_seconds=self.config.shutdown_grace_seconds
            )
        if self.http is not None:
            await self.http.aclose()
        if self.qdrant is not None:
            await self.qdrant.close()
        await self.engine.dispose()

    async def startup(self) -> None:
        """Check the Qdrant read path, and warm the encoders, before serving."""

        if self.qdrant is not None:
            await verify_qdrant_startup(
                self.qdrant,
                qdrant=self.config.qdrant,
                embedding=self.config.embedding,
            )
        # A cold lexical head costs ~29s on its first encode and 0.06s after.
        # Paid here, that is a slower boot; paid on the first request, it is an
        # agentic turn failing on `knowledge_search exceeded its 30s timeout`.
        await warm_encoders(*self.encoders)


def build_dependencies(
    config: ApiRuntimeConfig, *, with_chat: bool = True
) -> ApiDependencies:
    """Build the API's dependencies, or refuse to.

    ``with_chat`` exists because assembling chat loads the embedding model, and
    loading it eagerly is right for a server -- the first question should not
    pay forty seconds that every later one avoids. It is wrong for anything
    that only needs uploads or health, which is why the cost is a parameter
    rather than a surprise: a caller that does not serve chat should not be
    made to wait for a model it will never call.
    """

    if config.deployment_scope == "remote":
        # The only identity resolver that exists reads headers. Serving that
        # beyond a single machine would be an access-controlled API whose
        # access control is a request header.
        raise InsecureDeploymentError(
            "the API has no production identity provider yet; "
            "app.deployment_scope must be 'local' until one exists"
        )
    if not is_loopback_bind_address(config.host):
        # Settings rejects this too. Checked again here because a scope of
        # "local" says what a deployment calls itself, and the bind address is
        # what actually decides who can reach the header resolver -- and this
        # is the layer that chooses that resolver, so this is where refusing
        # to pair the two belongs.
        raise InsecureDeploymentError(
            f"the API resolves identity from request headers, so it may only "
            f"bind a loopback address; {config.host!r} is reachable from other "
            f"machines"
        )
    if config.artifacts.backend != "local":
        raise InsecureDeploymentError(
            f"the {config.artifacts.backend} artifact backend has no adapter yet"
        )

    engine = create_query_engine(
        config.database.dsn.get_secret_value(),
        application_name=config.database.application_name,
        statement_timeout_ms=config.database.statement_timeout_ms,
        pool_size=config.database.pool_size,
        max_overflow=config.database.max_overflow,
    )
    documents = PostgresDocumentStore(engine)
    knowledge_bases = KnowledgeBaseService(PostgresKnowledgeBaseStore(engine))
    artifacts = LocalArtifactStore(Path(config.artifacts.local_root))
    conversations = PostgresConversationStore(engine)
    releaser = PostgresChatReleaseCoordinator(engine)

    events = PostgresEventLog(engine)
    task_service = TaskService(
        registry=PostgresTaskRegistry(engine, events=events),
        events=events,
        graph_version=config.task.graph_version,
        semantics=lambda: SubmittedSemantics(
            # The projection contains deterministic values only.  Copying the
            # mapping makes every submission own its snapshot rather than
            # sharing mutable request-independent configuration state.
            run_semantics_snapshot=deepcopy(config.task.run_semantics_snapshot),
            run_semantics_revision=config.task.run_semantics_revision,
            policy_revision=config.task.policy_revision,
            policy_fingerprint=config.task.policy_fingerprint,
            authorization_envelope=config.task.default_authorization_envelope,
        ),
    )
    task_inputs = TaskInputService(
        inputs=TaskInputStore(artifacts),
        tasks=task_service,
    )

    telemetry = build_telemetry(config.observability)
    (
        chat,
        unavailable,
        http,
        qdrant,
        vector_index,
        no_reranker,
        no_sparse,
        encoders,
        retrieval,
    ) = (
        _assemble_chat(
            config,
            documents,
            conversations=conversations,
            releaser=releaser,
            telemetry=telemetry.telemetry,
        )
        if with_chat
        else (
            None,
            "chat was not requested for this process",
            None,
            None,
            None,
            None,
            None,
            (),
            None,
        )
    )
    return ApiDependencies(
        config=config,
        engine=engine,
        telemetry=telemetry,
        documents=documents,
        artifacts=artifacts,
        uploads=UploadService(
            documents=documents,
            artifacts=artifacts,
            knowledge_bases=knowledge_bases,
        ),
        knowledge_bases=knowledge_bases,
        principals=HeaderPrincipalResolver(),
        chat=chat,
        # Recovery is intentionally independent of the embedding/model stack.
        # A degraded API must still free sessions left by a previously healthy
        # process; otherwise "chat unavailable" would also mean "chat cannot
        # recover".
        chat_reaper=ChatTurnReaper(
            expiration=PostgresChatExpirationCoordinator(engine),
            poll_seconds=config.chat_recovery.reaper_poll_seconds,
            batch_size=config.chat_recovery.reaper_batch_size,
        ),
        chat_pending_recovery=ChatPendingReleaseRecovery(
            conversations=conversations,
            releaser=releaser,
            sink_for=lambda stream_id, run_id: ScopedEventSink(
                log=events,
                scope=EventScope(stream_id=stream_id, run_id=run_id),
            ),
            refusal_text=REFUSAL,
            poll_seconds=config.chat_recovery.reaper_poll_seconds,
            batch_size=config.chat_recovery.reaper_batch_size,
        ),
        chat_unavailable=unavailable,
        reranker_unavailable=no_reranker,
        sparse_unavailable=no_sparse,
        http=http,
        qdrant=qdrant,
        vector_index=vector_index,
        encoders=encoders,
        retrieval=retrieval,
        events=events,
        task_service=task_service,
        task_inputs=task_inputs,
        approvals=ApprovalService(
            approvals=PostgresApprovalStore(engine, events=events)
        ),
    )


def _assemble_chat(
    config: ApiRuntimeConfig,
    documents: PostgresDocumentStore,
    *,
    conversations: PostgresConversationStore,
    releaser: PostgresChatReleaseCoordinator,
    telemetry: Telemetry,
) -> tuple[
    ChatService | None,
    str | None,
    httpx.AsyncClient | None,
    AsyncQdrantClient | None,
    QdrantVectorIndex | None,
    str | None,
    str | None,
    tuple[object, ...],
    RetrievalService | None,
]:
    """Build the chat stack, or report the one reason it could not be built.

    The embedder is tried first because it is the only piece whose absence is
    an expected state rather than a misconfiguration. Everything after it --
    the model, the vector index -- either works or is a refusal, and refusing
    is ``build_model``'s job, not something to soften into a degraded mode.
    """

    embedder = build_embedder(config.embedding)
    if isinstance(embedder, EmbeddingUnavailable):
        return None, embedder.reason, None, None, None, None, None, (), None

    # After the embedder, because a process that cannot chat has no use for a
    # reranker and loading one would be several gigabytes spent on a capability
    # that is not being served.
    reranker = build_reranker(config.reranker)
    no_reranker = reranker.reason if isinstance(reranker, RerankerUnavailable) else None
    sparse = build_sparse_encoder(config.embedding)
    no_sparse = sparse.reason if isinstance(sparse, SparseEncodingUnavailable) else None

    qdrant = AsyncQdrantClient(
        url=config.qdrant.url,
        api_key=(
            config.qdrant.api_key.get_secret_value()
            if config.qdrant.api_key is not None
            else None
        ),
        timeout=config.qdrant.request_timeout_seconds,
    )
    # Read via the alias, never the ingestion collection. This makes an alias
    # switch affect new Chat requests without changing the write target.
    vector_index = QdrantVectorIndex(qdrant, collection=config.qdrant.read_alias)

    sparse_encoder = None if isinstance(sparse, SparseEncodingUnavailable) else sparse
    # What startup warms. Taken from what was just assembled rather than read
    # back off the retrieval service: which encoders a retriever holds is now
    # its own business -- LlamaIndex's does not expose them the way the
    # reference path did -- and warming is about the runtimes this process
    # loaded, which is a fact this scope already has.
    encoders = tuple(e for e in (embedder, sparse_encoder) if e is not None)

    retrieval = RetrievalService(
        candidate_retriever=build_candidate_retriever(
            llama_index_enabled=config.retrieval.llama_index_enabled,
            embedder=embedder,
            index=vector_index,
            sparse_encoder=sparse_encoder,
        ),
        documents=documents,
        telemetry=telemetry,
        reranker=None if isinstance(reranker, RerankerUnavailable) else reranker,
        rerank_timeout_seconds=config.reranker.timeout_seconds,
    )
    # The model comes last, and its absence costs only chat. Retrieval is
    # already assembled above and does not need a provider: a deployment with no
    # key can still index documents and search them, and saying "no chat"
    # is a smaller and truer thing to say than "no retrieval".
    #
    # `build_model` still refuses rather than returning something unusable --
    # that refusal is the behaviour worth keeping, see its module docstring.
    # What changed is only how much it takes down with it.
    client = httpx.AsyncClient(timeout=config.model.profiles["main"].timeout_seconds)
    try:
        model = build_model(config.model, client=client)
    except ModelNotConfiguredError as unconfigured:
        return (
            None,
            str(unconfigured),
            client,
            qdrant,
            vector_index,
            no_reranker,
            no_sparse,
            encoders,
            retrieval,
        )

    policy_identity = f"api-{config.deployment_scope}"
    # Which model actually answered. Without this the runtime falls back to its
    # own placeholder label and every ModelStarted this deployment writes says
    # "scripted-fake" while a real provider is being billed -- an event log that
    # disagrees with what happened, which is the one thing it may not do. The
    # Task Worker already passes this; the API did not.
    main_profile = config.model.profiles.get("main")
    model_label = (
        main_profile.model_id if main_profile is not None else config.model.provider
    )
    # Chat runs are priced by the same profile they are labelled with. Absent
    # when the deployment configured no prices, which leaves every chat spend
    # at zero and every chat cost ceiling refused -- see ClaudeLikeAgentRuntime.
    model_prices = main_profile.prices if main_profile is not None else None

    # Both no-tool shapes get the same deny-shaped runtime the fixed shape uses.
    # Written out rather than shared with a helper because the registry and the
    # policy engine are two separate reasons the model cannot reach a tool, and
    # a helper that built them together would make it look like one.
    def _web_search_tool(
        research: ResearchConfig | None, journal: WebSearchJournal
    ) -> ToolBinding | None:
        """The web tool, or nothing at all when no provider is configured.

        Reuses the model's HTTP client rather than opening a second one. The
        provider is the same service on a different path (ADR-020), the client
        is already owned and closed by this container, and a second one would
        be a second thing to leak.
        """

        if research is None:
            return None
        return WebSearchTool(
            search=DeepSeekWebSearch(
                http=client,
                api_key=research.api_key.get_secret_value(),
                model=research.model_id,
                base_url=research.base_url,
                max_uses=research.max_uses,
            ),
            # Chat has no per-run cancellation token to hand a tool here; the
            # gateway enforces the tool's own timeout, and the request dies with
            # the connection either way.
            cancellation=NullCancellationToken(),
            journal=journal,
        ).binding()

    def _tool_runtime(registry: StaticToolRegistry) -> ClaudeLikeAgentRuntime:
        """A runtime whose model may reach exactly the tools in `registry`."""

        return ClaudeLikeAgentRuntime(
            model=model,
            gateway=ToolGateway(
                registry=registry,
                policy=EnvelopePolicyEngine(registry=registry),
                record_step_inputs=config.record_step_inputs,
            ),
            policy_identity=policy_identity,
            model_label=model_label,
            record_step_inputs=config.record_step_inputs,
            prices=model_prices,
        )

    def _toolless_runtime() -> ClaudeLikeAgentRuntime:
        empty = StaticToolRegistry([])
        return ClaudeLikeAgentRuntime(
            model=model,
            gateway=ToolGateway(
                registry=empty,
                policy=EnvelopePolicyEngine(registry=empty),
                record_step_inputs=config.record_step_inputs,
            ),
            policy_identity=policy_identity,
            model_label=model_label,
            record_step_inputs=config.record_step_inputs,
            prices=model_prices,
        )

    # One tool, one journal, one runtime, shared by both evidence-free turns
    # (ADR-023). Built here rather than inside the `routed` branch because the
    # direct shape below needs the same objects: the binding writes its verdict
    # into exactly one journal, so two journals would leave whichever execution
    # held the one the tool does not write to reading False forever.
    #
    # `None` research still means no tool is built at all, so a deployment that
    # configured nothing cannot spend money by accident.
    web_journal = WebSearchJournal()
    web_tool = _web_search_tool(config.research, web_journal)
    web_runtime = (
        None if web_tool is None else _tool_runtime(StaticToolRegistry([web_tool]))
    )
    # Exactly the prompt's contract, written as a ceiling: two searches, and a
    # turn to answer from them.
    #
    # `max_tool_calls` below `max_steps` is the whole point and is legal under
    # ADR-022. The second search spends the allowance, the runtime stops
    # advertising `web_search`, and the third step is a model that can only
    # write. So "twice at most" binds without ever costing the results the two
    # searches returned.
    #
    # Both numbers were measured wrong before. At `max_steps=6` the model
    # rephrased the same question five times -- 丹东今天天气, 丹东天气预报 今天,
    # 丹东天气 今天 实时 -- ~14s each, and died having written nothing. At `3/3`
    # it searched twice, spent step three proposing a third search, and died
    # holding 5.5KB of results: the tool ceiling was never reached, because a
    # ceiling that must sit at or above `max_steps` cannot bind before it.
    web_budget = None if web_tool is None else RunBudget(max_steps=3, max_tool_calls=2)
    web_tool_names = () if web_tool is None else (WEB_SEARCH_TOOL_NAME,)

    # Direct is available beside every retrieval-backed shape. Its *toolless*
    # runtime is its own, deny-shaped, so a model-only turn cannot inherit the
    # agentic registry merely because the next turn in the same session uses it.
    #
    # It carries the web tool for the reason ADR-023 gives: this is the mode the
    # console opens in, and it is evidence-free by the asker's own choice, which
    # is the same standing the routed fallback reaches by measurement. Making
    # only the latter web-capable meant a user asking about today's news had to
    # first attach an unrelated knowledge base and wait for it to miss.
    direct_execution = UngroundedExecution(
        executor=_toolless_runtime(),
        budget=RunBudget(max_steps=1, max_tool_calls=1),
        web_executor=web_runtime,
        web_budget=web_budget,
        web_tool_names=web_tool_names,
        web_journal=web_journal,
        web_system_prompt=WEB_DIRECT_SYSTEM_PROMPT,
    )

    rag_execution: TurnExecution | None
    if config.chat.retrieval_shape == "ungrounded":
        # This legacy deployment choice remains useful as a capability ceiling:
        # the route rejects RAG before a turn is claimed and the selector keeps
        # the same refusal for non-HTTP callers.
        rag_execution = None
    elif config.chat.retrieval_shape == "routed":
        # The retrieval service is the router's input rather than an optional
        # extra: the switch is decided by asking, so it needs the same funnel
        # the fixed shape uses -- same ACL check, same top_k, same index. A
        # router given a narrower retriever would fall back to the ungrounded
        # answer more often than the deployment's own retrieval would.
        #
        # And it needs the reranker, which is why this is the one shape that
        # refuses to assemble without one. Everywhere else the reranker is an
        # optional quality step that fails open; here its score *is* the
        # decision. Falling open would mean answering every question from
        # evidence nothing established the relevance of -- which is how the
        # first version of this shape ended up behaving exactly like `fixed`.
        if isinstance(reranker, RerankerUnavailable):
            raise RerankerRequiredError(
                "chat.retrieval_shape='routed' decides between grounded and "
                "ungrounded answers using a cross-encoder relevance score, and "
                f"no reranker could be loaded: {reranker.reason}. "
                "Use 'fixed' to answer from retrieval unconditionally, or "
                "'ungrounded' to answer without it."
            )
        # The grounded path keeps no tool in reach: a question the corpus
        # answers is answered from the corpus, every time, which is what keeps
        # `routed` measurable (ADR-021 §2). Only "the corpus did not cover this"
        # reaches a model that may search -- and that branch is now the same
        # object the console's direct mode uses, wearing the one sentence that
        # differs (ADR-023).
        rag_execution = RoutedExecution(
            retrieval=retrieval,
            executor=_toolless_runtime(),
            budget=RunBudget(max_steps=1, max_tool_calls=1),
            relevance_threshold=config.chat.routed_relevance_threshold,
            fallback=replace(
                direct_execution, web_system_prompt=WEB_FALLBACK_SYSTEM_PROMPT
            ),
        )
    elif config.chat.retrieval_shape == "agentic":
        # The model decides when to search, so it needs the tool, a budget with
        # room for a loop, and somewhere for its searches to be journalled --
        # all three or none. A tool with a one-step budget is a tool the model
        # can propose and never get an answer from.
        journal = RetrievalJournal()
        registry = StaticToolRegistry(
            [KnowledgeSearchTool(retrieval=retrieval, journal=journal).binding()]
        )
        rag_execution = AgenticExecution(
            executor=ClaudeLikeAgentRuntime(
                model=model,
                gateway=ToolGateway(
                    registry=registry,
                    policy=EnvelopePolicyEngine(registry=registry),
                    record_step_inputs=config.record_step_inputs,
                ),
                policy_identity=policy_identity,
                model_label=model_label,
                record_step_inputs=config.record_step_inputs,
                prices=model_prices,
            ),
            journal=journal,
            budget=RunBudget(
                max_steps=config.chat.max_agentic_steps,
                max_tool_calls=config.chat.max_agentic_searches,
            ),
            tool_names=(KNOWLEDGE_SEARCH,),
        )
    else:
        # Empty registry *and* empty envelope. Either alone would leave the
        # other as the only thing standing between this deployment and the
        # agentic shape it deliberately is not.
        rag_execution = FixedTwoStepExecution(
            retrieval=retrieval,
            executor=ClaudeLikeAgentRuntime(
                model=model,
                gateway=ToolGateway(
                    registry=StaticToolRegistry([]),
                    policy=EnvelopePolicyEngine(registry=StaticToolRegistry([])),
                    record_step_inputs=config.record_step_inputs,
                ),
                policy_identity=policy_identity,
                model_label=model_label,
                record_step_inputs=config.record_step_inputs,
                prices=model_prices,
            ),
            budget=RunBudget(max_steps=1, max_tool_calls=1),
        )

    chat = ChatService(
        execution=AnswerModeSelector(
            direct=direct_execution,
            rag=rag_execution,
        ),
        conversations=conversations,
        releaser=releaser,
        request_timeout_seconds=config.request_timeout_seconds,
        orphan_grace_seconds=config.chat_recovery.orphan_grace_seconds,
    )
    return (
        chat,
        None,
        client,
        qdrant,
        vector_index,
        no_reranker,
        no_sparse,
        # Every RAG shape shares this RetrievalService. Direct turns bypass it,
        # while startup still warms what a later RAG turn will actually use.
        encoders,
        retrieval,
    )


__all__ = ["ApiDependencies", "InsecureDeploymentError", "build_dependencies"]
