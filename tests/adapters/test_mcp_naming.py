"""MCP names become stable local ToolNames without lossy deletion."""

from __future__ import annotations

import pytest

from agent_workbench.adapters.mcp.naming import SkipReason, tool_name_for


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("camelCase", "mcp_docs_camelcase"),
        ("kebab-case", "mcp_docs_kebab_case"),
        ("with.dots", "mcp_docs_with_dots"),
        ("with spaces", "mcp_docs_with_spaces"),
    ],
)
def test_supported_remote_names_have_one_deterministic_home(
    remote: str, expected: str
) -> None:
    assert tool_name_for("docs", remote) == expected
    assert tool_name_for("docs", remote) == expected


@pytest.mark.parametrize("remote", ["Ünicode", "", "x" * 64])
def test_unmappable_or_oversized_names_are_skipped(remote: str) -> None:
    result = tool_name_for("docs", remote)

    assert isinstance(result, SkipReason)
    assert result.remote_name == remote
    assert result.reason


def test_the_local_alias_disambiguates_equal_remote_names() -> None:
    first = tool_name_for("docs", "search")
    second = tool_name_for("tickets", "search")

    assert first == "mcp_docs_search"
    assert second == "mcp_tickets_search"
    assert first != second
