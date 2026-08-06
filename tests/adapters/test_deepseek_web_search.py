"""The web-search adapter, and what keeps its evidence honest.

The adapter runs two turns: one that searches, and one that summarizes the
sources the first turn fetched. URLs and titles come only from the search tool's
own result blocks, and the model addresses those sources by *index* rather than
by writing a URL -- measured against the live endpoint, asking it to echo URLs
agreed 5-of-6 times on one run and 0-of-6 on the next, so a URL it writes cannot
be the join key. These tests drive both turns with no network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_workbench.adapters.research.deepseek_web_search import (
    MAX_EXTRACT_CHARS,
    MAX_URL_CHARS,
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
    """Answers each POST from a queue, so both turns can be scripted."""

    def __init__(self, *responses: _FakeResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]


def _searched(*urls: str) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "web_search_tool_result",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": url,
                        "title": f"title of {url}",
                    }
                    for url in urls
                ],
            }
        ]
    }


def _extracted(*items: dict[str, Any], wrapper: str = "{payload}") -> dict[str, Any]:
    payload = json.dumps({"results": list(items)})
    return {"content": [{"type": "text", "text": wrapper.format(payload=payload)}]}


def _run(*responses: _FakeResponse, limit: int = 5) -> tuple[Any, _FakeHttp]:
    http = _FakeHttp(*responses)
    adapter = DeepSeekWebSearch(http=http, api_key="sk-test", model="deepseek-chat")
    hits = asyncio.run(
        adapter.search(
            query="今天丹东天气怎么样",
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


def test_a_searched_source_becomes_evidence_with_the_models_extract() -> None:
    hits, http = _run(
        _FakeResponse(_searched("https://example.com/a")),
        _FakeResponse(_extracted({"source": 1, "extract": "What that page said."})),
    )

    assert len(hits) == 1
    assert hits[0].url == "https://example.com/a"
    assert hits[0].title == "title of https://example.com/a"
    assert hits[0].text == "What that page said."
    # Two turns: the first searches, the second only summarizes.
    assert len(http.calls) == 2
    assert "tools" in http.calls[0]["json"]
    assert "tools" not in http.calls[1]["json"]


def test_the_request_targets_the_providers_messages_endpoint() -> None:
    _, http = _run(_FakeResponse({"content": []}))
    call = http.calls[0]

    assert call["url"].endswith(MESSAGES_PATH)
    # The provider's own key, on the Messages protocol's header. There is no
    # second credential: search runs on the model provider's side.
    assert call["headers"]["x-api-key"] == "sk-test"
    assert [tool["type"] for tool in call["json"]["tools"]] == [WEB_SEARCH_TOOL_TYPE]


def test_the_url_and_title_can_only_come_from_the_search_tool() -> None:
    """The model addresses sources by index, so it cannot introduce a source."""

    hits, _ = _run(
        _FakeResponse(_searched("https://example.com/real")),
        _FakeResponse(
            _extracted(
                {"source": 1, "extract": "About the real page."},
                # A URL and title the model made up, and an index nobody offered.
                {
                    "source": 9,
                    "url": "https://invented.example/nope",
                    "title": "Invented",
                    "extract": "A summary of a page nobody fetched.",
                },
            )
        ),
    )

    assert [hit.url for hit in hits] == ["https://example.com/real"]


def test_a_truncated_reply_keeps_the_entries_that_did_arrive() -> None:
    """`max_tokens` cuts mid-object; discarding the whole reply loses good work.

    Observed against the live endpoint: whole-document parsing returned nothing
    from a reply whose first extracts were complete.
    """

    complete = json.dumps({"source": 1, "extract": "First source."})
    truncated = '{"results": [' + complete + ', {"source": 2, "extract": "cut off'
    hits, _ = _run(
        _FakeResponse(_searched("https://example.com/a", "https://example.com/b")),
        _FakeResponse({"content": [{"type": "text", "text": truncated}]}),
    )

    assert [hit.text for hit in hits] == ["First source."]


def test_json_wrapped_in_a_code_fence_is_still_read() -> None:
    """There is no structured-output guarantee on this endpoint to lean on."""

    hits, _ = _run(
        _FakeResponse(_searched("https://example.com/a")),
        _FakeResponse(
            _extracted(
                {"source": 1, "extract": "Fenced."},
                wrapper="Here you go:\n```json\n{payload}\n```",
            )
        ),
    )

    assert [hit.text for hit in hits] == ["Fenced."]


def test_a_rejected_request_says_the_provider_refused_it() -> None:
    """The vendor-undocumented risk: this endpoint may not know the tool."""

    with pytest.raises(WebSearchUnavailableError, match="HTTP 400"):
        _run(_FakeResponse({"error": {"message": "unknown tool"}}, status_code=400))


def test_a_failed_search_reports_no_evidence_instead_of_raising() -> None:
    """An errored search sends an object where a successful one sends a list.

    Indexing that object is how a search error becomes a TypeError, so the
    adapter has to branch on the shape.
    """

    hits, http = _run(
        _FakeResponse(
            {
                "content": [
                    {
                        "type": "web_search_tool_result",
                        "content": {
                            "type": "web_search_tool_result_error",
                            "error_code": "max_uses_exceeded",
                        },
                    }
                ]
            }
        )
    )

    assert hits == ()
    # No sources means nothing to summarize; the second turn is not paid for.
    assert len(http.calls) == 1


def test_a_refusal_reports_no_evidence() -> None:
    payload = _searched("https://example.com/a")
    payload["stop_reason"] = "refusal"

    hits, http = _run(_FakeResponse(payload))

    assert hits == ()
    assert len(http.calls) == 1


def test_a_url_too_long_to_record_is_dropped_rather_than_truncated() -> None:
    """A cut URL is a different address, not a shorter one.

    Real search results carry tracking URLs past EvidenceUrl's 2048-character
    bound; letting one through fails at construction, and trimming it would
    record a link pointing somewhere else.
    """

    huge = "https://example.com/?q=" + "x" * MAX_URL_CHARS
    hits, _ = _run(
        _FakeResponse(_searched(huge, "https://example.com/ok")),
        _FakeResponse(_extracted({"source": 1, "extract": "About the short one."})),
    )

    assert [hit.url for hit in hits] == ["https://example.com/ok"]


def test_results_are_capped_at_the_requested_limit() -> None:
    urls = [f"https://example.com/{index}" for index in range(5)]
    hits, _ = _run(
        _FakeResponse(_searched(*urls)),
        _FakeResponse(
            _extracted(*({"source": n, "extract": f"e{n}"} for n in range(1, 6)))
        ),
        limit=2,
    )

    assert len(hits) == 2


def test_an_answer_with_no_json_yields_nothing() -> None:
    hits, _ = _run(
        _FakeResponse(_searched("https://example.com/a")),
        _FakeResponse({"content": [{"type": "text", "text": "Prose, no JSON."}]}),
    )

    assert hits == ()


def test_an_over_long_extract_is_truncated_to_the_evidence_bound() -> None:
    """EvidenceText caps at 8192; an unbounded extract would fail to construct."""

    hits, _ = _run(
        _FakeResponse(_searched("https://example.com/a")),
        _FakeResponse(
            _extracted({"source": 1, "extract": "x" * (MAX_EXTRACT_CHARS + 500)})
        ),
    )

    assert len(hits[0].text) == MAX_EXTRACT_CHARS
