"""The web-search adapter, and the cross-check that keeps its evidence honest.

``web_search_result`` blocks carry a URL the search tool actually returned but
no readable page content, while ``ExternalSearchHit.text`` has to be real text
because it becomes evidence an agent reads. The adapter therefore takes URLs
from the tool and extracts from the model, and drops any extract whose URL the
tool never produced. These tests drive that split through a fake client, which
is what lets ``anthropic`` stay an optional extra.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_workbench.adapters.research.anthropic_web_search import (
    MAX_EXTRACT_CHARS,
    WEB_SEARCH_TOOL_TYPE,
    AnthropicWebSearch,
    AnthropicWebSearchUnavailableError,
    build_anthropic_web_search,
)
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.research import ExternalSearchPort


class _FakeMessages:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _search_results(*urls: str) -> dict[str, Any]:
    return {
        "type": "web_search_tool_result",
        "content": [
            {"type": "web_search_result", "url": url, "title": f"title {url}"}
            for url in urls
        ],
    }


def _reported(*items: dict[str, str]) -> dict[str, Any]:
    return {"type": "text", "text": json.dumps({"results": list(items)})}


def _run(response: dict[str, Any], *, limit: int = 5) -> Any:
    messages = _FakeMessages(response)
    adapter = AnthropicWebSearch(messages=messages, model="claude-opus-5")
    hits = asyncio.run(
        adapter.search(
            query="hybrid retrieval tradeoffs",
            limit=limit,
            cancellation=NullCancellationToken(),
        )
    )
    return hits, messages


def test_the_adapter_satisfies_the_external_search_port() -> None:
    assert isinstance(
        AnthropicWebSearch(messages=_FakeMessages({}), model="m"), ExternalSearchPort
    )


def test_a_result_the_search_tool_returned_becomes_evidence() -> None:
    hits, messages = _run(
        {
            "content": [
                _search_results("https://example.com/a"),
                _reported(
                    {
                        "url": "https://example.com/a",
                        "title": "Hybrid retrieval",
                        "extract": "Hybrid retrieval fuses sparse and dense arms.",
                    }
                ),
            ]
        }
    )

    assert len(hits) == 1
    assert hits[0].url == "https://example.com/a"
    assert hits[0].title == "Hybrid retrieval"
    assert hits[0].text == "Hybrid retrieval fuses sparse and dense arms."
    # The dynamic-filtering tool version, and no `code_execution` beside it --
    # declaring a second execution environment confuses the model.
    tools = messages.calls[0]["tools"]
    assert [tool["type"] for tool in tools] == [WEB_SEARCH_TOOL_TYPE]


def test_a_url_the_search_tool_never_returned_is_dropped() -> None:
    """The whole point of the cross-check: a model-invented source is not evidence."""

    hits, _ = _run(
        {
            "content": [
                _search_results("https://example.com/real"),
                _reported(
                    {
                        "url": "https://example.com/real",
                        "title": "Real",
                        "extract": "Something the page says.",
                    },
                    {
                        "url": "https://invented.example/nope",
                        "title": "Invented",
                        "extract": "A confident summary of a page nobody fetched.",
                    },
                ),
            ]
        }
    )

    assert [hit.url for hit in hits] == ["https://example.com/real"]


def test_a_failed_search_reports_no_evidence_instead_of_raising() -> None:
    """An errored search sends an object where a successful one sends a list.

    Indexing that object is the documented way to turn a search error into a
    TypeError, so the adapter has to branch on the shape.
    """

    hits, _ = _run(
        {
            "content": [
                {
                    "type": "web_search_tool_result",
                    "content": {
                        "type": "web_search_tool_result_error",
                        "error_code": "max_uses_exceeded",
                    },
                },
                _reported(
                    {
                        "url": "https://example.com/a",
                        "title": "t",
                        "extract": "e",
                    }
                ),
            ]
        }
    )

    assert hits == ()


def test_a_refusal_reports_no_evidence() -> None:
    hits, _ = _run(
        {
            "stop_reason": "refusal",
            "content": [_search_results("https://example.com/a")],
        }
    )

    assert hits == ()


def test_results_are_capped_at_the_requested_limit() -> None:
    urls = [f"https://example.com/{index}" for index in range(5)]
    hits, _ = _run(
        {
            "content": [
                _search_results(*urls),
                _reported(
                    *({"url": url, "title": "t", "extract": "e"} for url in urls)
                ),
            ]
        },
        limit=2,
    )

    assert len(hits) == 2


def test_a_repeated_url_is_recorded_once() -> None:
    hits, _ = _run(
        {
            "content": [
                _search_results("https://example.com/a"),
                _reported(
                    {"url": "https://example.com/a", "title": "t", "extract": "one"},
                    {"url": "https://example.com/a", "title": "t", "extract": "two"},
                ),
            ]
        }
    )

    assert [hit.text for hit in hits] == ["one"]


def test_an_over_long_extract_is_truncated_to_the_evidence_bound() -> None:
    """EvidenceText caps at 8192; an unbounded extract would fail to construct."""

    hits, _ = _run(
        {
            "content": [
                _search_results("https://example.com/a"),
                _reported(
                    {
                        "url": "https://example.com/a",
                        "title": "t",
                        "extract": "x" * (MAX_EXTRACT_CHARS + 500),
                    }
                ),
            ]
        }
    )

    assert len(hits[0].text) == MAX_EXTRACT_CHARS


def test_a_response_with_no_structured_answer_yields_nothing() -> None:
    hits, _ = _run(
        {
            "content": [
                _search_results("https://example.com/a"),
                {"type": "text", "text": "I searched but here is prose instead."},
            ]
        }
    )

    assert hits == ()


def test_building_without_an_api_key_refuses_with_the_reason() -> None:
    with pytest.raises(AnthropicWebSearchUnavailableError, match="API key"):
        build_anthropic_web_search(api_key=None, model="claude-opus-5", max_uses=5)
