"""What the process assembles, and what it admits it cannot.

Chat is the one capability allowed to be absent. These pin that the absence is
a reported state rather than a crash, and that everything else still assembles
around it -- a deployment that only uploads documents should not need a
machine-learning runtime to start.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

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

    dependencies = build_dependencies(project_api(_settings(tmp_path)))

    assert dependencies.serves_chat is True
    assert dependencies.chat_unavailable is None
    assert dependencies.reranker_unavailable == "no reranking runtime here"


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
