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

from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr

from agent_workbench.bootstrap.settings import Settings


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """What it takes to build the ordinary query engine."""

    dsn: SecretStr
    application_name: str
    statement_timeout_ms: int
    pool_size: int
    max_overflow: int


@dataclass(frozen=True, slots=True)
class ArtifactStoreConfig:
    """Where artifacts live and how large one may be."""

    backend: Literal["local", "s3"]
    local_root: str
    max_artifact_bytes: int


@dataclass(frozen=True, slots=True)
class ApiRuntimeConfig:
    """Everything the API process needs, and nothing else."""

    deployment_scope: Literal["local", "remote"]
    log_level: str
    host: str
    port: int
    shutdown_grace_seconds: int
    max_control_request_body_bytes: int
    database: DatabaseConfig
    artifacts: ArtifactStoreConfig


def project_api(settings: Settings) -> ApiRuntimeConfig:
    """Project validated settings onto what the API process consumes."""

    return ApiRuntimeConfig(
        deployment_scope=settings.app.deployment_scope,
        log_level=settings.app.log_level,
        host=settings.api.host,
        port=settings.api.port,
        shutdown_grace_seconds=settings.api.shutdown_grace_seconds,
        max_control_request_body_bytes=settings.api.max_control_request_body_bytes,
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
    )


__all__ = [
    "ApiRuntimeConfig",
    "ArtifactStoreConfig",
    "DatabaseConfig",
    "project_api",
]
