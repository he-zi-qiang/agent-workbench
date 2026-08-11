from __future__ import annotations

import os
import tomllib
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_workbench.adapters.tools.external_search import SPEC as EXTERNAL_SEARCH_SPEC
from agent_workbench.adapters.tools.external_search import (
    TOOL_NAME as EXTERNAL_SEARCH_TOOL,
)
from agent_workbench.bootstrap import settings as settings_module
from agent_workbench.bootstrap.paths import (
    DEFAULT_CONFIG_FILE,
    PROJECT_ROOT,
    TEST_CONFIG_FILE,
)
from agent_workbench.bootstrap.projections import (
    TASK_V1_AUTHORIZATION_ENVELOPE,
    task_authorization_envelope,
)
from agent_workbench.bootstrap.settings import Settings, load_settings

CONFIG_FILE = DEFAULT_CONFIG_FILE
PYPROJECT_FILE = PROJECT_ROOT / "pyproject.toml"
POSTGRES_DSN = "postgresql+asyncpg://agent:unit-test@postgres:5432/agent_workbench"


def valid_payload() -> dict:
    with CONFIG_FILE.open("rb") as handle:
        payload = tomllib.load(handle)
    payload["database"].update(
        dsn=POSTGRES_DSN,
        guard_dsn=POSTGRES_DSN,
        listen_dsn=POSTGRES_DSN,
    )
    payload["model"]["main"]["model_id"] = "unit-main"
    payload["model"]["compact"]["model_id"] = "unit-compact"
    payload["secrets"] = {"deepseek_api_key": "unit-test-key"}
    return payload


def production_payload() -> dict:
    payload = valid_payload()
    payload["app"]["environment"] = "production"
    payload["app"]["deployment_scope"] = "remote"
    payload["model"]["main"]["model_id"] = "pinned-main-model"
    payload["model"]["compact"]["model_id"] = "pinned-compact-model"
    payload["rag"]["embedding"]["revision"] = "a" * 40
    payload["rag"]["reranker"]["revision"] = "b" * 40
    payload["qdrant"]["url"] = "https://qdrant.example.test"
    payload["qdrant"]["api_key_required"] = True
    payload["secrets"].update(
        deepseek_api_key="configured-unit-test-key",
        qdrant_api_key="configured-qdrant-test-key",
    )
    return payload


def test_default_configuration_is_valid_and_secret_safe() -> None:
    settings = Settings(**valid_payload())

    assert settings.rag.retrieval.fusion_owner == "qdrant"
    assert settings.workflow.control_plane == "langgraph"
    assert settings.database.guard_connection_scope == "task_pinned"
    assert settings.api.document_upload_transport == "artifact_data_plane"
    assert settings.event_stream.model_delta_mode == "ephemeral_sse_coalesced"
    assert POSTGRES_DSN not in str(settings.public_config())
    assert "unit-test-key" not in str(settings.public_config())
    assert settings.public_config()["model"]["main"]["max_output_tokens"] == 8192
    assert settings.public_config()["qdrant"]["api_key_required"] is False
    assert settings.public_config()["secrets"]["deepseek_api_key"] == "<configured>"
    assert Settings.model_config["secrets_nested_subdir"] is False
    assert len(settings.fingerprint()) == 64
    assert len(settings.policy_fingerprint()) == 64
    assert settings.policy_identity().startswith(f"{settings.policy.revision}:")
    semantics = settings.run_semantics_snapshot()
    assert set(semantics) == {
        "config_schema_version",
        "architecture_baseline",
        "model",
        "runtime",
        "langchain_adapter",
        "workflow",
        "multi_agent",
        "rag",
        "qdrant_index",
    }
    assert "read_alias" not in semantics["qdrant_index"]
    assert "write_collection" not in semantics["qdrant_index"]
    task_semantics = settings.task_run_semantics_snapshot(
        resolved_qdrant_collection="knowledge_bge_m3_v1",
        resolved_qdrant_index_version="bge-m3-v1",
    )
    assert (
        task_semantics["qdrant_index"]["resolved_collection_name"]
        == "knowledge_bge_m3_v1"
    )
    assert task_semantics["qdrant_index"]["resolved_index_version"] == "bge-m3-v1"
    for live_section in (
        "api",
        "database",
        "coordination",
        "event_stream",
        "artifact_store",
        "policy",
        "observability",
        "evaluation",
        "testing",
        "optional_labs",
        "secrets",
    ):
        assert live_section not in semantics
    assert {
        "precision_at_k",
        "mrr",
        "factual_correctness",
        "abstention_rate",
        "citation_precision",
        "citation_recall",
    } <= set(settings.evaluation.rag_metrics)
    assert {
        "node_retry_count",
        "human_intervention_count",
    } <= set(settings.evaluation.task_metrics)


def test_evaluation_judge_defaults_closed_and_pinned_when_enabled() -> None:
    settings = Settings(**valid_payload())
    assert settings.evaluation.judge.enabled is False
    assert settings.evaluation.online_judge_in_ci is False
    assert settings.evaluation.judge.temperature == 0.0

    missing_model = valid_payload()
    missing_model["evaluation"]["judge"]["enabled"] = True
    with pytest.raises(ValidationError, match="pinned model_id"):
        Settings(**missing_model)

    missing_revision = deepcopy(missing_model)
    missing_revision["evaluation"]["judge"]["model_id"] = "judge-model-v1"
    with pytest.raises(ValidationError, match="pinned model_revision"):
        Settings(**missing_revision)

    pinned = deepcopy(missing_revision)
    pinned["evaluation"]["judge"]["model_revision"] = "judge-revision-v1"
    enabled = Settings(**pinned)
    assert enabled.evaluation.judge.enabled is True


def test_ragas_cannot_be_enabled_while_no_runner_exists() -> None:
    """The flag may not claim an evaluation this repository cannot run.

    Three assertions, and the third is the one with teeth. Locking the value
    only proves the annotation holds. Pairing the lock with the *absence* of
    the dependency states the reason for it, and fails on the day somebody
    adds RAGAS without reopening the flag -- which is exactly the moment a
    closed flag stops being honest and starts being stale.
    """

    settings = Settings(**valid_payload())
    assert settings.evaluation.ragas_enabled is False

    enabled = valid_payload()
    enabled["evaluation"]["ragas_enabled"] = True
    with pytest.raises(ValidationError, match="ragas_enabled"):
        Settings(**enabled)

    with PYPROJECT_FILE.open("rb") as handle:
        pyproject = tomllib.load(handle)
    optional = pyproject["project"].get("optional-dependencies", {})
    groups = pyproject.get("dependency-groups", {})
    declared = [
        *pyproject["project"]["dependencies"],
        *(requirement for extra in optional.values() for requirement in extra),
        *(requirement for group in groups.values() for requirement in group),
    ]
    assert not [r for r in declared if "ragas" in str(r).lower()]


def test_evaluation_judge_temperature_is_deterministic() -> None:
    payload = valid_payload()
    payload["evaluation"]["judge"]["temperature"] = 0.1

    with pytest.raises(ValidationError, match="temperature"):
        Settings(**payload)


def test_guard_connection_rejects_transaction_pooling() -> None:
    payload = valid_payload()
    payload["database"]["guard_pool_mode"] = "transaction"

    with pytest.raises(ValidationError, match="guard_pool_mode"):
        Settings(**payload)


def test_lease_must_cover_missed_heartbeats_and_grace() -> None:
    payload = valid_payload()
    payload["coordination"]["lease_duration_seconds"] = 70

    with pytest.raises(ValidationError, match="lease_duration_seconds"):
        Settings(**payload)


def test_worker_concurrency_cannot_exceed_guard_budget() -> None:
    payload = valid_payload()
    payload["coordination"]["worker_concurrency"] = 5
    payload["coordination"]["claim_batch_size"] = 1
    payload["database"]["guard_connection_budget"] = 4

    with pytest.raises(ValidationError, match="guard_connection_budget"):
        Settings(**payload)


def test_llamaindex_cannot_take_over_hybrid_fusion() -> None:
    payload = valid_payload()
    payload["rag"]["llama_index"]["fusion_enabled"] = True

    with pytest.raises(ValidationError, match="fusion_enabled"):
        Settings(**payload)


def test_rerank_funnel_is_monotonic() -> None:
    payload = valid_payload()
    payload["rag"]["retrieval"]["rerank_top_k"] = 50

    with pytest.raises(ValidationError, match="rerank_top_k"):
        Settings(**payload)


def test_fault_injection_requires_test_environment_and_double_gate() -> None:
    payload = production_payload()
    payload["testing"].update(
        failpoints_enabled=True,
        allow_fault_injection=True,
        allowed_failpoints=["inside_checkpoint_put"],
    )

    with pytest.raises(ValidationError, match="fault injection"):
        Settings(**payload)


def test_production_rejects_unpinned_rag_revision() -> None:
    payload = production_payload()
    payload["rag"]["embedding"]["revision"] = "release-v1"

    with pytest.raises(ValidationError, match="40-character hexadecimal"):
        Settings(**payload)


def test_production_normalizes_full_rag_commit_sha() -> None:
    payload = production_payload()
    payload["rag"]["embedding"]["revision"] = "A" * 40

    settings = Settings(**payload)

    assert settings.rag.embedding.revision == "a" * 40


def test_production_rejects_optional_labs() -> None:
    payload = production_payload()
    payload["optional_labs"]["dynamic_agent_spawn"] = True

    with pytest.raises(ValidationError, match="Optional Labs"):
        Settings(**payload)


def test_production_requires_authenticated_https_qdrant() -> None:
    payload = production_payload()
    payload["qdrant"]["api_key_required"] = False

    with pytest.raises(ValidationError, match="api_key_required=true"):
        Settings(**payload)


def test_production_qdrant_rejects_plain_http() -> None:
    payload = production_payload()
    payload["qdrant"]["url"] = "http://qdrant.example.test"

    with pytest.raises(ValidationError, match="Qdrant HTTPS URL"):
        Settings(**payload)


def test_remote_development_also_requires_authenticated_https_qdrant() -> None:
    payload = valid_payload()
    payload["app"]["deployment_scope"] = "remote"

    with pytest.raises(ValidationError, match="api_key_required=true"):
        Settings(**payload)


def test_remote_scope_forbids_qdrant_bootstrap_even_with_https_and_a_key() -> None:
    payload = production_payload()
    payload["qdrant"]["allow_local_bootstrap"] = True

    with pytest.raises(ValidationError, match=r"forbids qdrant.allow_local_bootstrap"):
        Settings(**payload)


def test_local_scope_cannot_point_at_a_remote_qdrant_host() -> None:
    payload = valid_payload()
    payload["qdrant"]["url"] = "http://qdrant.example.test"

    with pytest.raises(ValidationError, match="local/Compose Qdrant host"):
        Settings(**payload)


def test_production_cannot_claim_a_local_deployment_scope() -> None:
    payload = production_payload()
    payload["app"]["deployment_scope"] = "local"

    with pytest.raises(ValidationError, match="deployment_scope=remote"):
        Settings(**payload)


@pytest.mark.parametrize(
    ("section", "field", "unsafe_url"),
    [
        ("qdrant", "url", "https://user:password@qdrant.example.test"),
        (
            "artifact_store",
            "endpoint",
            "https://artifact.example.test?token=secret",
        ),
        (
            "observability",
            "otel_exporter_endpoint",
            "https://otel.example.test#secret",
        ),
    ],
)
def test_public_service_endpoints_cannot_embed_credentials(
    section: str,
    field: str,
    unsafe_url: str,
) -> None:
    payload = valid_payload()
    payload[section][field] = unsafe_url

    with pytest.raises(ValidationError, match="must not") as exc_info:
        Settings(**payload)
    assert unsafe_url not in str(exc_info.value)
    assert "unit-test-key" not in str(exc_info.value)


def test_public_fingerprint_changes_with_non_secret_semantics_only() -> None:
    first_payload = valid_payload()
    second_payload = deepcopy(first_payload)
    second_payload["secrets"]["deepseek_api_key"] = "another-secret"

    first = Settings(**first_payload)
    second = Settings(**second_payload)
    assert first.fingerprint() == second.fingerprint()

    third_payload = deepcopy(first_payload)
    third_payload["runtime"]["max_steps"] = 13
    third = Settings(**third_payload)
    assert first.fingerprint() != third.fingerprint()

    fourth_payload = deepcopy(first_payload)
    fourth_payload["multi_agent"]["max_tokens_per_agent_invocation"] = 20_000
    fourth = Settings(**fourth_payload)
    assert first.fingerprint() != fourth.fingerprint()

    policy_payload = deepcopy(first_payload)
    policy_payload["policy"]["revision"] = "policy-v2"
    policy_settings = Settings(**policy_payload)
    assert first.fingerprint() != policy_settings.fingerprint()
    assert first.policy_fingerprint() == policy_settings.policy_fingerprint()
    assert first.policy_identity() != policy_settings.policy_identity()
    assert (
        first.run_semantics_fingerprint() == policy_settings.run_semantics_fingerprint()
    )

    stale_label_payload = deepcopy(first_payload)
    stale_label_payload["policy"]["max_tool_argument_bytes"] = 32_768
    stale_label_settings = Settings(**stale_label_payload)
    assert stale_label_settings.policy.revision == first.policy.revision
    assert stale_label_settings.policy_fingerprint() != first.policy_fingerprint()
    assert stale_label_settings.policy_identity() != first.policy_identity()

    lab_payload = deepcopy(first_payload)
    lab_payload["optional_labs"]["mcp_adapter"] = True
    lab_settings = Settings(**lab_payload)
    assert first.fingerprint() != lab_settings.fingerprint()
    assert first.run_semantics_fingerprint() == lab_settings.run_semantics_fingerprint()
    first_task_revision = first.task_run_semantics_revision(
        resolved_qdrant_collection="knowledge_bge_m3_v1",
        resolved_qdrant_index_version="bge-m3-v1",
    )
    second_task_revision = first.task_run_semantics_revision(
        resolved_qdrant_collection="knowledge_bge_m3_v2",
        resolved_qdrant_index_version="bge-m3-v2",
    )
    assert first_task_revision != second_task_revision

    with pytest.raises(ValueError, match="provided together"):
        first.task_run_semantics_snapshot(
            resolved_qdrant_collection="knowledge_bge_m3_v1",
        )
    with pytest.raises(ValueError, match="not the configured read alias"):
        first.task_run_semantics_snapshot(
            resolved_qdrant_collection=first.qdrant.read_alias,
            resolved_qdrant_index_version="bge-m3-v1",
        )


def _clear_agent_workbench_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in tuple(os.environ):
        if key.upper().startswith("AW_"):
            monkeypatch.delenv(key, raising=False)


def _set_required_database_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AW_DATABASE__DSN", POSTGRES_DSN)
    monkeypatch.setenv("AW_DATABASE__GUARD_DSN", POSTGRES_DSN)
    monkeypatch.setenv("AW_DATABASE__LISTEN_DSN", POSTGRES_DSN)


def test_load_settings_resolves_overlay_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    _set_required_database_environment(monkeypatch)
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        "[runtime]\nmax_steps = 14\nmax_tool_calls = 40\n",
        encoding="utf-8",
    )
    missing_dotenv = tmp_path / "missing.env"

    settings = load_settings(
        config_file=overlay,
        env_file=missing_dotenv,
    )
    assert settings.runtime.max_steps == 14

    monkeypatch.setenv("AW_RUNTIME__MAX_STEPS", "15")
    settings = load_settings(
        config_file=overlay,
        env_file=missing_dotenv,
    )
    assert settings.runtime.max_steps == 15


def test_test_overlay_uses_only_canonical_failpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    _set_required_database_environment(monkeypatch)

    settings = load_settings(
        config_file=TEST_CONFIG_FILE,
        env_file=tmp_path / "missing.env",
    )
    assert settings.testing.failpoints_enabled is True
    assert settings.qdrant.allow_local_bootstrap is True
    assert (
        set(settings.testing.allowed_failpoints) == settings_module.CANONICAL_FAILPOINTS
    )


def test_unknown_prefixed_environment_variable_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    monkeypatch.setenv("AW_COORDINATIOON__WORKER_CONCURRENCY", "99")

    with pytest.raises(ValueError, match="unknown Agent Workbench"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AW_DATABASE", '{"dsn":"must-not-bypass"}'),
        ("AW_SECRETS", '{"deepseek_api_key":"must-not-bypass"}'),
    ],
)
def test_parent_json_environment_variable_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="unknown Agent Workbench"):
        load_settings()


def test_parent_json_dotenv_variable_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    _set_required_database_environment(monkeypatch)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        'AW_SECRETS={"deepseek_api_key":"must-not-bypass"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"non-leaf.*dotenv"):
        load_settings(env_file=dotenv)


def test_case_normalized_duplicate_environment_variable_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    monkeypatch.setenv("AW_DATABASE__DSN", POSTGRES_DSN)
    monkeypatch.setenv("aw_database__dsn", "different")

    with pytest.raises(ValueError, match="after case normalization"):
        load_settings()


def test_toml_overlay_cannot_contain_a_dsn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    overlay = tmp_path / "unsafe.toml"
    overlay.write_text(
        f'[database]\ndsn = "{POSTGRES_DSN}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"must not contain database\.dsn"):
        load_settings(config_file=overlay)


def test_dotenv_cannot_turn_a_development_profile_into_production(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "AW_APP__ENVIRONMENT=production",
                "AW_APP__DEPLOYMENT_SCOPE=remote",
                f"AW_DATABASE__DSN={POSTGRES_DSN}",
                f"AW_DATABASE__GUARD_DSN={POSTGRES_DSN}",
                f"AW_DATABASE__LISTEN_DSN={POSTGRES_DSN}",
                "AW_MODEL__MAIN__MODEL_ID=pinned-main-model",
                "AW_MODEL__COMPACT__MODEL_ID=pinned-compact-model",
                f"AW_RAG__EMBEDDING__REVISION={'a' * 40}",
                f"AW_RAG__RERANKER__REVISION={'b' * 40}",
                "AW_QDRANT__URL=https://qdrant.example.test",
                "AW_QDRANT__API_KEY_REQUIRED=true",
                "AW_SECRETS__DEEPSEEK_API_KEY=configured-test-key",
                "AW_SECRETS__QDRANT_API_KEY=configured-qdrant-test-key",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not load a dotenv"):
        load_settings(env_file=dotenv)


def test_flat_mounted_secret_files_are_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    for filename in (
        "AW_DATABASE__DSN",
        "AW_DATABASE__GUARD_DSN",
        "AW_DATABASE__LISTEN_DSN",
    ):
        (secrets_dir / filename).write_text(POSTGRES_DSN, encoding="utf-8")
    (secrets_dir / "AW_SECRETS__DEEPSEEK_API_KEY").write_text(
        "mounted-test-key",
        encoding="utf-8",
    )

    settings = load_settings(
        env_file=tmp_path / "missing.env",
        secrets_dir=secrets_dir,
    )
    assert settings.database.guard_dsn.get_secret_value() == POSTGRES_DSN
    assert (
        settings.secrets.deepseek_api_key is not None
        and settings.secrets.deepseek_api_key.get_secret_value() == "mounted-test-key"
    )


def test_identical_env_and_mounted_secret_is_explicitly_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    _set_required_database_environment(monkeypatch)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    for filename in (
        "AW_DATABASE__DSN",
        "AW_DATABASE__GUARD_DSN",
        "AW_DATABASE__LISTEN_DSN",
    ):
        (secrets_dir / filename).write_text(POSTGRES_DSN, encoding="utf-8")

    with pytest.warns(
        RuntimeWarning,
        match="duplicate secret source",
    ) as warning_records:
        loaded = load_settings(
            env_file=tmp_path / "missing.env",
            secrets_dir=secrets_dir,
        )
    assert loaded.database.dsn.get_secret_value() == POSTGRES_DSN
    assert POSTGRES_DSN not in " ".join(
        str(record.message) for record in warning_records
    )


def test_different_env_and_mounted_secret_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    _set_required_database_environment(monkeypatch)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    mounted_value = "postgresql+asyncpg://agent:different@postgres:5432/agent_workbench"
    (secrets_dir / "AW_DATABASE__DSN").write_text(
        mounted_value,
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="secret source conflict",
    ) as exc_info:
        load_settings(
            env_file=tmp_path / "missing.env",
            secrets_dir=secrets_dir,
        )
    assert POSTGRES_DSN not in str(exc_info.value)
    assert mounted_value not in str(exc_info.value)


def test_whitespace_different_env_and_mounted_secret_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    _set_required_database_environment(monkeypatch)
    monkeypatch.setenv("AW_SECRETS__DEEPSEEK_API_KEY", "same-value ")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "AW_SECRETS__DEEPSEEK_API_KEY").write_text(
        "same-value",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="secret source conflict"):
        load_settings(
            env_file=tmp_path / "missing.env",
            secrets_dir=secrets_dir,
        )


def test_out_of_tree_secret_symlink_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    outside = tmp_path / "outside-dsn"
    outside.write_text(POSTGRES_DSN, encoding="utf-8")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "AW_DATABASE__DSN").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes secrets_dir"):
        load_settings(
            env_file=tmp_path / "missing.env",
            secrets_dir=secrets_dir,
        )


@pytest.mark.parametrize("filename", ["AW_DATABASE", "AW_SECRETS"])
def test_parent_json_mounted_secret_file_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filename: str,
) -> None:
    _clear_agent_workbench_environment(monkeypatch)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / filename).write_text(
        '{"dsn":"must-not-bypass"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-leaf"):
        load_settings(
            env_file=tmp_path / "missing.env",
            secrets_dir=secrets_dir,
        )


def test_unknown_failpoint_name_is_rejected() -> None:
    payload = valid_payload()
    payload["app"]["environment"] = "test"
    payload["model"]["provider"] = "fake"
    # Regression guard: this pre-E4 spelling must stay invalid.
    payload["testing"].update(
        failpoints_enabled=True,
        allow_fault_injection=True,
        allowed_failpoints=["after_fence_row_lock"],
    )

    with pytest.raises(ValidationError, match="unknown failpoint"):
        Settings(**payload)


def test_dependency_floor_excludes_cve_2026_58203_versions() -> None:
    pyproject = PYPROJECT_FILE.read_text(encoding="utf-8")
    assert '"pydantic-settings>=2.14.2,<3"' in pyproject
    settings_module._assert_safe_pydantic_settings_version()


def test_runtime_dependency_guard_rejects_vulnerable_installed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings_module,
        "distribution_version",
        lambda _: "2.14.1",
    )

    with pytest.raises(RuntimeError, match="CVE-2026-58203"):
        settings_module._assert_safe_pydantic_settings_version()


def test_runtime_dependency_guard_rejects_prerelease_lookalike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings_module,
        "distribution_version",
        lambda _: "2.14.2rc1",
    )

    with pytest.raises(RuntimeError, match="cannot parse"):
        settings_module._assert_safe_pydantic_settings_version()


def test_only_a_provider_with_a_shipped_adapter_is_configurable() -> None:
    """A provider string the process cannot start on fails at boot, not later."""

    payload = valid_payload()
    payload["model"]["provider"] = "anthropic"

    with pytest.raises(ValidationError):
        Settings(**payload)


def test_the_model_endpoint_must_be_encrypted_unless_it_is_loopback() -> None:
    """Every request to it carries the provider API key."""

    payload = valid_payload()
    payload["model"]["base_url"] = "http://api.deepseek.com"

    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(**payload)


def test_a_local_compatible_server_may_be_plain_http() -> None:
    payload = valid_payload()
    payload["model"]["base_url"] = "http://localhost:11434"

    assert Settings(**payload).model.base_url == "http://localhost:11434"


def test_the_model_endpoint_refuses_embedded_credentials() -> None:
    payload = valid_payload()
    payload["model"]["base_url"] = "https://key:secret@api.deepseek.com"

    with pytest.raises(ValidationError):
        Settings(**payload)


def test_the_model_endpoint_is_not_part_of_run_semantics() -> None:
    """Resuming a task must not restore where the model used to be reachable."""

    payload = valid_payload()
    moved = deepcopy(payload)
    moved["model"]["base_url"] = "https://deepseek.internal.example"

    original = Settings(**payload)
    relocated = Settings(**moved)

    assert "base_url" not in original.run_semantics_snapshot()["model"]
    assert original.run_semantics_snapshot() == relocated.run_semantics_snapshot()
    assert original.run_semantics_revision() == relocated.run_semantics_revision()
    # The startup revision still notices: it is the full configuration.
    assert original.revision() != relocated.revision()


def test_production_requires_a_real_provider_key() -> None:
    payload = production_payload()
    payload["secrets"]["deepseek_api_key"] = "replace-me"

    with pytest.raises(ValidationError, match="DeepSeek provider requires"):
        Settings(**payload)


def test_the_configuration_schema_version_is_pinned() -> None:
    """Adding a provider changed the contract, so the schema version moved.

    1.2 -> 1.3 is ADR-018: ``chat.retrieval_shape`` gained a third value.
    1.3 -> 1.4 is ADR-019: ``runtime.record_step_inputs`` lets a deployment put
    the prompt and the proposed tool arguments on its own event stream, which
    changes what it stores about its users.
    1.6 -> 1.7 is ADR-025: the ``[mcp]`` section arrived, and the tool names it
    resolves are written into the Task authorization envelope -- so a config
    file at this version can widen what a Task is allowed to call.
    1.7 -> 1.8 completes ADR-025's cross-process contract: each MCP server now
    carries an explicit remote-tool allowlist, so API submission and Worker
    discovery agree without granting whatever a server adds later.
    1.8 -> 1.9 is ADR-029: the ``[sandbox]`` section arrived. Enabling it puts
    ``sandbox_run`` in the Task envelope *and* raises that envelope's risk
    ceiling to "external", so a config file at this version can widen both
    what a Task may call and how far.
    1.9 -> 1.10 is ADR-027: `[[mcp.servers]].audience` decides which agent a
    server's tools reach, so a config file at this version changes which agent
    in a running graph can call what.
    1.10 -> 1.11 is ADR-030: `runtime.max_steps` widened from 100 to 1000 as
    the step ceiling stopped being the budget, and `[model.*.pricing]`
    arrived. The widening is the half that makes this a compatibility break in
    the direction the pin exists to catch -- a 1.11 file may set
    `max_steps = 500`, and a 1.10 binary rejects it at validation rather than
    running it with a different ceiling. The prices are additive, but they
    decide whether this deployment may enforce a cost ceiling at all, so a
    file that has them buys behaviour a file that does not cannot ask for.
    1.12 -> 1.13 is ADR-037: the `[rag.graph]` section arrived. It sits under
    `rag`, so it enters the Task semantics snapshot -- enabling it changes what
    a newly submitted Task's retrieval means, and two Tasks either side of the
    flip are not comparable. It also carries two frozen `Literal`s
    (`nominates_chunks_only`, `query_side_extraction_enabled`) whose whole
    purpose is that widening them cannot happen quietly.
    1.11 -> 1.12 is ADR-036: the `[triage]` section arrived. Additive and
    default-off, but a file that enables it asks the API to spend a model
    call per create-form submission and changes what the form asks a human
    -- behaviour a 1.11 binary cannot provide, so a file that sets it must
    not load quietly on one.

    The pin exists so widening a frozen Literal cannot happen quietly -- this
    test failing *is* the mechanism, and updating it is the last step of the
    decision rather than a chore around it.
    """

    assert Settings(**valid_payload()).app.config_schema_version == "1.13"


def test_external_search_stays_outside_the_task_envelope_by_default() -> None:
    """The default is load-bearing, not merely cautious.

    The envelope is stored with each Task and re-applied on every resume, so a
    deployment that never configured a provider must not have its historical
    Tasks widened by an upgrade (ADR-020).
    """

    envelope = task_authorization_envelope(external_search=False)

    assert envelope == TASK_V1_AUTHORIZATION_ENVELOPE
    assert EXTERNAL_SEARCH_TOOL not in envelope.allowed_tools
    assert not envelope.permits(EXTERNAL_SEARCH_SPEC)


def test_enabling_search_grants_both_halves_of_the_permission() -> None:
    """Allowlist and risk ceiling together, or the tool is still refused.

    `risk_within` ranks external above write, so raising only one of the two
    produces an envelope that denies the tool while looking like it allows it.
    """

    envelope = task_authorization_envelope(external_search=True)

    assert EXTERNAL_SEARCH_TOOL in envelope.allowed_tools
    assert envelope.max_tool_risk == "external"
    assert envelope.permits(EXTERNAL_SEARCH_SPEC)
    # The human gate stays at the graph's approval node, not the tool boundary.
    assert envelope.approval_required_risks == ()


def test_research_enabled_without_a_provider_key_is_refused_at_startup() -> None:
    """Enabled-but-unconfigured has to fail at startup, not at the first search.

    Search runs on the model provider's side under its key, so there is no
    second credential to check -- but "enabled with no key at all" still reads
    as working web search in the config file, and that is the defect.
    """

    payload = valid_payload()
    payload["research"] = {"enabled": True}
    payload["secrets"] = {}

    with pytest.raises(ValidationError, match="provider API key"):
        Settings(**payload)


def test_research_needs_no_credential_beyond_the_provider_s_own() -> None:
    payload = valid_payload()
    payload["research"] = {"enabled": True}

    settings = Settings(**payload)

    assert settings.research.enabled
    assert settings.research.base_url == "https://api.deepseek.com/anthropic"
