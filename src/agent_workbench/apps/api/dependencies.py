"""Assembling the API's dependencies from validated settings, once.

Everything the routes use is built here, at startup, from a Settings object
that has already been validated. Routes receive finished objects; they never
read configuration, and they never construct an engine or a store of their own.
That is what keeps "which database does this endpoint talk to" a question with
one answer.

The deployment scope decides whether this process may serve at all. A scope of
``remote`` with a development identity resolver would be an authenticated-
looking service that authenticates nothing, so it refuses to assemble instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.persistence import (
    PostgresDocumentStore,
    create_query_engine,
)
from agent_workbench.application.uploads import UploadService
from agent_workbench.apps.api.identity import HeaderPrincipalResolver
from agent_workbench.bootstrap.projections import ApiRuntimeConfig
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.documents import DocumentStore


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

    @property
    def max_control_request_body_bytes(self) -> int:
        return self.config.max_control_request_body_bytes

    @property
    def max_artifact_bytes(self) -> int:
        return self.config.artifacts.max_artifact_bytes

    async def dispose(self) -> None:
        await self.engine.dispose()


def build_dependencies(config: ApiRuntimeConfig) -> ApiDependencies:
    """Build the API's dependencies, or refuse to."""

    if config.deployment_scope == "remote":
        # The only identity resolver that exists reads headers. Serving that
        # beyond a single machine would be an access-controlled API whose
        # access control is a request header.
        raise InsecureDeploymentError(
            "the API has no production identity provider yet; "
            "app.deployment_scope must be 'local' until one exists"
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
    return ApiDependencies(
        config=config,
        engine=engine,
        documents=documents,
        artifacts=artifacts,
        uploads=UploadService(documents=documents, artifacts=artifacts),
        principals=HeaderPrincipalResolver(),
    )


__all__ = ["ApiDependencies", "InsecureDeploymentError", "build_dependencies"]
