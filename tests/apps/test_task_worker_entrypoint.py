"""The independent Task Worker composition and its cancellable poll loop."""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agent_workbench.adapters.embedding.fake import DeterministicEmbedder
from agent_workbench.adapters.langgraph import LangGraphTaskWorkflow
from agent_workbench.adapters.models.fake import FakeModel
from agent_workbench.apps.task_worker.composition import (
    RealTaskHandlersUnavailableError,
    build_task_worker_dependencies,
)
from agent_workbench.apps.task_worker.main import build_parser
from agent_workbench.apps.task_worker.runner import TaskWorkerRunner
from agent_workbench.bootstrap.embedding_factory import EmbeddingUnavailable
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import (
    ArtifactStoreConfig,
    DatabaseConfig,
    EmbeddingConfig,
    ModelConfig,
    TaskConfig,
    TaskWorkerRuntimeConfig,
    project_task_worker,
)
from agent_workbench.bootstrap.settings import Settings
from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.ports.model import ModelPort


def _config(tmp_path: object) -> TaskWorkerRuntimeConfig:
    return TaskWorkerRuntimeConfig(
        database=DatabaseConfig(
            dsn=SecretStr("postgresql+asyncpg://user:password@localhost/example"),
            application_name="agent-workbench-test-worker",
            statement_timeout_ms=30_000,
            pool_size=1,
            max_overflow=0,
        ),
        artifacts=ArtifactStoreConfig(
            backend="local",
            local_root=str(tmp_path),
            max_artifact_bytes=1_048_576,
        ),
        task=TaskConfig(
            graph_version="v1",
            claim_poll_seconds=0.01,
            lease_seconds=90,
            heartbeat_seconds=20,
            max_attempts=5,
            retry_base_seconds=2,
            retry_max_seconds=60,
            run_semantics_snapshot={},
            run_semantics_revision="test-v1",
            policy_revision="policy-v1",
            policy_fingerprint="f" * 16,
            default_authorization_envelope=AuthorizationEnvelope(),
        ),
        worker_id="worker_test",
        worker_concurrency=1,
    )


def _projected_config() -> TaskWorkerRuntimeConfig:
    with Path(DEFAULT_CONFIG_FILE).open("rb") as handle:
        payload = tomllib.load(handle)
    dsn = "postgresql+asyncpg://unit:test@postgres:5432/agent_workbench"
    payload["database"] = {
        **payload["database"],
        "dsn": dsn,
        "guard_dsn": dsn,
        "listen_dsn": dsn,
    }
    payload["model"]["main"]["model_id"] = "unit-main"
    payload["model"]["compact"]["model_id"] = "unit-compact"
    payload["secrets"] = {"deepseek_api_key": "unit-test-key"}
    return project_task_worker(Settings(**payload), worker_id="worker_production_test")


def test_default_worker_assembly_refuses_synthetic_handlers(tmp_path: object) -> None:
    with pytest.raises(RealTaskHandlersUnavailableError, match="--demo"):
        build_task_worker_dependencies(_config(tmp_path))


def test_demo_worker_assembly_uses_the_durable_adapters(tmp_path: object) -> None:
    async def scenario() -> tuple[str, str]:
        dependencies = build_task_worker_dependencies(_config(tmp_path), demo=True)
        try:
            return (
                type(dependencies.registry).__name__,
                type(dependencies.worker.workflow).__name__,
            )
        finally:
            await dependencies.dispose()

    assert asyncio.run(scenario()) == ("PostgresTaskRegistry", "LangGraphTaskWorkflow")


def test_real_worker_refuses_to_start_without_an_embedding_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_workbench.apps.task_worker.composition as composition

    def unavailable(_: EmbeddingConfig) -> EmbeddingUnavailable:
        return EmbeddingUnavailable("test embedding runtime is unavailable")

    monkeypatch.setattr(composition, "build_embedder", unavailable)

    with pytest.raises(RealTaskHandlersUnavailableError, match="embedding runtime"):
        build_task_worker_dependencies(_projected_config())


def test_real_worker_wires_model_retrieval_and_policy_gated_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_workbench.apps.task_worker.composition as composition
    import agent_workbench.bootstrap.model_factory as model_factory

    def dense(config: EmbeddingConfig) -> DeterministicEmbedder:
        return DeterministicEmbedder(dimension=config.vector_size)

    def no_sparse(_: EmbeddingConfig) -> composition.SparseEncodingUnavailable:
        return composition.SparseEncodingUnavailable("dense-only test")

    def fake_model(_: ModelConfig, *, client: httpx.AsyncClient) -> ModelPort:
        del client
        return FakeModel(())

    monkeypatch.setattr(composition, "build_embedder", dense)
    monkeypatch.setattr(composition, "build_sparse_encoder", no_sparse)
    monkeypatch.setattr(model_factory, "build_model", fake_model)

    async def scenario() -> tuple[bool, bool]:
        dependencies = build_task_worker_dependencies(_projected_config())
        try:
            workflow = dependencies.worker.workflow
            assert isinstance(workflow, LangGraphTaskWorkflow)
            return (
                dependencies.http is not None,
                dependencies.qdrant is not None,
            )
        finally:
            await dependencies.dispose()

    http, qdrant = asyncio.run(scenario())

    assert (http, qdrant) == (True, True)


def test_empty_queue_poll_exits_promptly_when_stop_is_requested() -> None:
    async def scenario() -> int:
        calls = 0
        observed = asyncio.Event()
        stop = asyncio.Event()

        async def empty_queue() -> None:
            nonlocal calls
            calls += 1
            observed.set()
            return None

        runner = TaskWorkerRunner(run_once=empty_queue, poll_seconds=30)
        running = asyncio.create_task(runner.run_forever(stop))
        await observed.wait()
        stop.set()
        await asyncio.wait_for(running, timeout=0.1)
        return calls

    assert asyncio.run(scenario()) == 1


def test_the_console_requires_explicit_demo_opt_in() -> None:
    assert build_parser().parse_args([]).demo is False
    assert build_parser().parse_args(["--demo"]).demo is True
