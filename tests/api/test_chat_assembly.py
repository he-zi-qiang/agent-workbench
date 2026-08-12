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


def test_a_missing_embedder_costs_retrieval_and_not_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reported once at startup, and scoped to the half that is actually gone.

    Direct chat reaches no index, so an absent embedding runtime is not a
    reason to withdraw it. It used to be: the whole ``/v1/chat`` router
    disappeared, which meant the deployment least able to run a RAG stack was
    also the one that could not answer a plain question -- the one thing it was
    still fully equipped to do.

    What must stay reported is the *grounded* half, and in the shape the route
    reads: `effective_retrieval_shape` is what decides whether a RAG request is
    refused with a 422 or accepted and then failed underneath.
    """

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.serves_chat is True
    assert dependencies.chat is not None
    assert dependencies.chat_unavailable is None
    assert dependencies.rag_unavailable is not None
    assert "--extra embedding" in dependencies.rag_unavailable
    assert dependencies.effective_retrieval_shape == "ungrounded"
    # Nothing to retrieve from, so /v1/search is not mounted either.
    assert dependencies.serves_search is False
    assert dependencies.chat_reaper is not None
    assert dependencies.chat_pending_recovery is not None


def test_nothing_is_substituted_for_the_missing_embedder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision, stated where a future change would have to break it.

    A stand-in embedder would make chat answer from vectors that mean nothing,
    which is worse than not answering. Serving Direct is not that: it answers
    from the model alone and says so, rather than from an index that is not
    there. So what may not exist here is *retrieval*, and the grounded
    execution built on it.
    """

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.retrieval is None
    assert dependencies.vector_index is None
    assert dependencies.encoders == ()
    selector = dependencies.chat.execution if dependencies.chat is not None else None
    assert selector is not None
    assert selector.rag is None
    assert selector.direct is not None


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

    monkeypatch.setattr(assembly, "build_embedder", lambda _c, **_: _Embedder())
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c, **_: RerankerUnavailable(reason="no reranking runtime here"),
    )
    monkeypatch.setattr(
        assembly,
        "build_sparse_encoder",
        lambda _c, **_: SparseEncodingUnavailable(reason="no lexical runtime here"),
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

    monkeypatch.setattr(assembly, "build_embedder", lambda _c, **_: _Embedder())
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c, **_: RerankerUnavailable(reason="no reranking runtime here"),
    )
    monkeypatch.setattr(
        assembly,
        "build_sparse_encoder",
        lambda _c, **_: SparseEncodingUnavailable(reason="no lexical runtime here"),
    )


def test_the_default_deployment_assembles_the_fixed_shape_with_no_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control group for the agentic assembly below.

    Both halves matter: the shape, and the fact that this one advertises
    nothing. A registry that quietly held the search tool would leave the
    envelope as the only thing keeping the two shapes apart.
    """

    from agent_workbench.application.chat_execution import (
        AnswerModeSelector,
        FixedTwoStepExecution,
        UngroundedExecution,
    )

    _stub_optional_runtimes(monkeypatch)
    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.chat is not None
    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    assert isinstance(selector.direct, UngroundedExecution)
    assert isinstance(selector.rag, FixedTwoStepExecution)
    assert selector.rag.budget.max_steps == 1


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

    from agent_workbench.application.chat_execution import (
        AgenticExecution,
        AnswerModeSelector,
        UngroundedExecution,
    )

    _stub_optional_runtimes(monkeypatch)
    dependencies = build_dependencies(
        project_api(_settings(tmp_path, chat={"retrieval_shape": "agentic"}))
    )

    assert dependencies.chat is not None
    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    assert isinstance(selector.direct, UngroundedExecution)
    execution = selector.rag
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


def test_the_legacy_ungrounded_deployment_assembles_direct_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_workbench.application.chat_execution import (
        AnswerModeSelector,
        UngroundedExecution,
    )

    _stub_optional_runtimes(monkeypatch)
    dependencies = build_dependencies(
        project_api(_settings(tmp_path, chat={"retrieval_shape": "ungrounded"}))
    )

    assert dependencies.chat is not None
    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    assert isinstance(selector.direct, UngroundedExecution)
    assert selector.rag is None


def test_the_routed_deployment_assembles_direct_beside_its_rag_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_workbench.application.chat_execution import (
        AnswerModeSelector,
        RoutedExecution,
        UngroundedExecution,
    )
    from agent_workbench.apps.api import dependencies as assembly

    class _Reranker:
        identity = "stub-reranker@v1"

        async def rerank(
            self, query: str, passages: tuple[str, ...]
        ) -> tuple[float, ...]:  # pragma: no cover - assembly only
            del query
            return tuple(1.0 for _ in passages)

    _stub_optional_runtimes(monkeypatch)
    monkeypatch.setattr(assembly, "build_reranker", lambda _c, **_: _Reranker())
    dependencies = build_dependencies(
        project_api(_settings(tmp_path, chat={"retrieval_shape": "routed"}))
    )

    assert dependencies.chat is not None
    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    assert isinstance(selector.direct, UngroundedExecution)
    assert isinstance(selector.rag, RoutedExecution)


def test_the_web_fallback_budget_enforces_the_prompt_it_ships_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Twice at most" has to be a ceiling, because prompt text is not one.

    Measured against the shipped prompt at `max_steps=6`: the model rephrased
    the same question five times, spent ~14s of provider time on each, and the
    run died at its ceiling having written nothing. The instruction was clear
    and the model ignored it, which is the ordinary case, not a surprising one.

    Three model turns is the arithmetic of the sentence in the prompt: search,
    search, answer. It is asserted here rather than trusted, because the number
    lives in assembly and the sentence lives in the application module, and
    nothing else would notice them drifting apart.
    """

    from agent_workbench.application.chat_execution import (
        WEB_FALLBACK_SYSTEM_PROMPT,
        AnswerModeSelector,
        RoutedExecution,
    )
    from agent_workbench.apps.api import dependencies as assembly

    class _Reranker:
        identity = "stub-reranker@v1"

        async def rerank(
            self, query: str, passages: tuple[str, ...]
        ) -> tuple[float, ...]:  # pragma: no cover - assembly only
            del query
            return tuple(1.0 for _ in passages)

    _stub_optional_runtimes(monkeypatch)
    monkeypatch.setattr(assembly, "build_reranker", lambda _c, **_: _Reranker())
    dependencies = build_dependencies(
        project_api(
            _settings(
                tmp_path,
                chat={"retrieval_shape": "routed"},
                research={"enabled": True},
            )
        )
    )

    assert dependencies.chat is not None
    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    execution = selector.rag
    assert isinstance(execution, RoutedExecution)

    # Read through `fallback` since ADR-023: the branch that may search is an
    # `UngroundedExecution`, the same class the direct shape is. The numbers
    # asserted are unchanged, which is the point -- the merge moved where they
    # live, not what they are.
    fallback = execution.fallback
    # The tool is built, so the ceiling below is the one that will bind.
    assert fallback.web_tool_names == ("web_search",)
    assert fallback.web_executor is not None
    assert fallback.web_budget is not None
    # Two searches, and the turn that answers from them. The tool ceiling sits
    # *below* the step ceiling on purpose (ADR-022): that is what makes the
    # third step a model with nothing left to call, rather than a model
    # proposing a third search into a run that then dies holding the results of
    # the first two.
    assert fallback.web_budget.max_tool_calls == 2
    assert fallback.web_budget.max_steps == 3
    # And the sentence the ceiling is the ceiling *for*. If this stops matching,
    # one of the two moved without the other.
    assert "twice at most" in WEB_FALLBACK_SYSTEM_PROMPT
    # The routed branch keeps the corpus-miss wording. The direct shape gets the
    # other one, and a swap here would tell a model that a knowledge base it was
    # never given had nothing for it.
    assert fallback.web_system_prompt == WEB_FALLBACK_SYSTEM_PROMPT


def test_the_direct_shape_reaches_the_web_when_a_provider_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-023, asserted where it is actually decided.

    The application-level tests prove an `UngroundedExecution` *given* a web
    executor behaves correctly. Only assembly decides whether the console's
    default mode is given one, and that was the whole defect: the tool existed,
    the scope was granted, the shape was configured, and the mode the user
    lands in still could not search because these four lines were inside the
    `routed` branch.

    The shared journal is asserted for a reason that is easy to miss. The tool
    binding writes its verdict into exactly *one* journal. Building a second one
    for the direct shape would leave whichever execution holds the other reading
    `False` forever -- a turn that searched the web reported as one that did
    not, with nothing failing anywhere.
    """

    from agent_workbench.application.chat_execution import (
        WEB_DIRECT_SYSTEM_PROMPT,
        AnswerModeSelector,
        RoutedExecution,
        UngroundedExecution,
    )
    from agent_workbench.apps.api import dependencies as assembly

    class _Reranker:
        identity = "stub-reranker@v1"

        async def rerank(
            self, query: str, passages: tuple[str, ...]
        ) -> tuple[float, ...]:  # pragma: no cover - assembly only
            del query
            return tuple(1.0 for _ in passages)

    _stub_optional_runtimes(monkeypatch)
    monkeypatch.setattr(assembly, "build_reranker", lambda _c, **_: _Reranker())
    dependencies = build_dependencies(
        project_api(
            _settings(
                tmp_path,
                chat={"retrieval_shape": "routed"},
                research={"enabled": True},
            )
        )
    )

    assert dependencies.chat is not None
    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    direct = selector.direct
    assert isinstance(direct, UngroundedExecution)

    assert direct.web_tool_names == ("web_search",)
    assert direct.web_executor is not None
    assert direct.web_budget is not None
    # The same ceiling the routed fallback gets: the sentence they share is the
    # same sentence, so the arithmetic enforcing it must be the same arithmetic.
    assert direct.web_budget.max_steps == 3
    assert direct.web_budget.max_tool_calls == 2
    # Its own wording, not the corpus-miss one.
    assert direct.web_system_prompt == WEB_DIRECT_SYSTEM_PROMPT

    # One journal, one tool, both shapes.
    assert isinstance(selector.rag, RoutedExecution)
    assert selector.rag.fallback.web_journal is direct.web_journal
    assert selector.rag.fallback.web_executor is direct.web_executor


def test_without_a_provider_the_direct_shape_gains_no_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above (ADR-021 §4).

    A deployment that configured nothing must not start calling a provider
    because someone merged two code paths. `research` defaults to disabled in
    `_settings`, so this is the ordinary checkout.
    """

    from agent_workbench.application.chat_execution import (
        AnswerModeSelector,
        UngroundedExecution,
    )

    _stub_optional_runtimes(monkeypatch)
    dependencies = build_dependencies(
        project_api(_settings(tmp_path, chat={"retrieval_shape": "ungrounded"}))
    )

    assert dependencies.chat is not None
    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    direct = selector.direct
    assert isinstance(direct, UngroundedExecution)

    assert direct.web_executor is None
    assert direct.web_tool_names == ()
    assert direct.web_budget is None


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

    monkeypatch.setattr(assembly, "build_embedder", lambda _c, **_: _Embedder())
    monkeypatch.setattr(assembly, "build_sparse_encoder", lambda _c, **_: _Sparse())
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c, **_: RerankerUnavailable(reason="no reranking runtime here"),
    )

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.chat is not None
    assert dependencies.sparse_unavailable is None
    from agent_workbench.application.chat_execution import AnswerModeSelector

    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    rag = selector.rag
    assert rag is not None
    retrieval = rag.retrieval  # pyright: ignore[reportAttributeAccessIssue]
    # The name is what an evaluation report prints, and since ADR-017 it prints
    # two facts rather than one: which arms ran, and which framework proposed
    # the candidates. A dense-only process labelled "hybrid" is a benchmark that
    # credits fusion for nothing; a LlamaIndex run labelled like the reference
    # path is a migration nobody can measure.
    assert retrieval.mode == "hybrid"


def test_turning_the_framework_on_assembles_the_llamaindex_retriever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above, and the only test of that config field.

    ``rag.llama_index.enabled`` shipped as ``true`` with no reader anywhere in
    ``src``; both settings therefore produced the same process, and the flag
    read like a decision that had been implemented. Asserting only the default
    case would leave it that way -- that assertion would pass just as well if
    the factory ignored the flag and always built the reference retriever.

    It defaults to off because ADR-017 step 3 moves traffic only on step 2's
    evidence, and that evidence came back inconclusive: tied fused scores are
    returned in an unstable order, so each retriever disagrees with itself on
    9-10 of 38 gold questions and no comparison between them can resolve
    anything narrower than that.
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

    monkeypatch.setattr(assembly, "build_embedder", lambda _c, **_: _Embedder())
    monkeypatch.setattr(assembly, "build_sparse_encoder", lambda _c, **_: _Sparse())
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c, **_: RerankerUnavailable(reason="no reranking runtime here"),
    )

    dependencies = build_dependencies(
        project_api(_settings(tmp_path, rag={"llama_index": {"enabled": True}}))
    )

    assert dependencies.chat is not None
    # Same arms, different proposer. The sparse encoder is still wired -- the
    # flag selects a retriever, not a retrieval quality.
    from agent_workbench.application.chat_execution import AnswerModeSelector

    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    rag = selector.rag
    assert rag is not None
    assert rag.retrieval.mode == "llama_index+hybrid"  # pyright: ignore[reportAttributeAccessIssue]


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

    monkeypatch.setattr(assembly, "build_embedder", lambda _c, **_: _Embedder())
    monkeypatch.setattr(assembly, "build_sparse_encoder", lambda _c, **_: _Sparse())
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c, **_: RerankerUnavailable(reason="no reranking runtime here"),
    )

    dependencies = build_dependencies(
        project_api(_settings(tmp_path, chat={"retrieval_shape": "agentic"}))
    )

    assert dependencies.chat is not None
    from agent_workbench.application.chat_execution import (
        AgenticExecution,
        AnswerModeSelector,
    )

    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    execution = selector.rag
    assert isinstance(execution, AgenticExecution)
    binding = execution.executor._gateway._registry.get("knowledge_search")
    assert binding is not None
    assert binding.handler.__self__.retrieval.mode == "hybrid"


def test_assembly_hands_startup_the_encoders_a_turn_will_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not any encoders -- the ones this process actually loaded.

    A sabotage round emptied this tuple and every other test stayed green: the
    process started, served, and paid a 29-second first encode on somebody's
    request. Asserting identity rather than count is what makes that impossible.

    Identity is now asserted against what the factories returned rather than
    read back off the retrieval service. The encoders moved behind
    ``CandidateRetrieverPort`` in ADR-017's step 1, and LlamaIndex's retriever
    does not hand them back the way the reference one did -- so this pins the
    exact objects assembly was given, which is the same claim from the other
    side. That the sparse one reached retrieval and not just the warm list is
    covered by the mode assertions above.
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

    embedder = _Embedder()
    sparse = _Sparse()
    monkeypatch.setattr(assembly, "build_embedder", lambda _c, **_: embedder)
    monkeypatch.setattr(assembly, "build_sparse_encoder", lambda _c, **_: sparse)
    monkeypatch.setattr(
        assembly,
        "build_reranker",
        lambda _c, **_: RerankerUnavailable(reason="no reranking runtime here"),
    )

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.chat is not None
    assert embedder in dependencies.encoders
    assert sparse in dependencies.encoders


def test_a_loaded_reranker_is_handed_to_startup_as_well(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third cold runtime, and the one that was left out of the warm list.

    Reranking is on by default and its cross-encoder pays the same first-forward
    toll as the two encoders beside it, but assembly only ever handed startup
    ``(embedder, sparse)``. So a deployment warmed two of the three runtimes it
    had just loaded and let the first reranked search discover the third.

    Identity, not membership by type: startup has to warm *the same instance*
    retrieval will call, since a second one would be a separate set of compiled
    kernels and the boot spent on it would buy the request nothing.
    """

    from agent_workbench.adapters.reranking.fake import LexicalOverlapReranker
    from agent_workbench.apps.api import dependencies as assembly

    _stub_optional_runtimes(monkeypatch)
    reranker = LexicalOverlapReranker()
    monkeypatch.setattr(assembly, "build_reranker", lambda _c, **_: reranker)

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert reranker in dependencies.encoders
    assert dependencies.retrieval is not None
    assert dependencies.retrieval.reranker is reranker


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
    from agent_workbench.application.chat_execution import AnswerModeSelector

    selector = dependencies.chat.execution
    assert isinstance(selector, AnswerModeSelector)
    assert selector.direct.executor._model_label == "deepseek-chat"  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    rag = selector.rag
    assert rag is not None
    assert rag.executor._model_label == "deepseek-chat"  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
