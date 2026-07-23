from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from agent_workbench.bootstrap.config_check import run

POSTGRES_DSN = "postgresql+asyncpg://agent:profile-test@postgres:5432/agent_workbench"


def _clear_agent_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)


def _database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_DATABASE__DSN", POSTGRES_DSN)
    monkeypatch.setenv("AW_DATABASE__GUARD_DSN", POSTGRES_DSN)
    monkeypatch.setenv("AW_DATABASE__LISTEN_DSN", POSTGRES_DSN)


def _production_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _database_environment(monkeypatch)
    monkeypatch.setenv("AW_MODEL__MAIN__MODEL_ID", "ci-contract-main-model")
    monkeypatch.setenv(
        "AW_MODEL__COMPACT__MODEL_ID",
        "ci-contract-compact-model",
    )
    monkeypatch.setenv("AW_RAG__EMBEDDING__REVISION", "a" * 40)
    monkeypatch.setenv("AW_RAG__RERANKER__REVISION", "b" * 40)
    monkeypatch.setenv(
        "AW_SECRETS__ANTHROPIC_API_KEY",
        "ci-contract-anthropic-key",
    )
    monkeypatch.setenv(
        "AW_SECRETS__QDRANT_API_KEY",
        "ci-contract-qdrant-key",
    )


@pytest.mark.parametrize(
    ("profile", "expected_environment"),
    [
        ("development", "development"),
        ("test", "test"),
    ],
)
def test_named_non_production_profiles_validate_offline(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    expected_environment: str,
) -> None:
    _clear_agent_environment(monkeypatch)
    _database_environment(monkeypatch)

    payload = run(["--profile", profile])

    assert payload["status"] == "ok"
    assert payload["environment"] == expected_environment
    assert "adapter" not in json.dumps(payload).lower()
    assert POSTGRES_DSN not in json.dumps(payload)


def test_named_development_profile_ignores_ambient_config_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_agent_environment(monkeypatch)
    _database_environment(monkeypatch)
    monkeypatch.setenv("AW_CONFIG_FILE", "config/config.test.toml")

    payload = run(["--profile", "development"])

    assert payload["status"] == "ok"
    assert payload["environment"] == "development"


def test_named_production_profile_validates_contract_without_starting_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_agent_environment(monkeypatch)
    _production_environment(monkeypatch)

    payload = run(["--profile", "production"])

    assert payload["status"] == "ok"
    assert payload["environment"] == "production"
    assert payload["deployment_scope"] == "remote"
    assert "adapter" not in json.dumps(payload).lower()
    assert "ci-contract-anthropic-key" not in json.dumps(payload)
    assert "ci-contract-qdrant-key" not in json.dumps(payload)


def test_production_profile_fails_closed_without_deployment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_agent_environment(monkeypatch)
    _database_environment(monkeypatch)

    with pytest.raises(ValidationError, match="Qdrant API key"):
        run(["--profile", "production"])


def test_profile_and_config_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as error:
        run(
            [
                "--profile",
                "test",
                "--config",
                "config/config.test.toml",
            ]
        )

    assert error.value.code == 2


@pytest.mark.parametrize(
    "environment_key",
    [
        "AW_SECRETS__ADMIN_TOKEN",
        "AW_SECRETS__WEBHOOK_TOKEN",
    ],
)
def test_removed_unowned_secret_fields_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    environment_key: str,
) -> None:
    _clear_agent_environment(monkeypatch)
    _database_environment(monkeypatch)
    monkeypatch.setenv(environment_key, "must-not-be-accepted")

    with pytest.raises(ValueError, match=environment_key.split("__")[-1]):
        run(["--profile", "development"])
