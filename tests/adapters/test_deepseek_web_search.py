"""The web-search adapter, and what keeps its evidence honest.

The adapter runs search, then fetch, then condense. The search tool returns
``title``, ``url`` and an ``encrypted_content`` blob only the provider can
read -- so the page text comes from this process fetching the URL itself, and
the model is only ever asked to condense text it was handed. URLs and titles
come only from the search tool's result blocks, and the model addresses sources
by *index* rather than by writing a URL -- measured against the live endpoint,
asking it to echo URLs agreed 5-of-6 times on one run and 0-of-6 on the next,
so a URL it writes cannot be the join key. These tests drive the whole path
with no network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_workbench.adapters.research.deepseek_web_search import (
    MAX_EXTRACT_CHARS,
    MAX_PAGE_BYTES,
    MAX_PAGE_CHARS,
    MAX_URL_CHARS,
    MESSAGES_PATH,
    WEB_SEARCH_TOOL_TYPE,
    DeepSeekWebSearch,
    WebSearchUnavailableError,
)
from agent_workbench.adapters.research.guarded_fetch import MAX_REDIRECTS
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.research import ExternalSearchPort

#: What a fetched page says, unless a test says otherwise.
PAGE_HTML = "<html><body><h1>丹东天气</h1><p>今天 晴 23°/36°</p></body></html>"


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakePage:
    def __init__(
        self,
        text: str = PAGE_HTML,
        status_code: int = 200,
        location: str | None = None,
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = {"location": location} if location is not None else {}


class _FakeHttp:
    """Answers POSTs from a queue and GETs from a per-URL page table."""

    def __init__(
        self,
        *responses: _FakeResponse,
        pages: dict[str, Any] | None = None,
        default_page: Any = None,
    ) -> None:
        self._responses = list(responses)
        self._pages = pages or {}
        self._default_page = default_page if default_page is not None else _FakePage()
        self.calls: list[dict[str, Any]] = []
        self.fetched: list[str] = []
        #: What each GET was told about redirects. Recorded because it is the
        #: one observable difference between "the adapter judged every hop" and
        #: "the client silently went wherever Location pointed".
        self.delegated_redirects: list[bool] = []

    async def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
        timeout: float,
    ) -> Any:
        self.fetched.append(url)
        self.delegated_redirects.append(follow_redirects)
        page = self._pages.get(url, self._default_page)
        if isinstance(page, Exception):
            raise page
        return page


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


#: What hostnames resolve to in these tests. Stated rather than looked up, so
#: the suite proves what the guard decides instead of what this machine's DNS
#: happens to answer -- and so it still runs with no network at all. Names that
#: point at this machine answer honestly, which is what makes the refusal test
#: below a test of the guard rather than of a hard-coded name list.
PUBLIC_ADDRESS = "93.184.216.34"
_RESOLVED: dict[str, tuple[str, ...]] = {
    "localhost": ("127.0.0.1",),
    "localhost.localdomain": ("127.0.0.1",),
    "ip6-localhost": ("::1",),
    "metadata.internal": ("169.254.169.254",),
}


async def _resolves_public(host: str) -> tuple[str, ...]:
    return _RESOLVED.get(host, (PUBLIC_ADDRESS,))


def _run(
    *responses: _FakeResponse,
    limit: int = 5,
    pages: dict[str, Any] | None = None,
    default_page: Any = None,
    resolve: Any = _resolves_public,
) -> tuple[Any, _FakeHttp]:
    http = _FakeHttp(*responses, pages=pages, default_page=default_page)
    adapter = DeepSeekWebSearch(
        http=http,
        api_key="sk-test",
        model="deepseek-chat",
        resolve_addresses=resolve,
    )
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


def test_every_searched_page_is_fetched_by_this_process() -> None:
    """The search tool returns no readable content, so the client must fetch.

    Its result blocks carry `title`, `url` and an `encrypted_content` blob that
    only the provider's model can decrypt. Skipping the fetch is what turns the
    condensing turn into a model writing about pages nobody read.
    """

    _, http = _run(
        _FakeResponse(_searched("https://example.com/a", "https://example.com/b")),
        _FakeResponse(_extracted({"source": 1, "extract": "e"})),
    )

    assert http.fetched == ["https://example.com/a", "https://example.com/b"]


def test_the_condensing_turn_is_shown_the_fetched_text() -> None:
    """Grounding, stated as a property of the request rather than hoped for."""

    _, http = _run(
        _FakeResponse(_searched("https://example.com/a")),
        _FakeResponse(_extracted({"source": 1, "extract": "e"})),
        pages={
            "https://example.com/a": _FakePage(
                "<html><body><p>今天 晴 23°/36°</p></body></html>"
            )
        },
    )
    condense = json.dumps(http.calls[1]["json"], ensure_ascii=False)

    assert "今天 晴 23°/36°" in condense
    # Nothing but the page: no search tool to call again, and no earlier turn
    # carrying content this process could not read.
    assert "tools" not in http.calls[1]["json"]
    assert [m["role"] for m in http.calls[1]["json"]["messages"]] == ["user"]


def test_a_page_that_cannot_be_fetched_yields_no_evidence_for_it() -> None:
    """A source we could not read is dropped, not described from its title."""

    hits, _ = _run(
        _FakeResponse(_searched("https://example.com/dead", "https://example.com/ok")),
        _FakeResponse(
            _extracted(
                {"source": 1, "extract": "About the page that loaded."},
            )
        ),
        pages={"https://example.com/dead": _FakePage(status_code=500)},
    )

    assert [hit.url for hit in hits] == ["https://example.com/ok"]


def test_a_fetch_that_raises_is_not_a_failed_search() -> None:
    hits, _ = _run(
        _FakeResponse(_searched("https://example.com/boom", "https://example.com/ok")),
        _FakeResponse(_extracted({"source": 1, "extract": "About the live one."})),
        pages={"https://example.com/boom": TimeoutError("connect timed out")},
    )

    assert [hit.url for hit in hits] == ["https://example.com/ok"]


def test_no_page_readable_means_no_evidence_and_no_condensing_turn() -> None:
    """The failure this whole design exists to prevent, pinned.

    With every page unreadable there is nothing to condense. Asking the model
    anyway is precisely how invented weather figures got recorded as evidence.
    """

    hits, http = _run(
        _FakeResponse(_searched("https://example.com/a")),
        _FakeResponse(_extracted({"source": 1, "extract": "Invented."})),
        default_page=_FakePage(status_code=404),
    )

    assert hits == ()
    assert len(http.calls) == 1


def test_a_loopback_or_private_url_is_never_fetched() -> None:
    """Search results should never name this machine; if one does, it is data."""

    _, http = _run(
        _FakeResponse(
            _searched(
                "http://localhost/admin",
                "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.1/",
                "https://example.com/ok",
            )
        ),
        _FakeResponse(_extracted({"source": 4, "extract": "About the public one."})),
    )

    assert http.fetched == ["https://example.com/ok"]


def test_page_text_is_bounded_before_it_reaches_the_model() -> None:
    body = "<p>" + ("政" * (MAX_PAGE_CHARS * 2)) + "</p>"
    _, http = _run(
        _FakeResponse(_searched("https://example.com/a")),
        _FakeResponse(_extracted({"source": 1, "extract": "e"})),
        pages={"https://example.com/a": _FakePage(f"<html><body>{body}</body></html>")},
    )
    condense = json.dumps(http.calls[1]["json"], ensure_ascii=False)

    assert condense.count("政") == MAX_PAGE_CHARS


def test_an_oversized_body_is_not_parsed() -> None:
    huge = ("<p>x</p>" * (MAX_PAGE_BYTES // 8 + 8)).encode()

    class _Bytes:
        status_code = 200
        content = huge
        encoding = "utf-8"

    hits, http = _run(
        _FakeResponse(_searched("https://example.com/a")),
        _FakeResponse(_extracted({"source": 1, "extract": "e"})),
        pages={"https://example.com/a": _Bytes()},
    )

    assert hits == ()
    assert len(http.calls) == 1


def test_a_gbk_page_that_declared_no_charset_is_still_read() -> None:
    """Chinese sites still serve GB families; UTF-8 decoding them loses the page."""

    class _Gbk:
        status_code = 200
        content = "<html><body><p>丹东今天晴</p></body></html>".encode("gb18030")
        encoding = "utf-8"

    _, http = _run(
        _FakeResponse(_searched("https://example.com/a")),
        _FakeResponse(_extracted({"source": 1, "extract": "e"})),
        pages={"https://example.com/a": _Gbk()},
    )

    assert "丹东今天晴" in json.dumps(http.calls[1]["json"], ensure_ascii=False)


def test_a_fetched_source_becomes_evidence_with_the_models_extract() -> None:
    hits, http = _run(
        _FakeResponse(_searched("https://example.com/a")),
        _FakeResponse(_extracted({"source": 1, "extract": "What that page said."})),
    )

    assert len(hits) == 1
    assert hits[0].url == "https://example.com/a"
    assert hits[0].title == "title of https://example.com/a"
    assert hits[0].text == "What that page said."
    # Two model turns: the first searches, the second condenses fetched text.
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


def test_a_redirect_is_followed_here_so_every_hop_goes_through_the_guard() -> None:
    """`follow_redirects=True` would hand the destination choice to the client.

    A public URL answering `302 Location: http://169.254.169.254/` is the same
    SSRF with one extra step, and a check that only ran on the first URL would
    never see the second. So redirects are followed in the adapter, and the
    guard runs again on each hop.
    """

    _, http = _run(
        _FakeResponse(_searched("https://example.com/redirect")),
        _FakeResponse(_extracted({"source": 1, "extract": "e"})),
        pages={
            "https://example.com/redirect": _FakePage(
                text="", status_code=302, location="http://169.254.169.254/latest/"
            ),
        },
    )

    # The first hop was requested; the second was refused before any connection.
    assert http.fetched == ["https://example.com/redirect"]
    # And this is the line that makes the one above mean something. On its own,
    # "only the first URL was fetched" is equally true of an adapter that told
    # the client to follow redirects itself -- in that case the client would
    # have connected to the metadata address and this fake would never have
    # been asked for the second URL, so the assertion would pass while the
    # thing it is named after had failed.
    assert http.delegated_redirects == [False]


def test_a_redirect_to_a_public_address_is_followed_and_read() -> None:
    """The control. Without it, an adapter that stopped following redirects at
    all would satisfy the refusal above."""

    _, http = _run(
        _FakeResponse(_searched("https://example.com/redirect")),
        _FakeResponse(_extracted({"source": 1, "extract": "e"})),
        pages={
            "https://example.com/redirect": _FakePage(
                text="", status_code=302, location="https://example.com/final"
            ),
            "https://example.com/final": _FakePage(
                "<html><body><p>The page that answered.</p></body></html>"
            ),
        },
    )

    assert http.fetched == [
        "https://example.com/redirect",
        "https://example.com/final",
    ]
    condense = json.dumps(http.calls[1]["json"], ensure_ascii=False)
    assert "The page that answered." in condense


def test_a_relative_redirect_resolves_against_the_url_that_answered() -> None:
    """A browser computes the same destination; so must the guard."""

    _, http = _run(
        _FakeResponse(_searched("https://example.com/a/b")),
        _FakeResponse(_extracted({"source": 1, "extract": "e"})),
        pages={
            "https://example.com/a/b": _FakePage(
                text="", status_code=301, location="../c"
            ),
        },
    )

    assert http.fetched == ["https://example.com/a/b", "https://example.com/c"]


def test_an_endless_redirect_loop_yields_no_evidence_rather_than_spinning() -> None:
    _, http = _run(
        _FakeResponse(_searched("https://example.com/loop")),
        _FakeResponse(_extracted({"source": 1, "extract": "e"})),
        pages={
            "https://example.com/loop": _FakePage(
                text="", status_code=302, location="https://example.com/loop"
            ),
        },
    )

    assert len(http.fetched) == MAX_REDIRECTS + 1
