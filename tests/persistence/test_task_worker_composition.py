"""The independent Task Worker composition over its real durable stores."""

from __future__ import annotations

import asyncio
import os
import re
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from agent_workbench.adapters.langgraph import PostgresCheckpointSaver
from agent_workbench.application.task_inputs import TaskInputService, TaskInputStore
from agent_workbench.application.tasks import SubmittedSemantics, TaskService
from agent_workbench.apps.task_worker.composition import (
    TaskWorkerDependencies,
    build_task_worker_dependencies,
)
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
from agent_workbench.bootstrap.sparse_factory import SparseEncodingUnavailable
from agent_workbench.domain.messages import TextBlock
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import TokenUsage
from agent_workbench.domain.task_inputs import TaskInput
from agent_workbench.ports.model import (
    ModelEvent,
    ModelRequest,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelUsageReported,
)

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
TABLES = (
    "task_runs, events, event_streams, workflow_checkpoints, "
    "workflow_checkpoint_blobs, workflow_checkpoint_writes"
)
OWNER = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _config(root: Path) -> TaskWorkerRuntimeConfig:
    return TaskWorkerRuntimeConfig(
        database=DatabaseConfig(
            dsn=SecretStr(_dsn()),
            application_name="agent-workbench-task-worker-tests",
            statement_timeout_ms=30_000,
            pool_size=2,
            max_overflow=0,
        ),
        artifacts=ArtifactStoreConfig(
            backend="local",
            local_root=str(root),
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
            run_semantics_snapshot={"model": {"provider": "demo"}},
            run_semantics_revision="test-v1",
            policy_revision="policy-v1",
            policy_fingerprint="f" * 16,
            default_authorization_envelope=AuthorizationEnvelope(),
        ),
        worker_id="worker_composition_test",
        worker_concurrency=1,
    )


def _run(
    root: Path,
    scenario: Callable[[TaskWorkerDependencies], Awaitable[Any]],
) -> Any:
    async def execute() -> Any:
        dependencies = await build_task_worker_dependencies(_config(root), demo=True)
        try:
            async with dependencies.engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            return await scenario(dependencies)
        finally:
            await dependencies.dispose()

    return asyncio.run(execute())


def _ungrounded_config(root: Path) -> TaskWorkerRuntimeConfig:
    """The real projection, over this machine's test database.

    Deliberately `project_task_worker` rather than the hand-built config above:
    what this test is about is that the *projected* Worker -- the only kind a
    deployment ever gets -- can assemble and run without an embedding runtime,
    and a hand-built config could omit the retrieval sections that the
    projection is incapable of omitting.
    """

    with Path(DEFAULT_CONFIG_FILE).open("rb") as handle:
        payload = tomllib.load(handle)
    dsn = _dsn()
    payload["database"] = {
        **payload["database"],
        "dsn": dsn,
        "guard_dsn": dsn,
        "listen_dsn": dsn,
    }
    payload["artifact_store"] = {**payload["artifact_store"], "local_root": str(root)}
    payload["model"]["main"]["model_id"] = "composition-main"
    payload["model"]["compact"]["model_id"] = "composition-compact"
    payload["secrets"] = {"deepseek_api_key": "composition-test-key"}
    # No human is here to answer a gate, and the gate is not what this test is
    # about: with it on, `review` routes to `approval` and the run parks
    # correctly, which would prove nothing about export.
    payload["workflow"] = {**payload["workflow"], "export_requires_approval": False}
    return project_task_worker(
        Settings(**payload), worker_id="worker_ungrounded_composition"
    )


class _WorkerModel:
    """A stand-in provider that answers by what the prompt admits.

    ``ModelRequest`` carries no node id on purpose -- it names a profile, not a
    caller -- so this dispatches on the one thing v2's reviewer projection puts
    in the prompt and no other v2 node does: ``draft_ref=``.

    Reading that id back out is the point rather than a shortcut. The verdict
    has to name the draft the ``work`` node actually produced, and that id is
    minted by the artifact store while the graph is running, so no fixed script
    can contain it -- ``_REVIEW_CONTRACT`` says as much to a real model. A
    stand-in that could not satisfy the same requirement would be proving that
    the decoder had been bypassed, not that the graph ran.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        prompt = "\n".join(
            block.text
            for message in request.messages
            for block in message.content
            if isinstance(block, TextBlock)
        )
        self.prompts.append(prompt)
        yield ModelTextDelta(text=self._answer(prompt))
        usage = TokenUsage(input_tokens=12, output_tokens=6)
        yield ModelUsageReported(usage=usage)
        yield ModelStreamCompleted(finish_reason="stop", usage=usage)

    @staticmethod
    def _answer(prompt: str) -> str:
        draft = re.search(r"draft_ref=(\S+)", prompt)
        if draft is None:
            return "A short working note, produced without retrieving anything."
        revision = re.search(r"revision_number=(\d+)", prompt)
        assert revision is not None, prompt
        return (
            '{"decision":"pass","reviewed_draft_ref":"'
            + draft.group(1)
            + '","revision_number":'
            + revision.group(1)
            + ',"summary":"The note answers the objective.",'
            '"issues":[],"score":88}'
        )


def _patch_ungrounded_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> _WorkerModel:
    """A machine with no embedding runtime, and a provider that never dials."""

    import agent_workbench.apps.task_worker.composition as composition
    import agent_workbench.bootstrap.model_factory as model_factory

    model = _WorkerModel()

    def no_embedder(_: EmbeddingConfig) -> EmbeddingUnavailable:
        return EmbeddingUnavailable("the embedding extra is not installed")

    def no_sparse(_: EmbeddingConfig) -> SparseEncodingUnavailable:
        return SparseEncodingUnavailable("the embedding extra is not installed")

    def build(_: ModelConfig, *, client: httpx.AsyncClient) -> _WorkerModel:
        del client
        return model

    monkeypatch.setattr(composition, "build_embedder", no_embedder)
    monkeypatch.setattr(composition, "build_sparse_encoder", no_sparse)
    monkeypatch.setattr(model_factory, "build_model", build)
    return model


async def _final_state(
    dependencies: TaskWorkerDependencies, thread_id: str
) -> dict[str, Any]:
    """The channel values the thread ended on.

    Read through a second saver over the same engine rather than off the
    Worker's, because nothing on the workflow port exposes state -- `inspect`
    answers where a thread stopped, not what it holds.
    """

    saver = PostgresCheckpointSaver(dependencies.engine)
    checkpoint = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    assert checkpoint is not None
    return dict(checkpoint.checkpoint["channel_values"])


def _submission_service(dependencies: TaskWorkerDependencies) -> TaskInputService:
    config = dependencies.config.task
    return TaskInputService(
        inputs=TaskInputStore(dependencies.artifacts),
        tasks=TaskService(
            registry=dependencies.registry,
            events=dependencies.events,
            graph_version=config.graph_version,
            semantics=lambda: SubmittedSemantics(
                run_semantics_snapshot=config.run_semantics_snapshot,
                run_semantics_revision=config.run_semantics_revision,
                policy_revision=config.policy_revision,
                policy_fingerprint=config.policy_fingerprint,
                authorization_envelope=config.default_authorization_envelope,
            ),
        ),
    )


def test_demo_worker_composition_claims_and_completes_a_submitted_task(
    tmp_path: Path,
) -> None:
    async def scenario(dependencies: TaskWorkerDependencies) -> tuple[str, list[str]]:
        # The advisory guard must own a different physical session from the
        # Registry/checkpointer engine even when the configuration falls back
        # to the same DSN. Session locks cannot survive transaction pooling.
        guard = await dependencies.guards.acquire(
            task_id="task_composition_guard",
            worker_id="worker_composition_guard",
            epoch=1,
        )
        try:
            async with dependencies.engine.connect() as connection:
                query_pid = int(
                    (
                        await connection.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                )
            assert guard.backend_pid != query_pid
        finally:
            await guard.release()

        submitted = await _submission_service(dependencies).submit(
            principal=OWNER,
            task_input=TaskInput(objective="Draft a retrieval architecture brief."),
            submission_dedup_key="dedup_1",
        )
        # One claim channel for the process. The Worker publishes into it and
        # the real handler set reads out of it; two objects would be a Worker
        # announcing its lease to nobody, and every node refusing.
        assert dependencies.worker.scope is dependencies.scope
        outcome = await dependencies.worker.run_once()
        assert outcome is not None
        assert outcome.task.task_id == submitted.task_id
        return outcome.final_status, [decision.action for decision in outcome.decisions]

    status, actions = _run(tmp_path, scenario)

    assert status == "succeeded"
    assert actions == ["start", "settle_succeeded"]


def test_an_ungrounded_worker_completes_a_v2_task_that_names_no_knowledge_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Work a machine without the embedding extra could not run at all.

    The entrypoint test proves such a Worker assembles. That is not the thing
    the defect cost anybody: what it cost was an ordinary Task -- no knowledge
    base, no index, nothing to embed -- which could not run because assembly
    refused before reaching it. So this drives the real handlers, over the real
    Registry, checkpointer and event log, to a settled terminal status.

    Three assertions, and only the first is about it finishing:

    * ``succeeded`` with an ``export_ref`` -- the Task produced its file;
    * ``evidence_refs`` empty -- it did not reach v1's research fallbacks and
      write model prose where retrieved evidence goes, which is the failure
      `_build_real_handlers` restricts the graph registry to prevent;
    * a v1 submission on the same Worker parks for migration rather than
      running with those fallbacks -- the same guarantee from the other side,
      and the half that has no other test.
    """

    model = _patch_ungrounded_runtime(monkeypatch)
    # The demo graph answers its own export; this one goes through the real
    # tool gateway, which checks `artifact:export` against the scopes frozen
    # onto the submission. A principal without it is refused at export, which
    # would look like the graph failing rather than like the test under-scoping
    # its submitter.
    exporter = PrincipalContext(
        principal_id=OWNER.principal_id,
        tenant_id=OWNER.tenant_id,
        scopes=("artifact:export",),
    )

    async def scenario() -> tuple[Any, ...]:
        dependencies = await build_task_worker_dependencies(
            _ungrounded_config(tmp_path)
        )
        try:
            async with dependencies.engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            submissions = _submission_service(dependencies)
            general = await submissions.submit(
                principal=exporter,
                task_input=TaskInput(
                    objective="Summarise the attached note in three lines.",
                    # The whole point of the shape: nothing to retrieve from.
                    knowledge_base_id=None,
                    wants_report=True,
                    max_revisions=1,
                ),
                submission_dedup_key="dedup_ungrounded_v2",
                graph="general",
            )
            done = await dependencies.worker.run_once()
            assert done is not None and done.task.task_id == general.task_id
            state = await _final_state(dependencies, general.thread_id)

            research = await submissions.submit(
                principal=exporter,
                task_input=TaskInput(objective="Write a research brief."),
                submission_dedup_key="dedup_ungrounded_v1",
                graph="research",
            )
            parked = await dependencies.worker.run_once()
            assert parked is not None and parked.task.task_id == research.task_id
            return (
                done.final_status,
                state.get("export_ref"),
                state.get("evidence_refs"),
                dependencies.grounding_unavailable,
                parked.final_status,
                [decision.action for decision in parked.decisions],
            )
        finally:
            await dependencies.dispose()

    status, export_ref, evidence, grounding, v1_status, v1_actions = asyncio.run(
        scenario()
    )

    assert status == "succeeded"
    assert export_ref is not None
    # Normalised because the channel round-trips through the checkpoint's
    # serialiser and comes back a list. What is being asserted is that nothing
    # was ever appended: a v1 research fallback would have written the model's
    # own prose here and let the report cite it as retrieved evidence.
    assert tuple(evidence or ()) == ()
    assert grounding == "the embedding extra is not installed"

    assert v1_status == "waiting_migration"
    assert v1_actions == ["wait_for_migration"]

    # Understand, work, review. Export goes through the tool gateway, not the
    # provider, and the parked v1 Task reaches no node at all -- so a fourth
    # call means either a node re-asked after a decode failure or a graph ran
    # that should not have. Last, so the two assertions above get to name the
    # failure first when a Worker starts registering v1 again.
    assert len(model.prompts) == 3
