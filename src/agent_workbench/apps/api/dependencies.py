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
from dataclasses import dataclass
from pathlib import Path

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.persistence import (
    PostgresChatExpirationCoordinator,
    PostgresChatReleaseCoordinator,
    PostgresConversationStore,
    PostgresDocumentStore,
    PostgresEventLog,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chat import REFUSAL, ChatService
from agent_workbench.application.chat_recovery import (
    ChatPendingReleaseRecovery,
    ChatTurnReaper,
)
from agent_workbench.application.retrieval import RetrievalService
from agent_workbench.application.task_inputs import TaskInputService, TaskInputStore
from agent_workbench.application.tasks import SubmittedSemantics, TaskService
from agent_workbench.application.uploads import UploadService
from agent_workbench.apps.api.identity import HeaderPrincipalResolver
from agent_workbench.bootstrap.embedding_factory import (
    EmbeddingUnavailable,
    build_embedder,
)
from agent_workbench.bootstrap.model_factory import build_model
from agent_workbench.bootstrap.network import is_loopback_bind_address
from agent_workbench.bootstrap.projections import ApiRuntimeConfig
from agent_workbench.bootstrap.qdrant_startup import verify_qdrant_startup
from agent_workbench.bootstrap.reranker_factory import (
    RerankerUnavailable,
    build_reranker,
)
from agent_workbench.bootstrap.sparse_factory import (
    SparseEncodingUnavailable,
    build_sparse_encoder,
)
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.documents import DocumentStore
from agent_workbench.ports.event_log import EventLogPort, EventScope
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway


class InsecureDeploymentError(RuntimeError):
    """A deployment asked to serve remotely without a real identity provider."""


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    """Everything the routes need, assembled once at startup."""

    config: ApiRuntimeConfig
    engine: AsyncEngine
    documents: DocumentStore
    artifacts: ArtifactStore
    uploads: UploadService
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
    events: EventLogPort
    task_service: TaskService
    task_inputs: TaskInputService

    @property
    def max_control_request_body_bytes(self) -> int:
        return self.config.max_control_request_body_bytes

    @property
    def max_artifact_bytes(self) -> int:
        return self.config.artifacts.max_artifact_bytes

    @property
    def serves_chat(self) -> bool:
        return self.chat is not None

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
        """Check the Qdrant read path before accepting any HTTP request."""

        if self.qdrant is not None:
            await verify_qdrant_startup(
                self.qdrant,
                qdrant=self.config.qdrant,
                embedding=self.config.embedding,
            )


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

    chat, unavailable, http, qdrant, vector_index, no_reranker, no_sparse = (
        _assemble_chat(
            config,
            documents,
            conversations=conversations,
            releaser=releaser,
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
        )
    )
    return ApiDependencies(
        config=config,
        engine=engine,
        documents=documents,
        artifacts=artifacts,
        uploads=UploadService(documents=documents, artifacts=artifacts),
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
        events=events,
        task_service=task_service,
        task_inputs=task_inputs,
    )


def _assemble_chat(
    config: ApiRuntimeConfig,
    documents: PostgresDocumentStore,
    *,
    conversations: PostgresConversationStore,
    releaser: PostgresChatReleaseCoordinator,
) -> tuple[
    ChatService | None,
    str | None,
    httpx.AsyncClient | None,
    AsyncQdrantClient | None,
    QdrantVectorIndex | None,
    str | None,
    str | None,
]:
    """Build the chat stack, or report the one reason it could not be built.

    The embedder is tried first because it is the only piece whose absence is
    an expected state rather than a misconfiguration. Everything after it --
    the model, the vector index -- either works or is a refusal, and refusing
    is ``build_model``'s job, not something to soften into a degraded mode.
    """

    embedder = build_embedder(config.embedding)
    if isinstance(embedder, EmbeddingUnavailable):
        return None, embedder.reason, None, None, None, None, None

    # After the embedder, because a process that cannot chat has no use for a
    # reranker and loading one would be several gigabytes spent on a capability
    # that is not being served.
    reranker = build_reranker(config.reranker)
    no_reranker = reranker.reason if isinstance(reranker, RerankerUnavailable) else None
    sparse = build_sparse_encoder(config.embedding)
    no_sparse = sparse.reason if isinstance(sparse, SparseEncodingUnavailable) else None

    # Constructed before the model because the adapter takes it: httpx opens
    # no socket until the first request, so a refusal below simply ends a
    # startup that was going to end anyway.
    client = httpx.AsyncClient(timeout=config.model.profiles["main"].timeout_seconds)
    model = build_model(config.model, client=client)

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

    chat = ChatService(
        retrieval=RetrievalService(
            embedder=embedder,
            index=vector_index,
            documents=documents,
            sparse_encoder=(
                None if isinstance(sparse, SparseEncodingUnavailable) else sparse
            ),
            reranker=None if isinstance(reranker, RerankerUnavailable) else reranker,
            rerank_timeout_seconds=config.reranker.timeout_seconds,
        ),
        executor=ClaudeLikeAgentRuntime(
            model=model,
            gateway=ToolGateway(
                registry=StaticToolRegistry([]),
                policy=EnvelopePolicyEngine(registry=StaticToolRegistry([])),
            ),
            policy_identity=f"api-{config.deployment_scope}",
        ),
        conversations=conversations,
        releaser=releaser,
        budget=RunBudget(max_steps=1, max_tool_calls=1),
        request_timeout_seconds=config.request_timeout_seconds,
        orphan_grace_seconds=config.chat_recovery.orphan_grace_seconds,
    )
    return chat, None, client, qdrant, vector_index, no_reranker, no_sparse


__all__ = ["ApiDependencies", "InsecureDeploymentError", "build_dependencies"]
