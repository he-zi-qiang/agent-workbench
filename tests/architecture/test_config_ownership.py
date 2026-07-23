from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import TypedDict, cast, get_args

from pydantic import BaseModel

from agent_workbench.bootstrap.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNERSHIP_FILE = PROJECT_ROOT / "config" / "ownership.yaml"

EXPECTED_LIFECYCLES = frozenset(
    {"startup", "live", "task_snapshot", "test_only", "lab"}
)
EXPECTED_TASK_SNAPSHOT_ALLOWLIST = (
    "app.config_schema_version",
    "app.architecture_baseline",
    "model.*",
    "runtime.*",
    "langchain_adapter.*",
    "workflow.*",
    "multi_agent.*",
    "rag.*",
    "qdrant.collection_schema_version",
    "qdrant.distance",
)
EXPECTED_DERIVED_TASK_SNAPSHOT_VALUES = (
    "resolved_qdrant_collection",
    "resolved_qdrant_index_version",
    "resolved_qdrant_index_generation_id",
)
OWNER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class OwnershipGroup(TypedDict):
    owner: str
    lifecycle: str
    fields: list[str]


class OwnershipManifest(TypedDict):
    schema_version: int
    allowed_lifecycles: list[str]
    task_snapshot_allowlist: list[str]
    derived_task_snapshot_values: list[str]
    groups: list[OwnershipGroup]


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError(f"ownership manifest has duplicate key: {key}")
        result[key] = value
    return result


def _load_manifest() -> OwnershipManifest:
    # JSON is a YAML 1.2 subset, so the manifest remains valid YAML while CI
    # needs only the standard library and cannot acquire another parser.
    raw = json.loads(
        OWNERSHIP_FILE.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_json_keys,
    )
    return cast(OwnershipManifest, raw)


def _nested_model_type(annotation: object) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for argument in get_args(annotation):
        nested = _nested_model_type(argument)
        if nested is not None:
            return nested
    return None


def _settings_leaf_fields(
    model_type: type[BaseModel] = Settings,
    prefix: str = "",
) -> set[str]:
    leaves: set[str] = set()
    for field_name, field_info in model_type.model_fields.items():
        path = f"{prefix}.{field_name}" if prefix else field_name
        nested_model = _nested_model_type(field_info.annotation)
        if nested_model is None:
            leaves.add(path)
        else:
            leaves.update(_settings_leaf_fields(nested_model, path))
    return leaves


def _registrations(
    manifest: OwnershipManifest,
) -> list[tuple[str, str, str]]:
    return [
        (field, group["owner"], group["lifecycle"])
        for group in manifest["groups"]
        for field in group["fields"]
    ]


def _expand_positive_allowlist(
    patterns: tuple[str, ...],
    settings_fields: set[str],
) -> set[str]:
    expanded: set[str] = set()
    for pattern in patterns:
        if pattern.endswith(".*"):
            prefix = pattern.removesuffix("*")
            matches = {field for field in settings_fields if field.startswith(prefix)}
            assert matches, f"snapshot allowlist pattern matches no field: {pattern}"
            expanded.update(matches)
        else:
            assert pattern in settings_fields, (
                f"snapshot allowlist names an unknown field: {pattern}"
            )
            expanded.add(pattern)
    return expanded


def test_manifest_has_a_small_explicit_schema() -> None:
    manifest = _load_manifest()

    assert set(manifest) == {
        "schema_version",
        "allowed_lifecycles",
        "task_snapshot_allowlist",
        "derived_task_snapshot_values",
        "groups",
    }
    assert manifest["schema_version"] == 1
    assert frozenset(manifest["allowed_lifecycles"]) == EXPECTED_LIFECYCLES
    assert len(manifest["allowed_lifecycles"]) == len(EXPECTED_LIFECYCLES)
    assert manifest["groups"]

    for group in manifest["groups"]:
        assert set(group) == {"owner", "lifecycle", "fields"}
        assert OWNER_PATTERN.fullmatch(group["owner"]), group["owner"]
        assert group["lifecycle"] in EXPECTED_LIFECYCLES
        assert group["fields"], group["owner"]
        assert all(isinstance(field, str) and field for field in group["fields"])


def test_every_pydantic_settings_leaf_has_exactly_one_owner() -> None:
    manifest = _load_manifest()
    registrations = _registrations(manifest)
    registration_counts = Counter(field for field, _, _ in registrations)
    duplicated = sorted(
        field for field, count in registration_counts.items() if count != 1
    )
    settings_fields = _settings_leaf_fields()

    assert not duplicated, f"fields must be registered exactly once: {duplicated}"
    assert set(registration_counts) == settings_fields, (
        "ownership drift detected; "
        f"missing={sorted(settings_fields - set(registration_counts))}, "
        f"unknown={sorted(set(registration_counts) - settings_fields)}"
    )


def test_task_snapshot_is_the_planned_positive_allowlist() -> None:
    manifest = _load_manifest()
    settings_fields = _settings_leaf_fields()

    configured_allowlist = tuple(manifest["task_snapshot_allowlist"])
    assert configured_allowlist == EXPECTED_TASK_SNAPSHOT_ALLOWLIST

    expected_snapshot_fields = _expand_positive_allowlist(
        EXPECTED_TASK_SNAPSHOT_ALLOWLIST,
        settings_fields,
    )
    actual_snapshot_fields = {
        field
        for field, _, lifecycle in _registrations(manifest)
        if lifecycle == "task_snapshot"
    }
    assert actual_snapshot_fields == expected_snapshot_fields
    assert tuple(manifest["derived_task_snapshot_values"]) == (
        EXPECTED_DERIVED_TASK_SNAPSHOT_VALUES
    )


def test_test_and_lab_lifecycles_are_closed_namespaces() -> None:
    manifest = _load_manifest()
    registrations = _registrations(manifest)
    settings_fields = _settings_leaf_fields()
    lifecycle_by_field = {field: lifecycle for field, _, lifecycle in registrations}

    testing_fields = {
        field for field in settings_fields if field.startswith("testing.")
    }
    optional_lab_fields = {
        field for field in settings_fields if field.startswith("optional_labs.")
    }

    assert {
        field
        for field, lifecycle in lifecycle_by_field.items()
        if lifecycle == "test_only"
    } == testing_fields
    assert {
        field for field, lifecycle in lifecycle_by_field.items() if lifecycle == "lab"
    } == optional_lab_fields


def test_high_risk_fields_keep_their_narrow_owners() -> None:
    manifest = _load_manifest()
    owner_by_field = {field: owner for field, owner, _ in _registrations(manifest)}

    assert (
        owner_by_field["database.guard_disconnect_action"]
        == "adapters.persistence.guard_runner"
    )
    assert (
        owner_by_field["database.listener_healthcheck_seconds"]
        == "adapters.persistence.event_listener"
    )
    assert (
        owner_by_field["coordination.tool_execution_ledger_enabled"]
        == "adapters.persistence.tool_execution_ledger"
    )
    assert (
        owner_by_field["testing.allowed_failpoints"]
        == "adapters.testing.fault_injector"
    )
