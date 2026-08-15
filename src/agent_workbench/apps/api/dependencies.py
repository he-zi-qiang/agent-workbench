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
from datetime import UTC, datetime
from pathlib import Path

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.concurrency import BlockingCallRunner
from agent_workbench.adapters.events import ObservingEventSink, ScopedEventSink
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
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
from agent_workbench.adapters.tools.workspace import (
    WorkspaceEditTool,
    WorkspaceGrepTool,
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceWriteTool,
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
from agent_workbench.application.code_approvals import (
    ApprovalScope,
    CodeApprovalRegistry,
)
from agent_workbench.application.code_session import CodeSessionService
from agent_workbench.application.knowledge_bases import KnowledgeBaseService
from agent_workbench.application.retrieval import RetrievalService
from agent_workbench.application.task_inputs import TaskInputService, TaskInputStore
from agent_workbench.application.task_triage import TaskTriageService
from agent_workbench.application.tasks import SubmittedSemantics, TaskService
from agent_workbench.application.uploads import UploadService
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.apps.api.identity import HeaderPrincipalResolver
from agent_workbench.apps.api.sse import LiveEventChannel
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
from agent_workbench.ports.event_log import EventLogPort, EventScope, EventSink
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
    #: Why grounded answers are unavailable while Chat itself is served, or
    #: None when they are available. Distinct from `chat_unavailable`: a
    #: process with no embedding runtime still answers Direct, and reporting
    #: that as "no chat" is what withdrew the whole router from the deployment
    #: least able to spare it.
    rag_unavailable: str | None
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
    #: Where transient events go on their way to a subscriber. Process-local by
    #: construction, because a transient event is never written anywhere and so
    #: can only ever reach the process that produced it. Held here rather than
    #: per request: a subscriber and the run it is watching are two different
    #: requests, and they have to meet somewhere that outlives both.
    live_events: LiveEventChannel
    # Assembled once per process. Records nothing when no collector is
    # configured, and never lets one break a request either way.
    telemetry: AssembledTelemetry
    task_service: TaskService
    task_inputs: TaskInputService
    # The human half of a Task. Assembled unconditionally, like the Task
    # service: an API that can open a Task must be able to answer the
    # approval that Task stops on, or the Task has no way forward.
    approvals: ApprovalService
    # Absent when triage is disabled or no model is configured. The route
    # answers "default" in that case, which clients treat as "submit what you
    # always submitted" (ADR-036).
    triage: TaskTriageService | None = None
    #: Absent unless `code.enabled` and this process could build a model.
    #: Present means this deployment runs coding turns *here*, which is the
    #: only arrangement in which a held tool call can be answered at all.
    code: CodeSessionService | None = None
    #: The questions currently on somebody's screen. Held beside the service
    #: because a decision arrives on a different request from the turn waiting
    #: for it, and the two have to meet somewhere that outlives both.
    code_approvals: CodeApprovalRegistry | None = None

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
    def serves_code(self) -> bool:
        """Both, because either alone is a route that cannot answer.

        The service without the registry would hold turns nobody could
        release; the registry without the service would be a decision endpoint
        for questions nothing asks.
        """

        return self.code is not None and self.code_approvals is not None

    @property
    def serves_search(self) -> bool:
        """Retrieval is servable without a model; chat is not."""

        return self.retrieval is not None

    @property
    def effective_retrieval_shape(self) -> str:
        """The shape this process actually serves, not the one it configured.

        These differ exactly when the grounded half could not be assembled --
        no embedding runtime, most often -- and the difference matters at the
        route: refusing RAG by reading the *configured* shape would accept a
        grounded request this deployment cannot answer, and turn a 422 the
        client can act on into a 500 from the selector underneath.

        Read off the selector that will run the turn rather than off
        ``rag_unavailable``. The two would normally agree, but only one of them
        is the thing that decides: ``rag_unavailable`` is a sentence recorded at
        assembly, and anything that replaces ``chat`` afterwards -- a test
        harness substituting an execution, most obviously -- leaves the
        sentence describing a service that is no longer there. Asking the
        selector cannot go stale, because it *is* the answer.
        """

        execution = self.chat.execution if self.chat is not None else None
        if isinstance(execution, AnswerModeSelector) and execution.rag is None:
            return "ungrounded"
        return self.config.chat.retrieval_shape

    def sink_for(self, *, stream_id: str, run_id: str) -> EventSink:
        """The sink one run writes into.

        A stream per session and a run per turn: a subscriber follows the
        session and resumes from wherever it left off, while each turn stays
        identifiable inside it.

        Tee-ed into the live channel, and the layering is what makes that safe
        rather than a second way for an answer to escape. ``ChatService`` wraps
        whatever it is given in ``AnswerReleaseSink`` (``application/chat.py``),
        so redaction happens *outside* this sink: by the time a payload reaches
        the observer below it has already crossed the publication fence. The
        observer keeps only transient events, which is why the durable answer
        events pass it untouched -- they are delivered by the replay, with the
        position a reconnecting client can resume from.

        Only this factory is tee-ed. The recovery reaper writes nothing but
        durable terminal events, and triage runs against a per-call in-memory
        log nobody can subscribe to; teeing either would fan out to no one at
        the cost of a callback on a synchronous submission path.
        """

        return ObservingEventSink(
            inner=ScopedEventSink(
                log=self.events,
                scope=EventScope(stream_id=stream_id, run_id=run_id),
            ),
            observer=self.live_events.observe,
        )

    async def dispose(self) -> None:
        # Flushed first: a batch span processor holds the tail of every run,
        # and that tail is the part somebody is usually looking for.
        await self.telemetry.dispose()
        if self.chat is not None:
            await self.chat.drain_cleanup(
                timeout_seconds=self.config.shutdown_grace_seconds
            )
        # Before the engine and the client close under it. A coding turn that
        # is nearly done gets the same grace a chat turn does; one that needs
        # longer is cut off and its workspace stands at its last successful
        # write (known-gaps F-14).
        if self.code is not None:
            await self.code.drain_cleanup(
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
    # ADR-042. One pool per process, threaded into every adapter that blocks.
    blocking = BlockingCallRunner(
        slots=config.blocking_calls.slots,
        queue_timeout_seconds=config.blocking_calls.queue_timeout_seconds,
    )
    artifacts = LocalArtifactStore(Path(config.artifacts.local_root), runner=blocking)
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
    assembled = (
        _assemble_chat(
            config,
            documents,
            artifacts=artifacts,
            blocking=blocking,
            conversations=conversations,
            releaser=releaser,
            telemetry=telemetry.telemetry,
        )
        if with_chat
        else _AssembledChat(
            chat_unavailable="chat was not requested for this process",
            rag_unavailable="chat was not requested for this process",
        )
    )
    chat = assembled.chat
    code = assembled.code
    code_approvals = assembled.code_approvals
    unavailable = assembled.chat_unavailable
    http = assembled.http
    qdrant = assembled.qdrant
    vector_index = assembled.vector_index
    no_reranker = assembled.reranker_unavailable
    no_sparse = assembled.sparse_unavailable
    encoders = assembled.encoders
    retrieval = assembled.retrieval
    triage = assembled.triage
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
        rag_unavailable=assembled.rag_unavailable,
        reranker_unavailable=no_reranker,
        sparse_unavailable=no_sparse,
        http=http,
        qdrant=qdrant,
        vector_index=vector_index,
        encoders=encoders,
        retrieval=retrieval,
        events=events,
        live_events=LiveEventChannel(
            buffer_events=config.event_stream.subscriber_buffer_events,
            max_subscribers_per_stream=(
                config.event_stream.max_live_subscribers_per_stream
            ),
        ),
        task_service=task_service,
        task_inputs=task_inputs,
        approvals=ApprovalService(
            approvals=PostgresApprovalStore(engine, events=events)
        ),
        triage=triage,
        code=code,
        code_approvals=code_approvals,
    )


@dataclass(frozen=True, slots=True)
class _AssembledChat:
    """What one attempt at the chat stack produced, and what it could not.

    Two separate absences, which used to be one. ``chat_unavailable`` means no
    turn can be answered at all -- there is no model. ``rag_unavailable`` means
    only the grounded half is missing, which is the ordinary state of a process
    with no embedding runtime installed and is not a reason to withdraw Chat.
    """

    chat: ChatService | None = None
    chat_unavailable: str | None = None
    #: Why grounded answers are unavailable, or None when they are available.
    rag_unavailable: str | None = None
    http: httpx.AsyncClient | None = None
    qdrant: AsyncQdrantClient | None = None
    vector_index: QdrantVectorIndex | None = None
    reranker_unavailable: str | None = None
    sparse_unavailable: str | None = None
    encoders: tuple[object, ...] = ()
    retrieval: RetrievalService | None = None
    triage: TaskTriageService | None = None
    #: Built here rather than beside the Task stack because Code needs exactly
    #: what Chat's model half needs -- a provider and the client it speaks
    #: over -- and building a second of either would give this process two
    #: connection pools and two things for `dispose` to remember.
    code: CodeSessionService | None = None
    code_approvals: CodeApprovalRegistry | None = None


def _assemble_chat(
    config: ApiRuntimeConfig,
    documents: PostgresDocumentStore,
    *,
    artifacts: ArtifactStore,
    blocking: BlockingCallRunner,
    conversations: PostgresConversationStore,
    releaser: PostgresChatReleaseCoordinator,
    telemetry: Telemetry,
) -> _AssembledChat:
    """Build the chat stack, and report whichever halves could not be built.

    The embedder is tried first because it is the only piece whose absence is
    an expected state rather than a misconfiguration. Everything after it --
    the model, the vector index -- either works or is a refusal, and refusing
    is ``build_model``'s job, not something to soften into a degraded mode.

    **A missing embedder costs retrieval, not Chat.** It used to return here,
    which withdrew the entire ``/v1/chat`` router -- including Direct, the mode
    the console opens in, which reaches no index, needs no embedding and is the
    one thing such a deployment can still do. The deployment that most needs a
    working Direct chat is exactly the one that could not load an embedding
    runtime, and that was the deployment that had no Chat at all. What follows
    is the same assembly with the retrieval half elided, which is a shape this
    module already had a name for: ``retrieval_shape = "ungrounded"``.
    """

    built_embedder = build_embedder(config.embedding, runner=blocking)
    no_embedder = (
        built_embedder.reason
        if isinstance(built_embedder, EmbeddingUnavailable)
        else None
    )
    embedder = (
        None if isinstance(built_embedder, EmbeddingUnavailable) else built_embedder
    )

    # After the embedder, because a process with nothing to retrieve has no use
    # for a reranker and loading one would be several gigabytes spent on a
    # capability that is not being served.
    #
    # Both notes stay `None` when the embedder is missing, and that is not an
    # oversight: they exist so an ablation cannot mistake an unreranked process
    # for a reranked one, so they have to mean "this deployment loaded no
    # reranker" and nothing else. Filling them in with "there was no embedder"
    # would report a downgrade at a step that was never reached.
    reranker = (
        None if embedder is None else build_reranker(config.reranker, runner=blocking)
    )
    no_reranker = reranker.reason if isinstance(reranker, RerankerUnavailable) else None
    sparse = (
        None
        if embedder is None
        else build_sparse_encoder(config.embedding, runner=blocking)
    )
    no_sparse = sparse.reason if isinstance(sparse, SparseEncodingUnavailable) else None

    qdrant: AsyncQdrantClient | None = None
    vector_index: QdrantVectorIndex | None = None
    retrieval: RetrievalService | None = None
    encoders: tuple[object, ...] = ()
    if embedder is not None:
        qdrant = AsyncQdrantClient(
            url=config.qdrant.url,
            api_key=(
                config.qdrant.api_key.get_secret_value()
                if config.qdrant.api_key is not None
                else None
            ),
            timeout=config.qdrant.request_timeout_seconds,
        )
        # Read via the alias, never the ingestion collection. This makes an
        # alias switch affect new Chat requests without changing the write
        # target.
        vector_index = QdrantVectorIndex(qdrant, collection=config.qdrant.read_alias)

        sparse_encoder = (
            None if isinstance(sparse, SparseEncodingUnavailable) else sparse
        )
        # What startup warms. Taken from what was just assembled rather than
        # read back off the retrieval service: which encoders a retriever holds
        # is now its own business -- LlamaIndex's does not expose them the way
        # the reference path did -- and warming is about the runtimes this
        # process loaded, which is a fact this scope already has.
        #
        # The reranker is in here for the same reason the other two are: it is
        # on by default and its first forward pass is exactly as cold. It is
        # bound to a name and reused below so that what startup warms is
        # provably the object retrieval will call -- warming a second instance
        # would spend the boot time and save the request nothing. `None` here
        # means one thing only: the weights or the runtime behind them did not
        # load. There is no switch to be off -- `rag.reranker.enabled` is a
        # `Literal[True]`, so settings rejects a deployment that tries -- and
        # warming skips `None` rather than making this a conditional.
        loaded_reranker = (
            None if isinstance(reranker, RerankerUnavailable) else reranker
        )
        encoders = tuple(
            e for e in (embedder, sparse_encoder, loaded_reranker) if e is not None
        )

        retrieval = RetrievalService(
            candidate_retriever=build_candidate_retriever(
                llama_index_enabled=config.retrieval.llama_index_enabled,
                embedder=embedder,
                index=vector_index,
                sparse_encoder=sparse_encoder,
            ),
            documents=documents,
            telemetry=telemetry,
            reranker=loaded_reranker,
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
        return _AssembledChat(
            chat_unavailable=str(unconfigured),
            # No model means no answer of either kind, so the grounded half is
            # unavailable for this reason too rather than for its own.
            rag_unavailable=str(unconfigured),
            http=client,
            qdrant=qdrant,
            vector_index=vector_index,
            reranker_unavailable=no_reranker,
            sparse_unavailable=no_sparse,
            encoders=encoders,
            retrieval=retrieval,
            # No model, no triage: the endpoint answers "default" and every
            # client submits what it always has.
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
    rag_unavailable: str | None = None
    if retrieval is None:
        # No embedding runtime, so no grounded half to build. Reached before the
        # configured shape is consulted because a shape is a choice among
        # retrieval-backed executions, and there is no retrieval to back one --
        # this deployment is `ungrounded` in fact whatever the file says. Direct
        # is assembled below exactly as it is everywhere else, which is the
        # whole point: it never touched an index.
        rag_execution = None
        rag_unavailable = no_embedder or "no retrieval was assembled"
    elif config.chat.retrieval_shape == "ungrounded":
        # This legacy deployment choice remains useful as a capability ceiling:
        # the route rejects RAG before a turn is claimed and the selector keeps
        # the same refusal for non-HTTP callers.
        rag_execution = None
        rag_unavailable = "this deployment serves chat.retrieval_shape='ungrounded'"
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
    # Submission-time triage (ADR-036), on the same toolless deny-shaped
    # runtime the direct chat turn uses. Its run events go to a per-call
    # in-memory log and die with it: a triage run precedes the Task, so there
    # is no timeline to write to, and the verdict's durable record is the
    # `intent` block on TaskSubmitted.
    triage = (
        TaskTriageService(
            executor=_toolless_runtime(),
            timeout_seconds=config.triage.timeout_seconds,
            sink_for=lambda stream_id: ScopedEventSink(
                log=InMemoryEventLog(),
                scope=EventScope(stream_id=stream_id, run_id=stream_id),
            ),
        )
        if config.triage.enabled
        else None
    )
    code_scope = WorkspaceScope()
    code_approvals = CodeApprovalRegistry()
    code_registry = StaticToolRegistry(
        [
            WorkspaceListTool(code_scope).binding(),
            WorkspaceReadTool(code_scope).binding(),
            WorkspaceWriteTool(code_scope).binding(),
            WorkspaceEditTool(code_scope).binding(),
            WorkspaceGrepTool(code_scope).binding(),
        ]
    )

    def _code_runtime(scope: ApprovalScope) -> ClaudeLikeAgentRuntime:
        return ClaudeLikeAgentRuntime(
            model=model,
            gateway=ToolGateway(
                registry=code_registry,
                policy=EnvelopePolicyEngine(registry=code_registry),
                record_step_inputs=config.record_step_inputs,
                # The gate Code exists to be able to supply: this run happens
                # in the process the answering request reaches. No ledger is
                # passed, which is also a check -- a registry holding a tool
                # that records external effects would refuse to assemble here.
                approvals=code_approvals.gate_for(scope),
                approval_timeout_seconds=config.code.approval_timeout_seconds,
            ),
            # Its own identity, so a tool execution can be attributed to a
            # coding session rather than to "the API".
            policy_identity=f"{policy_identity}-code",
            model_label=model_label,
            record_step_inputs=config.record_step_inputs,
            prices=model_prices,
        )

    code = (
        CodeSessionService(
            conversations=conversations,
            artifacts=artifacts,
            executor_for=_code_runtime,
            scope=code_scope,
            budget=RunBudget(
                max_steps=config.code.max_steps,
                max_tool_calls=config.code.max_tool_calls,
                max_total_tokens=config.code.max_total_tokens,
                max_cost_micro_usd=config.code.max_cost_micro_usd,
            ),
            turn_timeout_seconds=config.code.turn_timeout_seconds,
            max_concurrent_turns=config.code.max_concurrent_turns,
            clock=lambda: datetime.now(UTC),
        )
        if config.code.enabled
        else None
    )
    return _AssembledChat(
        chat=chat,
        chat_unavailable=None,
        rag_unavailable=rag_unavailable,
        code=code,
        code_approvals=code_approvals if code is not None else None,
        http=client,
        qdrant=qdrant,
        vector_index=vector_index,
        reranker_unavailable=no_reranker,
        sparse_unavailable=no_sparse,
        # Every RAG shape shares this RetrievalService. Direct turns bypass it,
        # while startup still warms what a later RAG turn will actually use.
        encoders=encoders,
        retrieval=retrieval,
        triage=triage,
    )


__all__ = ["ApiDependencies", "InsecureDeploymentError", "build_dependencies"]
