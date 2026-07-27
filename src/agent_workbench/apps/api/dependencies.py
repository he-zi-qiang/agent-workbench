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

from dataclasses import dataclass
from pathlib import Path

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.persistence import (
    PostgresConversationStore,
    PostgresDocumentStore,
    create_query_engine,
)
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chat import ChatService
from agent_workbench.application.retrieval import RetrievalService
from agent_workbench.application.uploads import UploadService
from agent_workbench.apps.api.identity import HeaderPrincipalResolver
from agent_workbench.bootstrap.embedding_factory import (
    EmbeddingUnavailable,
    build_embedder,
)
from agent_workbench.bootstrap.model_factory import build_model
from agent_workbench.bootstrap.network import is_loopback_bind_address
from agent_workbench.bootstrap.projections import ApiRuntimeConfig
from agent_workbench.domain.runs import RunBudget
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.documents import DocumentStore
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
    chat_unavailable: str | None
    http: httpx.AsyncClient | None

    @property
    def max_control_request_body_bytes(self) -> int:
        return self.config.max_control_request_body_bytes

    @property
    def max_artifact_bytes(self) -> int:
        return self.config.artifacts.max_artifact_bytes

    @property
    def serves_chat(self) -> bool:
        return self.chat is not None

    async def dispose(self) -> None:
        if self.http is not None:
            await self.http.aclose()
        await self.engine.dispose()


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

    chat, unavailable, http = (
        _assemble_chat(config, engine, documents)
        if with_chat
        else (None, "chat was not requested for this process", None)
    )
    return ApiDependencies(
        config=config,
        engine=engine,
        documents=documents,
        artifacts=artifacts,
        uploads=UploadService(documents=documents, artifacts=artifacts),
        principals=HeaderPrincipalResolver(),
        chat=chat,
        chat_unavailable=unavailable,
        http=http,
    )


def _assemble_chat(
    config: ApiRuntimeConfig,
    engine: AsyncEngine,
    documents: PostgresDocumentStore,
) -> tuple[ChatService | None, str | None, httpx.AsyncClient | None]:
    """Build the chat stack, or report the one reason it could not be built.

    The embedder is tried first because it is the only piece whose absence is
    an expected state rather than a misconfiguration. Everything after it --
    the model, the vector index -- either works or is a refusal, and refusing
    is ``build_model``'s job, not something to soften into a degraded mode.
    """

    embedder = build_embedder(config.embedding)
    if isinstance(embedder, EmbeddingUnavailable):
        return None, embedder.reason, None

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
    chat = ChatService(
        retrieval=RetrievalService(
            embedder=embedder,
            index=QdrantVectorIndex(qdrant, collection=config.qdrant.write_collection),
            documents=documents,
        ),
        executor=ClaudeLikeAgentRuntime(
            model=model,
            gateway=ToolGateway(
                registry=StaticToolRegistry([]),
                policy=EnvelopePolicyEngine(registry=StaticToolRegistry([])),
            ),
            policy_identity=f"api-{config.deployment_scope}",
        ),
        conversations=PostgresConversationStore(engine),
        budget=RunBudget(max_steps=1, max_tool_calls=1),
    )
    return chat, None, client


__all__ = ["ApiDependencies", "InsecureDeploymentError", "build_dependencies"]
