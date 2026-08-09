"""The closed input contract for ``run_python`` (ADR-029 §3.1, §3.3).

Every refusal here is paired with the accepted form of the same thing. A test
that only asserts "this was rejected" cannot tell a working validator from one
that rejects everything, and a contract whose whole job is to be narrow is
exactly where that mistake would survive.
"""

from __future__ import annotations

import base64

import pytest

from agent_workbench.apps.sandbox_mcp.contract import (
    MAX_INPUT_FILE_BYTES,
    MAX_INPUT_FILES,
    MAX_SCRIPT_CHARS,
    MAX_TOTAL_INPUT_BYTES,
    RUN_PYTHON_INPUT_SCHEMA,
    RUN_PYTHON_OUTPUT_SCHEMA,
    SandboxInputError,
    base64_length,
    parse_run_request,
)
from agent_workbench.runtime.schema_validation import (
    SUPPORTED_KEYWORDS,
    assert_schema_supported,
)


def _file(name: str, content: bytes) -> dict[str, str]:
    return {"name": name, "content_base64": base64.b64encode(content).decode("ascii")}


def test_the_schemas_stay_inside_the_validator_this_project_actually_has() -> None:
    """The 17 keywords are the whole vocabulary; nothing here adds an 18th."""

    assert_schema_supported(RUN_PYTHON_INPUT_SCHEMA, origin="run_python.input")
    assert_schema_supported(RUN_PYTHON_OUTPUT_SCHEMA, origin="run_python.output")
    assert len(SUPPORTED_KEYWORDS) == 17


def test_the_contract_names_no_path_tenant_owner_or_artifact() -> None:
    """ADR-029 §3.1: a process that cannot name a tenant cannot write under one.

    Asserted on every property the schema declares at any depth, rather than on
    the parsed dataclass: a field added to the wire without a field on the
    parser would still be a field this process accepts.
    """

    declared = _property_names(RUN_PYTHON_INPUT_SCHEMA)
    assert declared == {"script", "inputs", "name", "content_base64"}
    for forbidden in ("tenant", "owner", "artifact", "principal", "workspace", "url"):
        assert not any(forbidden in name for name in declared)


def _property_names(schema: object) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    names: set[str] = set()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            names.add(str(name))
            names |= _property_names(child)
    names |= _property_names(schema.get("items"))
    return names


def test_a_script_with_bounded_inputs_is_accepted() -> None:
    """The control group for every refusal below."""

    request = parse_run_request(
        {
            "script": "print(1)",
            "inputs": [_file("data_1.csv", b"a,b\n1,2\n"), _file("draft-v2.md", b"#")],
        }
    )

    assert request.script == "print(1)"
    assert [file.name for file in request.inputs] == ["data_1.csv", "draft-v2.md"]
    assert request.inputs[0].content == b"a,b\n1,2\n"


def test_a_request_without_inputs_is_accepted() -> None:
    """A script that computes rather than transforms needs no files."""

    assert parse_run_request({"script": "print(2 + 2)"}).inputs == ()


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "sub/dir.txt",
        "sub\\dir.txt",
        "..",
        ".hidden",
        "",
        "a" * 129,
        "naughty name.txt",
    ],
)
def test_a_name_that_is_not_flat_is_refused(name: str) -> None:
    with pytest.raises(SandboxInputError):
        parse_run_request({"script": "print(1)", "inputs": [_file(name, b"x")]})


@pytest.mark.parametrize("name", ["draft-v2.md", "data_1.csv", "a", "a" * 128, "9.txt"])
def test_a_flat_name_is_accepted(name: str) -> None:
    """The control group for the refusals above."""

    request = parse_run_request({"script": "print(1)", "inputs": [_file(name, b"x")]})

    assert request.inputs[0].name == name


def test_two_entries_for_one_name_are_refused() -> None:
    with pytest.raises(SandboxInputError, match="duplicate"):
        parse_run_request(
            {
                "script": "print(1)",
                "inputs": [_file("a.txt", b"one"), _file("a.txt", b"two")],
            }
        )


def test_content_that_is_not_base64_is_refused_and_valid_base64_is_not() -> None:
    with pytest.raises(SandboxInputError, match="base64"):
        parse_run_request(
            {
                "script": "print(1)",
                "inputs": [{"name": "a.txt", "content_base64": "not base64!!"}],
            }
        )

    assert (
        parse_run_request({"script": "print(1)", "inputs": [_file("a.txt", b"")]})
        .inputs[0]
        .content
        == b""
    )


def test_a_script_past_the_ceiling_is_refused_and_one_at_it_is_not() -> None:
    at_limit = "#" * MAX_SCRIPT_CHARS
    assert len(parse_run_request({"script": at_limit}).script) == MAX_SCRIPT_CHARS

    with pytest.raises(SandboxInputError):
        parse_run_request({"script": at_limit + "#"})


def test_an_empty_or_blank_script_is_refused() -> None:
    for script in ("", "   \n\t "):
        with pytest.raises(SandboxInputError):
            parse_run_request({"script": script})


def test_too_many_input_files_are_refused_and_the_limit_itself_is_not() -> None:
    at_limit = [_file(f"f{index}.txt", b"x") for index in range(MAX_INPUT_FILES)]
    parsed = parse_run_request({"script": "print(1)", "inputs": at_limit})
    assert len(parsed.inputs) == MAX_INPUT_FILES

    with pytest.raises(SandboxInputError):
        parse_run_request(
            {
                "script": "print(1)",
                "inputs": [*at_limit, _file("one-more.txt", b"x")],
            }
        )


def test_one_oversized_file_is_refused_and_a_file_at_the_limit_is_not() -> None:
    """Checked after decoding, not on the encoded length.

    The schema bounds the base64 string, which is the cheap check; this is the
    one that holds when a caller pads or pretty-prints its encoding.
    """

    at_limit = _file("big.bin", b"x" * MAX_INPUT_FILE_BYTES)
    parsed = parse_run_request({"script": "print(1)", "inputs": [at_limit]})
    assert len(parsed.inputs[0].content) == MAX_INPUT_FILE_BYTES

    with pytest.raises(SandboxInputError):
        parse_run_request(
            {
                "script": "print(1)",
                "inputs": [_file("big.bin", b"x" * (MAX_INPUT_FILE_BYTES + 1))],
            }
        )


def test_the_total_input_ceiling_is_enforced_across_files() -> None:
    """Each file fits; together they do not."""

    count = MAX_TOTAL_INPUT_BYTES // MAX_INPUT_FILE_BYTES
    within = [
        _file(f"f{index}.bin", b"x" * MAX_INPUT_FILE_BYTES) for index in range(count)
    ]
    assert len(parse_run_request({"script": "print(1)", "inputs": within}).inputs) == (
        count
    )

    with pytest.raises(SandboxInputError, match="total"):
        parse_run_request(
            {
                "script": "print(1)",
                "inputs": [*within, _file("over.bin", b"x")],
            }
        )


def test_an_unexpected_property_is_refused() -> None:
    with pytest.raises(SandboxInputError):
        parse_run_request({"script": "print(1)", "timeout_seconds": 3600})


def test_base64_length_matches_the_encoder() -> None:
    """The schema's maxLength is computed; a wrong formula would reject legally
    sized files or admit oversized ones before the decoder ever sees them."""

    for size in (0, 1, 2, 3, 4, 1023, MAX_INPUT_FILE_BYTES):
        assert base64_length(size) == len(base64.b64encode(b"x" * size))
