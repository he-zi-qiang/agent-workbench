"""Task projections are narrow, deterministic and honest about concurrency."""

from __future__ import annotations

import tomllib
from copy import deepcopy
from pathlib import Path

import pytest

from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import (
    project_api,
    project_ingestion_worker,
    project_task,
    project_task_worker,
)
from agent_workbench.bootstrap.settings import Settings

TEST_DSN = "postgresql+asyncpg://unit:test@postgres:5432/agent_workbench"


def _settings() -> Settings:
    with Path(DEFAULT_CONFIG_FILE).open("rb") as handle:
        payload = tomllib.load(handle)
    payload["database"] = {
        **payload["database"],
        "dsn": TEST_DSN,
        "guard_dsn": TEST_DSN,
        "listen_dsn": TEST_DSN,
    }
    payload["model"]["main"]["model_id"] = "unit-main"
    payload["model"]["compact"]["model_id"] = "unit-compact"
    payload["secrets"] = {"deepseek_api_key": "unit-test-key"}
    return Settings(**payload)


def test_task_projection_contains_submission_decisions_but_no_raw_settings() -> None:
    settings = _settings()

    task = project_task(settings)
    api = project_api(settings)

    assert api.task == task
    assert api.qdrant.read_alias == settings.qdrant.read_alias
    assert api.qdrant.write_collection == settings.qdrant.write_collection
    assert api.qdrant.read_alias != api.qdrant.write_collection
    assert api.qdrant.allow_local_bootstrap is False
    assert task.graph_version == settings.workflow.graph_version
    assert task.claim_poll_seconds == 1.0
    assert task.run_semantics_snapshot == settings.task_run_semantics_snapshot()
    assert task.run_semantics_revision == settings.task_run_semantics_revision()
    assert task.policy_revision == settings.policy.revision
    assert task.policy_fingerprint == settings.policy_fingerprint()
    assert task.default_authorization_envelope.allowed_tools == ()
    assert task.default_authorization_envelope.denied_tools == ()
    assert task.default_authorization_envelope.max_tool_risk == "read"


def test_single_task_worker_projection_reuses_only_its_storage_configs() -> None:
    settings = _settings()

    worker = project_task_worker(settings)

    assert worker.worker_concurrency == 1
    assert worker.database.dsn == settings.database.dsn
    assert worker.artifacts.local_root == settings.artifact_store.local_root
    assert worker.task == project_task(settings)
    assert worker.model is not None
    assert worker.qdrant is not None
    assert worker.embedding is not None
    assert worker.retrieval is not None
    assert worker.runtime is not None
    assert worker.qdrant.read_alias == settings.qdrant.read_alias
    assert worker.embedding.vector_size == settings.rag.embedding.vector_size
    assert worker.runtime.max_steps == settings.runtime.max_steps


def test_ingestion_worker_projection_owns_the_write_side_of_the_index() -> None:
    settings = _settings()

    worker = project_ingestion_worker(settings, worker_id="ingester_test")

    assert worker.worker_id == "ingester_test"
    assert worker.qdrant.write_collection == settings.qdrant.write_collection
    assert worker.qdrant.read_alias == settings.qdrant.read_alias
    assert worker.embedding.sparse_enabled is True
    assert worker.embedding.sparse_vocabulary_size == 250_002
    assert worker.claim_limit == 1
    assert worker.heartbeat_seconds < worker.lease_seconds


def test_task_worker_projection_refuses_unimplemented_multi_worker_topology() -> None:
    settings = _settings()
    payload = deepcopy(settings.model_dump(mode="python"))
    payload["coordination"]["worker_concurrency"] = 2

    with pytest.raises(ValueError, match="exactly one worker"):
        project_task_worker(Settings(**payload))
