"""Task projections are narrow, deterministic and honest about concurrency."""

from __future__ import annotations

import tomllib
from copy import deepcopy
from pathlib import Path

import pytest

from agent_workbench.adapters.tools.export_artifact import SPEC as EXPORT_SPEC
from agent_workbench.adapters.tools.export_artifact import (
    TOOL_NAME as EXPORT_ARTIFACT_TOOL,
)
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import (
    WORKSPACE_TOOLS,
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
    envelope = task.default_authorization_envelope
    # One tool, named. The graph has exactly one node that writes, and a
    # ceiling wide enough for a tool it does not have would authorise the next
    # tool somebody registers without anyone deciding to.
    assert envelope.allowed_tools == (EXPORT_ARTIFACT_TOOL, *WORKSPACE_TOOLS)
    assert envelope.denied_tools == ()
    assert envelope.max_tool_risk == "write"
    assert envelope.permits(EXPORT_SPEC)
    # The approval this write needs is the graph's, taken before the node runs;
    # a second requirement here is one nothing in v1 can satisfy. See ADR-015.
    assert envelope.requires_approval(EXPORT_SPEC) is False
    # Naming one tool is not raising a ceiling for every tool at that risk.
    assert not envelope.permits(
        EXPORT_SPEC.model_copy(update={"name": "export_anything_else"})
    )


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


def test_task_worker_projection_carries_the_configured_lane_count() -> None:
    """The projection reports the lanes it was configured for (ADR-024).

    This replaces a test that asserted the opposite -- ``worker_concurrency=2``
    used to raise "exactly one worker". That refusal was written when the
    checkpointer had no epoch predicate; it does now, so the number reaches the
    runner instead of being rejected on the way.
    """

    settings = _settings()
    payload = deepcopy(settings.model_dump(mode="python"))
    payload["coordination"]["worker_concurrency"] = 3

    worker = project_task_worker(Settings(**payload))

    assert worker.worker_concurrency == 3


def test_task_worker_lanes_may_not_outnumber_guard_connections() -> None:
    """The ceiling that replaced the refusal, and why it is the right one.

    Every concurrent Task pins its own guard connection, so lanes past the
    guard budget are lanes that would claim work they cannot guard. Settings
    refuses that combination outright, which is why ``project_task_worker`` no
    longer re-checks anything: a second copy of the rule is the copy that keeps
    running after somebody edits the first.
    """

    settings = _settings()
    payload = deepcopy(settings.model_dump(mode="python"))
    payload["coordination"]["worker_concurrency"] = (
        settings.database.guard_connection_budget + 1
    )

    with pytest.raises(ValueError, match="guard_connection_budget"):
        Settings(**payload)
