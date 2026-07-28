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
