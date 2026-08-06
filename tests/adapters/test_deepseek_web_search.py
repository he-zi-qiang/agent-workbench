"""The web-search adapter, and the cross-check that keeps its evidence honest.

``web_search_result`` blocks name the pages the provider actually fetched, while
``ExternalSearchHit.text`` has to be text the client can read, because it
becomes evidence an agent reads. The adapter therefore takes URLs from the tool
and extracts from the model, and drops any extract whose URL the tool never
returned. These tests drive that split with no network and no provider account.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_workbench.adapters.research.deepseek_web_search import (
    MAX_EXTRACT_CHARS,
    MESSAGES_PATH,
    WEB_SEARCH_TOOL_TYPE,
    DeepSeekWebSearch,
    WebSearchUnavailableError,
)
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.research import ExternalSearchPort


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHttp:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def _search_results(*urls: str) -> dict[str, Any]:
    return {
        "type": "web_search_tool_result",
        "content": [
            {"type": "web_search_result", "url": url, "title": f"title {url}"}
            for url in urls
        ],
    }


def _reported(*items: dict[str, str], wrapper: str = "{payload}") -> dict[str, Any]:
    payload = json.dumps({"results": list(items)})
    return {"type": "text", "text": wrapper.format(payload=payload)}


def _run(
    payload: dict[str, Any], *, limit: int = 5, status_code: int = 200
) -> tuple[Any, _FakeHttp]:
    http = _FakeHttp(_FakeResponse(payload, status_code))
    adapter = DeepSeekWebSearch(http=http, api_key="sk-test", model="deepseek-chat")
    hits = asyncio.run(
        adapter.search(
            query="hybrid retrieval tradeoffs",
            limit=limit,
            cancellation=NullCancellationToken(),
        )
    )
    return hits, http


def test_the_adapter_satisfies_the_external_search_port() -> None:
    adapter = DeepSeekWebSearch(
        http=_FakeHttp(_FakeResponse({})), api_key="k", model="m"
    )

    assert isinstance(adapter, ExternalSearchPort)


def test_a_result_the_search_tool_returned_becomes_evidence() -> None:
    hits, _ = _run(
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


def test_the_request_targets_the_providers_messages_endpoint() -> None:
    _, http = _run({"content": []})
    call = http.calls[0]

    assert call["url"].endswith(MESSAGES_PATH)
    # The provider's own key, on the Messages protocol's header. There is no
    # second credential: search runs on the model provider's side.
    assert call["headers"]["x-api-key"] == "sk-test"
    assert [tool["type"] for tool in call["json"]["tools"]] == [WEB_SEARCH_TOOL_TYPE]


def test_a_url_the_search_tool_never_returned_is_dropped() -> None:
    """The point of the cross-check: a model-invented source is not evidence."""

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


def test_a_rejected_request_says_the_provider_refused_it() -> None:
    """The vendor-undocumented risk: this endpoint may not know the tool."""

    with pytest.raises(WebSearchUnavailableError, match="HTTP 400"):
        _run({"error": {"message": "unknown tool"}}, status_code=400)


def test_a_failed_search_reports_no_evidence_instead_of_raising() -> None:
    """An errored search sends an object where a successful one sends a list.

    Indexing that object is how a search error becomes a TypeError, so the
    adapter has to branch on the shape.
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
                    {"url": "https://example.com/a", "title": "t", "extract": "e"}
                ),
            ]
        }
    )

    assert hits == ()


def test_json_wrapped_in_a_code_fence_is_still_read() -> None:
    """There is no structured-output guarantee on this endpoint to lean on."""

    hits, _ = _run(
        {
            "content": [
                _search_results("https://example.com/a"),
                _reported(
                    {"url": "https://example.com/a", "title": "t", "extract": "e"},
                    wrapper="Here is what I found:\n```json\n{payload}\n```",
                ),
            ]
        }
    )

    assert [hit.url for hit in hits] == ["https://example.com/a"]


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


def test_an_answer_with_no_json_yields_nothing() -> None:
    hits, _ = _run(
        {
            "content": [
                _search_results("https://example.com/a"),
                {"type": "text", "text": "I searched but here is prose instead."},
            ]
        }
    )

    assert hits == ()
