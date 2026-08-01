"""What the process assembles, and what it admits it cannot.

Chat is the one capability allowed to be absent. These pin that the absence is
a reported state rather than a crash, and that everything else still assembles
around it -- a deployment that only uploads documents should not need a
machine-learning runtime to start.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_workbench.apps.api.dependencies import build_dependencies
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"


def _settings(root: Path, **overrides: Any) -> Settings:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    with DEFAULT_CONFIG_FILE.open("rb") as handle:
        payload: dict[str, Any] = tomllib.load(handle)
    payload["database"].update(dsn=dsn, guard_dsn=dsn, listen_dsn=dsn)
    payload["model"]["main"]["model_id"] = "deepseek-chat"
    payload["model"]["compact"]["model_id"] = "deepseek-chat"
    payload["artifact_store"]["local_root"] = str(root)
    payload["secrets"] = {"deepseek_api_key": "sk-unit-test"}
    for section, values in overrides.items():
        payload[section].update(values)
    return Settings(**payload)


def test_the_process_assembles_without_the_embedding_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uploads and artifacts must not need a machine-learning stack to start."""

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    dependencies = build_dependencies(project_api(_settings(tmp_path)))
    try:
        assert dependencies.uploads is not None
        assert dependencies.artifacts is not None
    finally:
        pass


def test_chat_is_absent_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reported once at startup, not rediscovered per request."""

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.serves_chat is False
    assert dependencies.chat is None
    assert dependencies.chat_unavailable is not None
    assert "--extra embedding" in dependencies.chat_unavailable
    assert dependencies.chat_reaper is not None
    assert dependencies.chat_pending_recovery is not None


def test_nothing_is_substituted_for_the_missing_embedder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision, stated where a future change would have to break it.

    A stand-in embedder would make chat answer from vectors that mean nothing,
    which is worse than not answering.
    """

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.chat is None


def test_a_missing_reranker_does_not_cost_the_chat_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The asymmetry, stated where a future change would have to break it.

    A missing embedder removes chat, because nothing can turn a question into a
    query. A missing reranker only makes the answers worse, and treating the
    two the same way would take a whole capability offline over a quality step.

    Both factories are substituted because the real ones read the same optional
    extra: with the extra absent the embedder fails first and returns early, so
    a test that only removed the runtime would never reach the reranker branch
    and would pass whatever that branch did.
    """

    from agent_workbench.apps.api import dependencies as assembly
    from agent_workbench.bootstrap.reranker_factory import RerankerUnavailable
    from agent_workbench.bootstrap.sparse_factory import SparseEncodingUnavailable

    class _Embedder:
        dimension = 1024
        identity = "stub@v1"

        async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Any, ...]:
            return tuple((0.0,) for _ in texts)

        async def embed_query(self, text: str) -> Any:
            return (0.0,)

    monkeypatch.setattr(assembly, "build_embedder", lambda _c: _Embedder())
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c: RerankerUnavailable(reason="no reranking runtime here"),
    )
    monkeypatch.setattr(
        assembly,
        "build_sparse_encoder",
        lambda _c: SparseEncodingUnavailable(reason="no lexical runtime here"),
    )

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.serves_chat is True
    assert dependencies.chat_unavailable is None
    assert dependencies.reranker_unavailable == "no reranking runtime here"
    assert dependencies.sparse_unavailable == "no lexical runtime here"


def test_an_unreranked_process_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing about a response reveals whether reranking ran.

    So the process has to record it. An ablation report written against a
    silently unreranked deployment would attribute the difference to the model.
    """

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    # Chat is unavailable here for the embedder's reason, and the reranker was
    # never reached -- so the note is absent rather than misattributed.
    assert dependencies.reranker_unavailable is None


def _stub_optional_runtimes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the three optional model runtimes chat assembly loads."""

    from agent_workbench.apps.api import dependencies as assembly
    from agent_workbench.bootstrap.reranker_factory import RerankerUnavailable
    from agent_workbench.bootstrap.sparse_factory import SparseEncodingUnavailable

    class _Embedder:
        dimension = 1024
        identity = "stub@v1"

        async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Any, ...]:
            return tuple((0.0,) for _ in texts)

        async def embed_query(self, text: str) -> Any:
            return (0.0,)

    monkeypatch.setattr(assembly, "build_embedder", lambda _c: _Embedder())
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c: RerankerUnavailable(reason="no reranking runtime here"),
    )
    monkeypatch.setattr(
        assembly,
        "build_sparse_encoder",
        lambda _c: SparseEncodingUnavailable(reason="no lexical runtime here"),
    )


def test_the_default_deployment_assembles_the_fixed_shape_with_no_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control group for the agentic assembly below.

    Both halves matter: the shape, and the fact that this one advertises
    nothing. A registry that quietly held the search tool would leave the
    envelope as the only thing keeping the two shapes apart.
    """

    from agent_workbench.application.chat_execution import FixedTwoStepExecution

    _stub_optional_runtimes(monkeypatch)
    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.chat is not None
    execution = dependencies.chat.execution
    assert isinstance(execution, FixedTwoStepExecution)
    assert execution.budget.max_steps == 1


def test_the_agentic_deployment_grants_the_tool_a_budget_and_a_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three or none.

    A tool with a one-step budget is a tool the model can propose and never get
    an answer from; a tool whose searches nothing journals is an answer that
    cannot be fenced. A sabotage round removed the journal from this assembly
    and every other test stayed green, which is why this one reaches into the
    binding rather than trusting the shape's name.
    """

    from agent_workbench.application.chat_execution import AgenticExecution

    _stub_optional_runtimes(monkeypatch)
    dependencies = build_dependencies(
        project_api(_settings(tmp_path, chat={"retrieval_shape": "agentic"}))
    )

    assert dependencies.chat is not None
    execution = dependencies.chat.execution
    assert isinstance(execution, AgenticExecution)
    assert execution.tool_names == ("knowledge_search",)
    # A loop needs room to loop.
    assert execution.budget.max_steps > 1
    assert execution.budget.max_tool_calls >= execution.budget.max_steps
    # And the tool the model will actually reach writes into the journal this
    # execution reads. Different objects here would fence nothing.
    binding = execution.executor._gateway._registry.get("knowledge_search")
    assert binding is not None
    assert binding.handler.__self__.journal is execution.journal


def test_a_process_with_a_lexical_runtime_assembles_the_hybrid_retriever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other assembly test stubs the sparse runtime as *absent*.

    That leaves the positive case unasserted: a process that has a lexical
    runtime and wires only the dense arm would pass all of them, and would then
    be evaluated as "hybrid" while retrieving one way. The retriever names what
    it is, so this asks it.
    """

    from agent_workbench.apps.api import dependencies as assembly
    from agent_workbench.bootstrap.reranker_factory import RerankerUnavailable

    class _Embedder:
        dimension = 1024
        identity = "stub@v1"

        async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Any, ...]:
            return tuple((0.0,) for _ in texts)

        async def embed_query(self, text: str) -> Any:
            return (0.0,)

    class _Sparse:
        identity = "stub-lexical@v1"

        async def encode_query(self, text: str) -> Any:  # pragma: no cover - unused
            raise AssertionError("assembly must not encode anything")

    monkeypatch.setattr(assembly, "build_embedder", lambda _c: _Embedder())
    monkeypatch.setattr(assembly, "build_sparse_encoder", lambda _c: _Sparse())
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c: RerankerUnavailable(reason="no reranking runtime here"),
    )

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.chat is not None
    assert dependencies.sparse_unavailable is None
    retrieval = dependencies.chat.execution.retrieval
    assert retrieval.sparse_encoder is not None
    # The name is what an evaluation report prints. A dense-only process
    # labelled "hybrid" is a benchmark that credits fusion for nothing.
    assert retrieval.mode == "hybrid"


def test_the_agentic_shape_gets_the_same_hybrid_retriever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One retrieval service, both shapes.

    Two retrievers would be two sets of authorization checks and two things to
    evaluate, and the one that got less attention would be the one that leaked.
    """

    from agent_workbench.apps.api import dependencies as assembly
    from agent_workbench.bootstrap.reranker_factory import RerankerUnavailable

    class _Embedder:
        dimension = 1024
        identity = "stub@v1"

        async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Any, ...]:
            return tuple((0.0,) for _ in texts)

        async def embed_query(self, text: str) -> Any:
            return (0.0,)

    class _Sparse:
        identity = "stub-lexical@v1"

        async def encode_query(self, text: str) -> Any:  # pragma: no cover - unused
            raise AssertionError("assembly must not encode anything")

    monkeypatch.setattr(assembly, "build_embedder", lambda _c: _Embedder())
    monkeypatch.setattr(assembly, "build_sparse_encoder", lambda _c: _Sparse())
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c: RerankerUnavailable(reason="no reranking runtime here"),
    )

    dependencies = build_dependencies(
        project_api(_settings(tmp_path, chat={"retrieval_shape": "agentic"}))
    )

    assert dependencies.chat is not None
    binding = dependencies.chat.execution.executor._gateway._registry.get(
        "knowledge_search"
    )
    assert binding is not None
    assert binding.handler.__self__.retrieval.mode == "hybrid"


def test_assembly_hands_startup_the_encoders_a_turn_will_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not any encoders -- the ones retrieval actually holds.

    A sabotage round emptied this tuple and every other test stayed green: the
    process started, served, and paid a 29-second first encode on somebody's
    request. Asserting identity rather than count is what makes that impossible.
    """

    from agent_workbench.apps.api import dependencies as assembly
    from agent_workbench.bootstrap.reranker_factory import RerankerUnavailable

    class _Embedder:
        dimension = 1024
        identity = "stub@v1"

        async def embed_documents(self, texts: tuple[str, ...]) -> tuple[Any, ...]:
            return tuple((0.0,) for _ in texts)

        async def embed_query(self, text: str) -> Any:
            return (0.0,)

    class _Sparse:
        identity = "stub-lexical@v1"

        async def encode_query(self, text: str) -> Any:  # pragma: no cover
            raise AssertionError("assembly must not encode anything")

    monkeypatch.setattr(assembly, "build_embedder", lambda _c: _Embedder())
    monkeypatch.setattr(assembly, "build_sparse_encoder", lambda _c: _Sparse())
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c: RerankerUnavailable(reason="no reranking runtime here"),
    )

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.chat is not None
    retrieval = dependencies.chat.execution.retrieval
    assert retrieval.embedder in dependencies.encoders
    assert retrieval.sparse_encoder in dependencies.encoders


def test_startup_warms_before_a_request_can_arrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of holding them.

    Removing the call from `startup` changed no other test, which is how a
    29-second first encode gets rediscovered by a user instead of a suite.
    """

    import dataclasses

    from agent_workbench.apps.api import dependencies as assembly

    warmed: list[tuple[object, ...]] = []

    async def record(*encoders: object) -> None:
        warmed.append(encoders)

    monkeypatch.setattr(assembly, "warm_encoders", record)

    sentinel = object()
    # Built by hand rather than assembled: this asserts one behaviour of
    # startup, and no Qdrant is needed to see it.
    fields = {f.name: None for f in dataclasses.fields(assembly.ApiDependencies)}
    fields["encoders"] = (sentinel,)
    deps = assembly.ApiDependencies(**fields)  # type: ignore[arg-type]

    asyncio.run(deps.startup())

    assert warmed == [(sentinel,)]


def test_a_missing_model_costs_chat_but_not_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason `/v1/search` exists.

    Retrieval used to be assembled after the model, so a deployment with no
    provider key could index documents and then have no way to look at them --
    the vectors were there and unreachable. A missing model is now a missing
    model.
    """

    from agent_workbench.apps.api import dependencies as assembly
    from agent_workbench.bootstrap.model_factory import ModelNotConfiguredError

    _stub_optional_runtimes(monkeypatch)

    def unconfigured(*_a: Any, **_k: Any) -> Any:
        raise ModelNotConfiguredError("no key here")

    monkeypatch.setattr(assembly, "build_model", unconfigured)

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.chat is None
    assert dependencies.serves_chat is False
    assert dependencies.chat_unavailable is not None and "no key" in (
        dependencies.chat_unavailable
    )
    # The half that needs no provider survives.
    assert dependencies.serves_search is True
    assert dependencies.retrieval is not None
    assert dependencies.encoders  # and startup still has something to warm


def test_a_process_without_retrieval_does_not_publish_the_search_route(
    tmp_path: Path,
) -> None:
    """404, not a 409 from inside the handler.

    A route that exists and always refuses is a worse answer than one that is
    not there: a client discovers the first per request and the second once.
    """

    from agent_workbench.apps.api.main import build_app

    app, dependencies = build_app(project_api(_settings(tmp_path)), with_chat=False)
    try:
        assert dependencies.serves_search is False

        async def call() -> Any:
            transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
            async with httpx.AsyncClient(
                transport=transport, base_url="http://api.test"
            ) as client:
                return await client.post(
                    "/v1/search",
                    headers={
                        "x-tenant-id": "tenant_a",
                        "x-principal-id": "user_1",
                        "content-type": "application/json",
                    },
                    json={"query": "anything", "knowledge_base_id": "kb"},
                )

        assert asyncio.run(call()).status_code == 404
    finally:
        asyncio.run(dependencies.dispose())


@pytest.mark.parametrize("shape", ["fixed", "agentic"])
def test_the_runtime_reports_the_model_that_actually_answered(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An event log may not disagree with what happened.

    ``ClaudeLikeAgentRuntime`` carries a placeholder label for the scripted
    model the walking skeleton uses, and the API assembled around it without
    passing one -- so every ``ModelStarted`` this deployment wrote named a fake
    while a real provider was being called and billed. Somebody reading the
    stream to find out which model produced an answer would have been told the
    wrong thing, which is worse than being told nothing.

    Both shapes, because they build separate executors and only one of them was
    ever going to be noticed.
    """

    _stub_optional_runtimes(monkeypatch)
    dependencies = build_dependencies(
        project_api(_settings(tmp_path, chat={"retrieval_shape": shape}))
    )

    assert dependencies.chat is not None
    executor = dependencies.chat.execution.executor
    assert executor._model_label == "deepseek-chat"  # pyright: ignore[reportPrivateUsage]
