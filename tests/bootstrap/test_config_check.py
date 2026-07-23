from __future__ import annotations

import json
from pathlib import Path

from agent_workbench.bootstrap.config_check import main, run
from agent_workbench.bootstrap.paths import TEST_CONFIG_FILE


def _required_environment(monkeypatch) -> None:
    dsn = "postgresql+asyncpg://agent:test@postgres:5432/agent_workbench"
    monkeypatch.setenv("AW_DATABASE__DSN", dsn)
    monkeypatch.setenv("AW_DATABASE__GUARD_DSN", dsn)
    monkeypatch.setenv("AW_DATABASE__LISTEN_DSN", dsn)


def test_config_check_returns_safe_revisions(monkeypatch, tmp_path: Path) -> None:
    _required_environment(monkeypatch)

    payload = run(
        [
            "--config",
            str(TEST_CONFIG_FILE),
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )

    assert payload["status"] == "ok"
    assert payload["environment"] == "test"
    assert payload["startup_config_revision"]
    assert payload["run_semantics_template_revision"]
    assert payload["policy_identity"]
    assert "settings" not in payload
    assert "postgresql" not in json.dumps(payload)


def test_config_check_cli_prints_only_redacted_values(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    _required_environment(monkeypatch)
    canary = "agent-workbench-config-check-secret-canary"
    monkeypatch.setenv("AW_SECRETS__ANTHROPIC_API_KEY", canary)

    exit_code = main(
        [
            "--config",
            str(TEST_CONFIG_FILE),
            "--env-file",
            str(tmp_path / "missing.env"),
            "--show-public-config",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert canary not in output
    assert "<configured>" in output
