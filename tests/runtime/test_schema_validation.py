"""The supported JSON Schema subset, and the schemas it refuses to pretend about."""

from __future__ import annotations

import pytest

from agent_workbench.domain.errors import ToolInputInvalidError
from agent_workbench.domain.schema import JsonObject
from agent_workbench.runtime.schema_validation import (
    UnsupportedToolSchema,
    assert_schema_supported,
    validate_arguments,
)

SEARCH_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
        "mode": {"type": "string", "enum": ["dense", "hybrid"]},
        "tags": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[a-z]+$"},
            "maxItems": 3,
        },
        "filters": {
            "type": "object",
            "properties": {"tenant": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def test_a_supported_schema_is_accepted_at_assembly() -> None:
    assert_schema_supported(SEARCH_SCHEMA, origin="tool knowledge_search")


@pytest.mark.parametrize(
    "keyword",
    ["oneOf", "anyOf", "allOf", "not", "$ref", "patternProperties", "if"],
)
def test_a_schema_reaching_beyond_the_subset_is_refused(keyword: str) -> None:
    """Ignoring a keyword would report every call valid while enforcing nothing."""

    with pytest.raises(UnsupportedToolSchema, match=keyword.replace("$", r"\$")):
        assert_schema_supported({"type": "object", keyword: []}, origin="tool t")


def test_an_unsupported_keyword_nested_in_a_property_is_refused() -> None:
    schema: JsonObject = {
        "type": "object",
        "properties": {"q": {"type": "string", "format": "email"}},
    }

    with pytest.raises(UnsupportedToolSchema, match="at q"):
        assert_schema_supported(schema, origin="tool t")


def test_an_unsupported_keyword_nested_in_items_is_refused() -> None:
    schema: JsonObject = {
        "type": "object",
        "properties": {"xs": {"type": "array", "items": {"$ref": "#/x"}}},
    }

    with pytest.raises(UnsupportedToolSchema, match=r"xs\[\]"):
        assert_schema_supported(schema, origin="tool t")


def test_an_unknown_type_is_refused() -> None:
    with pytest.raises(UnsupportedToolSchema, match="unknown type"):
        assert_schema_supported({"type": "date"}, origin="tool t")


def test_valid_arguments_pass() -> None:
    validate_arguments(
        SEARCH_SCHEMA,
        {
            "query": "hybrid fusion",
            "top_k": 8,
            "mode": "hybrid",
            "tags": ["rag"],
            "filters": {"tenant": "tenant_a"},
        },
    )


def test_a_missing_required_property_names_its_path() -> None:
    with pytest.raises(ToolInputInvalidError, match=r"arguments\.query: is required"):
        validate_arguments(SEARCH_SCHEMA, {"top_k": 3})


def test_an_unexpected_property_is_rejected() -> None:
    with pytest.raises(ToolInputInvalidError, match="unexpected properties: sql"):
        validate_arguments(SEARCH_SCHEMA, {"query": "x", "sql": "drop table"})


def test_a_wrong_type_names_the_path_and_the_expectation() -> None:
    with pytest.raises(
        ToolInputInvalidError, match=r"arguments\.top_k: expected integer"
    ):
        validate_arguments(SEARCH_SCHEMA, {"query": "x", "top_k": "eight"})


def test_a_boolean_is_not_an_integer() -> None:
    """``isinstance(True, int)`` is true in Python; a flag is not a count."""

    with pytest.raises(ToolInputInvalidError, match="expected integer"):
        validate_arguments(SEARCH_SCHEMA, {"query": "x", "top_k": True})


def test_bounds_are_enforced() -> None:
    with pytest.raises(ToolInputInvalidError, match="below the minimum"):
        validate_arguments(SEARCH_SCHEMA, {"query": "x", "top_k": 0})
    with pytest.raises(ToolInputInvalidError, match="above the maximum"):
        validate_arguments(SEARCH_SCHEMA, {"query": "x", "top_k": 99})
    with pytest.raises(ToolInputInvalidError, match="shorter than 1"):
        validate_arguments(SEARCH_SCHEMA, {"query": ""})
    with pytest.raises(ToolInputInvalidError, match="longer than 200"):
        validate_arguments(SEARCH_SCHEMA, {"query": "x" * 201})


def test_enums_are_enforced() -> None:
    with pytest.raises(ToolInputInvalidError, match="permitted values"):
        validate_arguments(SEARCH_SCHEMA, {"query": "x", "mode": "sparse"})


def test_array_items_and_length_are_enforced() -> None:
    with pytest.raises(ToolInputInvalidError, match=r"arguments\.tags\[1\]"):
        validate_arguments(SEARCH_SCHEMA, {"query": "x", "tags": ["ok", "NOT OK"]})
    with pytest.raises(ToolInputInvalidError, match="more than 3 items"):
        validate_arguments(SEARCH_SCHEMA, {"query": "x", "tags": ["a", "b", "c", "d"]})


def test_nested_objects_are_validated_with_their_path() -> None:
    with pytest.raises(ToolInputInvalidError, match=r"arguments\.filters"):
        validate_arguments(
            SEARCH_SCHEMA,
            {"query": "x", "filters": {"tenant": "a", "raw_sql": "select 1"}},
        )


def test_a_rejected_value_never_appears_in_the_message() -> None:
    """These messages reach events, operator logs and the model's context."""

    canary = "canary-secret-value-must-not-leak"

    with pytest.raises(ToolInputInvalidError) as excinfo:
        validate_arguments(SEARCH_SCHEMA, {"query": "x", "top_k": canary})

    assert canary not in str(excinfo.value)
    assert "top_k" in str(excinfo.value)


def test_an_unexpected_property_name_is_reported_but_not_its_value() -> None:
    canary = "canary-secret-value-must-not-leak"

    with pytest.raises(ToolInputInvalidError) as excinfo:
        validate_arguments(SEARCH_SCHEMA, {"query": "x", "extra": canary})

    assert canary not in str(excinfo.value)
