"""Checked-in local profile contract for the Word MCP demonstration."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_workbench.bootstrap.projections import project_task
from agent_workbench.bootstrap.settings import Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONFIG = ROOT / "config/config.local.toml"
WORD_CONFIG = ROOT / "config/config.word-local.toml"
POSTGRES_DSN = (
    "postgresql+asyncpg://agent:local-profile-test@127.0.0.1:5433/agent_workbench_local"
)


def _load_profile(monkeypatch: pytest.MonkeyPatch, path: Path) -> Settings:
    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", POSTGRES_DSN)
    return load_settings(config_file=path)


def test_ordinary_local_profile_does_not_widen_tasks_with_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_profile(monkeypatch, LOCAL_CONFIG)

    assert settings.optional_labs.mcp_adapter is False
    assert settings.mcp.servers == ()
    assert "mcp_word_render_document" not in (
        project_task(settings).default_authorization_envelope.allowed_tools
    )


def test_explicit_word_profile_enables_only_the_word_mcp_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_profile(monkeypatch, WORD_CONFIG)

    assert settings.optional_labs.mcp_adapter is True
    assert len(settings.mcp.servers) == 1
    server = settings.mcp.servers[0]
    assert server.alias == "word"
    assert server.transport == "http"
    assert server.endpoint == "http://127.0.0.1:8765/mcp"
    assert server.tools == ("render_document",)
    assert server.retryable_effects is True
    assert settings.model.main.tool_calling_required is False

    envelope = project_task(settings).default_authorization_envelope
    assert "mcp_word_render_document" in envelope.allowed_tools


def test_dev_script_starts_the_project_owned_word_mcp_module() -> None:
    result = subprocess.run(
        ["bash", "scripts/dev.sh", "word-server"],
        cwd=ROOT,
        env={**os.environ, "PYTHON": "/bin/echo"},
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "-m agent_workbench.apps.word_mcp.main"


def test_dev_script_exposes_one_protocol_and_health_check_command() -> None:
    result = subprocess.run(
        ["bash", "scripts/dev.sh", "word-check"],
        cwd=ROOT,
        env={**os.environ, "PYTHON": "/bin/echo"},
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    # One probe script for every project-owned server, told which one it is
    # talking to. A copy per server would be a second place for the cursor loop
    # and the health wait to be wrong.
    assert result.stdout.split() == [
        "scripts/smoke_mcp_server.py",
        "--label",
        "word",
        "--endpoint",
        "http://127.0.0.1:8765/mcp",
        "--health-url",
        "http://127.0.0.1:8765/health",
        "--expect-tool",
        "render_document",
    ]


def test_word_worker_refuses_to_fake_the_task_path_without_a_provider() -> None:
    environment = {**os.environ, "PYTHON": "/bin/echo"}
    environment.pop("AW_SECRETS__DEEPSEEK_API_KEY", None)

    result = subprocess.run(
        ["bash", "scripts/dev.sh", "word-worker"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 2
    assert "requires AW_SECRETS__DEEPSEEK_API_KEY" in result.stderr


def test_word_worker_uses_the_real_graph_and_explicit_profile_with_a_provider(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "python-probe"
    probe.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$AW_CONFIG_FILE\"\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    result = subprocess.run(
        ["bash", "scripts/dev.sh", "word-worker"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHON": str(probe),
            "AW_SECRETS__DEEPSEEK_API_KEY": "contract-only-not-a-real-key",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "config/config.word-local.toml",
        "-m agent_workbench.apps.task_worker.main",
    ]
    assert "scripts/smoke_mcp_server.py --label word --endpoint" in result.stderr
    assert "real graph" in result.stderr


def test_word_api_uses_the_same_explicit_profile(tmp_path: Path) -> None:
    probe = tmp_path / "python-probe"
    probe.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$AW_CONFIG_FILE\"\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    result = subprocess.run(
        ["bash", "scripts/dev.sh", "word-api", "--web-dir", "./web/dist"],
        cwd=ROOT,
        env={**os.environ, "PYTHON": str(probe)},
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "config/config.word-local.toml",
        "-m agent_workbench.apps.api.main --web-dir ./web/dist",
    ]
