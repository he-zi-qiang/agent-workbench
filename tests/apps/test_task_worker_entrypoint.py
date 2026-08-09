"""The independent Task Worker composition and its cancellable poll loop."""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from agent_workbench.adapters.embedding.fake import DeterministicEmbedder
from agent_workbench.adapters.langgraph import LangGraphTaskWorkflow
from agent_workbench.adapters.mcp.client import (
    RemoteCallResult,
    RemoteToolDefinition,
    RemoteToolPage,
)
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
    MCPConfig,
    MCPServerConfig,
    ModelConfig,
    TaskConfig,
    TaskWorkerRuntimeConfig,
    project_task_worker,
)
from agent_workbench.bootstrap.settings import Settings
from agent_workbench.domain.policies import AuthorizationEnvelope
from agent_workbench.domain.schema import JsonObject
from agent_workbench.ports.model import ModelPort
from agent_workbench.workflows.task_handlers import build_task_v1_handlers


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


class _DirectoryMCPClient:
    async def list_tools_page(self, cursor: str | None) -> RemoteToolPage:
        assert cursor is None
        return RemoteToolPage(
            tools=(
                RemoteToolDefinition(
                    name="render-document",
                    description="Render a document.",
                    input_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                ),
            )
        )

    async def call_tool(self, name: str, arguments: JsonObject) -> RemoteCallResult:
        del name, arguments
        return RemoteCallResult(content=())


def _patch_real_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_default_worker_assembly_refuses_synthetic_handlers(tmp_path: object) -> None:
    with pytest.raises(RealTaskHandlersUnavailableError, match="--demo"):
        asyncio.run(build_task_worker_dependencies(_config(tmp_path)))


def test_demo_worker_assembly_uses_the_durable_adapters(tmp_path: object) -> None:
    async def scenario() -> tuple[str, str]:
        dependencies = await build_task_worker_dependencies(
            _config(tmp_path), demo=True
        )
        try:
            return (
                type(dependencies.registry).__name__,
                type(dependencies.worker.workflow).__name__,
            )
        finally:
            await dependencies.dispose()

    assert asyncio.run(scenario()) == ("PostgresTaskRegistry", "LangGraphTaskWorkflow")


def test_partial_assembly_disposes_the_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    import agent_workbench.apps.task_worker.composition as composition

    class Engine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = Engine()

    def fail_to_build_guards(*_: object, **__: object) -> None:
        raise RuntimeError("guard assembly failed")

    def build_engine(*_: object, **__: object) -> Engine:
        return engine

    monkeypatch.setattr(composition, "create_query_engine", build_engine)
    monkeypatch.setattr(
        composition, "PostgresExecutionGuardFactory", fail_to_build_guards
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="guard assembly failed"):
            await build_task_worker_dependencies(_config(tmp_path), demo=True)

    asyncio.run(scenario())

    assert engine.disposed is True


def test_real_worker_refuses_to_start_without_an_embedding_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_workbench.apps.task_worker.composition as composition

    def unavailable(_: EmbeddingConfig) -> EmbeddingUnavailable:
        return EmbeddingUnavailable("test embedding runtime is unavailable")

    monkeypatch.setattr(composition, "build_embedder", unavailable)

    with pytest.raises(RealTaskHandlersUnavailableError, match="embedding runtime"):
        asyncio.run(build_task_worker_dependencies(_projected_config()))


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
        dependencies = await build_task_worker_dependencies(_projected_config())
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


def test_mcp_connection_lives_until_worker_disposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_workbench.apps.task_worker.composition as composition

    _patch_real_runtime(monkeypatch)
    lifecycle: list[str] = []
    active = False

    @asynccontextmanager
    async def connect(
        endpoint: str, *, timeout_seconds: int
    ) -> AsyncGenerator[_DirectoryMCPClient]:
        nonlocal active
        if endpoint == "http://127.0.0.1:9000":
            lifecycle.append("failed")
            raise OSError("server unavailable")
        assert endpoint == "http://127.0.0.1:9100"
        assert timeout_seconds == 7
        lifecycle.append("enter")
        active = True
        try:
            yield _DirectoryMCPClient()
        finally:
            active = False
            lifecycle.append("exit")

    monkeypatch.setattr(composition, "connect_mcp_client", connect)
    projected = _projected_config()
    config = replace(
        projected,
        mcp=MCPConfig(
            servers=(
                MCPServerConfig(
                    alias="offline",
                    endpoint="http://127.0.0.1:9000",
                    retryable_effects=True,
                    timeout_seconds=7,
                    remote_tools=("render-document",),
                ),
                MCPServerConfig(
                    alias="office",
                    endpoint="http://127.0.0.1:9100",
                    retryable_effects=True,
                    timeout_seconds=7,
                    remote_tools=("render-document",),
                ),
            ),
            artifact_threshold_bytes=4_096,
            max_result_bytes=8_192,
            max_artifact_bytes=projected.artifacts.max_artifact_bytes,
        ),
    )

    async def scenario() -> tuple[str, ...]:
        dependencies = await build_task_worker_dependencies(config)
        try:
            assert active is True
            return tuple(dependencies.mcp_tool_names)
        finally:
            await dependencies.dispose()

    assert asyncio.run(scenario()) == ("mcp_office_render_document",)
    assert active is False
    assert lifecycle == ["failed", "enter", "exit"]


def test_mcp_connection_negotiation_obeys_the_server_startup_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_workbench.apps.task_worker.composition as composition

    _patch_real_runtime(monkeypatch)
    lifecycle: list[str] = []
    never = asyncio.Event()

    @asynccontextmanager
    async def connect(
        endpoint: str, *, timeout_seconds: int
    ) -> AsyncGenerator[_DirectoryMCPClient]:
        if endpoint == "http://127.0.0.1:9000":
            assert timeout_seconds == 1
            lifecycle.append("hang-enter")
            try:
                await never.wait()
            finally:
                lifecycle.append("hang-cancelled")
            raise AssertionError("unreachable")
        lifecycle.append("enter")
        try:
            yield _DirectoryMCPClient()
        finally:
            lifecycle.append("exit")

    monkeypatch.setattr(composition, "connect_mcp_client", connect)
    projected = _projected_config()
    config = replace(
        projected,
        mcp=MCPConfig(
            servers=(
                MCPServerConfig(
                    alias="hanging",
                    endpoint="http://127.0.0.1:9000",
                    retryable_effects=True,
                    timeout_seconds=1,
                    remote_tools=("render-document",),
                ),
                MCPServerConfig(
                    alias="office",
                    endpoint="http://127.0.0.1:9100",
                    retryable_effects=True,
                    timeout_seconds=7,
                    remote_tools=("render-document",),
                ),
            ),
            artifact_threshold_bytes=4_096,
            max_result_bytes=8_192,
            max_artifact_bytes=projected.artifacts.max_artifact_bytes,
        ),
    )

    async def scenario() -> tuple[str, ...]:
        dependencies = await build_task_worker_dependencies(config)
        try:
            return tuple(dependencies.mcp_tool_names)
        finally:
            await dependencies.dispose()

    assert asyncio.run(scenario()) == ("mcp_office_render_document",)
    assert lifecycle == ["hang-enter", "hang-cancelled", "enter", "exit"]


def test_mcp_connection_closes_immediately_when_discovery_admits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_workbench.apps.task_worker.composition as composition

    _patch_real_runtime(monkeypatch)
    lifecycle: list[str] = []

    @asynccontextmanager
    async def connect(
        endpoint: str, *, timeout_seconds: int
    ) -> AsyncGenerator[_DirectoryMCPClient]:
        del endpoint, timeout_seconds
        lifecycle.append("enter")
        try:
            yield _DirectoryMCPClient()
        finally:
            lifecycle.append("exit")

    async def discover_nothing(**_: object) -> tuple[()]:
        return ()

    monkeypatch.setattr(composition, "connect_mcp_client", connect)
    monkeypatch.setattr(composition, "discover_bindings", discover_nothing)
    projected = _projected_config()
    config = replace(
        projected,
        mcp=MCPConfig(
            servers=(
                MCPServerConfig(
                    alias="empty",
                    endpoint="http://127.0.0.1:9100",
                    retryable_effects=True,
                    timeout_seconds=7,
                    remote_tools=("render-document",),
                ),
            ),
            artifact_threshold_bytes=4_096,
            max_result_bytes=8_192,
            max_artifact_bytes=projected.artifacts.max_artifact_bytes,
        ),
    )

    async def scenario() -> None:
        dependencies = await build_task_worker_dependencies(config)
        try:
            assert dependencies.mcp_tool_names == ()
            assert lifecycle == ["enter", "exit"]
        finally:
            await dependencies.dispose()

    asyncio.run(scenario())

    assert lifecycle == ["enter", "exit"]


def test_mcp_connection_rolls_back_when_later_worker_assembly_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_workbench.apps.task_worker.composition as composition

    _patch_real_runtime(monkeypatch)
    lifecycle: list[str] = []

    @asynccontextmanager
    async def connect(
        endpoint: str, *, timeout_seconds: int
    ) -> AsyncGenerator[_DirectoryMCPClient]:
        del endpoint, timeout_seconds
        lifecycle.append("enter")
        try:
            yield _DirectoryMCPClient()
        finally:
            lifecycle.append("exit")

    def fail_after_mcp(*_: object, **__: object) -> None:
        raise RuntimeError("workflow assembly failed after MCP discovery")

    monkeypatch.setattr(composition, "connect_mcp_client", connect)
    monkeypatch.setattr(composition, "build_task_v1_handlers", fail_after_mcp)
    projected = _projected_config()
    config = replace(
        projected,
        mcp=MCPConfig(
            servers=(
                MCPServerConfig(
                    alias="office",
                    endpoint="http://127.0.0.1:9100",
                    retryable_effects=True,
                    timeout_seconds=7,
                    remote_tools=("render-document",),
                ),
            ),
            artifact_threshold_bytes=4_096,
            max_result_bytes=8_192,
            max_artifact_bytes=projected.artifacts.max_artifact_bytes,
        ),
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="after MCP discovery"):
            await build_task_worker_dependencies(config)

    asyncio.run(scenario())

    assert lifecycle == ["enter", "exit"]


def test_real_worker_assembles_the_human_gate_and_the_ledger_that_answers_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployed Worker must be able to ask, and to hear the answer.

    Both halves are liveness rather than safety. Without the node the router
    already fails closed -- a graph with no approval handler stops at the gate
    instead of exporting -- and without the ledger the Worker parks every
    interrupted Task. Neither loses the human, and both bring every Task to a
    standstill, which is worth catching here rather than in a deployment.
    """

    _patch_real_runtime(monkeypatch)

    async def scenario() -> tuple[bool, bool, bool]:
        dependencies = await build_task_worker_dependencies(_projected_config())
        try:
            return (
                "approval" in dependencies.handlers,
                dependencies.worker.approvals is dependencies.approvals,
                # The control group for the node: the factory that builds every
                # other handler does not build this one, so its presence is the
                # composition root's doing rather than something inherited.
                "approval"
                not in build_task_v1_handlers(
                    executor=FakeModel(()),  # type: ignore[arg-type]
                    artifacts=dependencies.artifacts,
                    invocations=None,  # type: ignore[arg-type]
                ),
            )
        finally:
            await dependencies.dispose()

    assert asyncio.run(scenario()) == (True, True, True)


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


def test_serve_awaits_assembly_and_disposes_if_runner_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_workbench.apps.task_worker.main as task_worker_main

    lifecycle: list[str] = []
    settings = object()
    config = SimpleNamespace(
        task=SimpleNamespace(claim_poll_seconds=0.01),
        worker_concurrency=1,
    )

    class Worker:
        async def run_once(self) -> None:
            return None

    class Dependencies:
        worker = Worker()

        async def startup(self) -> None:
            lifecycle.append("startup")

        async def dispose(self) -> None:
            lifecycle.append("dispose")

    dependencies = Dependencies()

    async def assemble(configured: object, *, demo: bool) -> Dependencies:
        assert configured is config
        assert demo is True
        lifecycle.append("assemble")
        await asyncio.sleep(0)
        return dependencies

    class Runner:
        def __init__(self, **_: object) -> None:
            lifecycle.append("runner")
            raise RuntimeError("runner setup failed")

    def load() -> object:
        return settings

    def project(_: object) -> SimpleNamespace:
        return config

    def install_signals(_: asyncio.Event) -> None:
        lifecycle.append("signals")

    monkeypatch.setattr(task_worker_main, "load_settings", load)
    monkeypatch.setattr(task_worker_main, "project_task_worker", project)
    monkeypatch.setattr(task_worker_main, "build_task_worker_dependencies", assemble)
    monkeypatch.setattr(
        task_worker_main,
        "_install_shutdown_handlers",
        install_signals,
    )
    monkeypatch.setattr(task_worker_main, "TaskWorkerRunner", Runner)

    with pytest.raises(RuntimeError, match="runner setup failed"):
        asyncio.run(task_worker_main.serve(demo=True))

    assert lifecycle == [
        "assemble",
        "signals",
        "runner",
        "dispose",
    ]
