"""External search over DeepSeek's own web-search server tool (ADR-020).

DeepSeek exposes an Anthropic-compatible endpoint alongside its usual
OpenAI-compatible one, and on that endpoint the model can run a *provider-side*
web search: the request declares the tool, DeepSeek performs the searches, and
the response carries ``web_search_tool_result`` blocks. That is the same shape
Claude Code's WebSearch uses, which is why this adapter is written against the
Messages protocol rather than against a search vendor's REST API.

Written directly on ``httpx`` rather than through a provider SDK. The request is
one POST with a JSON body, ``httpx`` is already a dependency, and the endpoint
being called is DeepSeek's -- pulling in a vendor SDK to talk to a different
vendor's compatible endpoint would add a dependency without adding a guarantee.

**Vendor-undocumented.** DeepSeek's published API reference documents the
Anthropic-compatible endpoint but not a managed web-search tool on it; the
capability is reported to work rather than specified. Everything here is
therefore written to fail legibly rather than to assume: an endpoint that
rejects the tool, or answers without search blocks, produces no evidence and a
readable reason instead of a crash or an invented result.

Two sources of truth, deliberately kept apart. ``web_search_result`` blocks name
the pages the provider actually fetched, but the text an agent later reads has
to come from somewhere the client can read. So the extract is written by the
model, and every extract is matched back to a URL the search tool actually
returned; anything else is dropped. "This page was fetched" is checkable and is
checked. "This summarizes it" is the model's claim, and travels as the untrusted
external evidence it already was.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from agent_workbench.domain.evidence import ExternalSearchHit
from agent_workbench.ports.cancellation import CancellationToken

#: The basic web-search tool version. Deliberately not the `_20260209`
#: dynamic-filtering variant: that one runs server-side code on Anthropic's
#: models, and nothing says DeepSeek implements it.
WEB_SEARCH_TOOL_TYPE: Final[str] = "web_search_20250305"
WEB_SEARCH_TOOL_NAME: Final[str] = "web_search"

#: The Messages-protocol path under the configured base URL.
MESSAGES_PATH: Final[str] = "/v1/messages"
ANTHROPIC_VERSION: Final[str] = "2023-06-01"

#: Bounded well under EvidenceText's 8192 ceiling. The extract summarizes one
#: source rather than copying it: big enough to be useful as evidence, small
#: enough that `limit` sources stay inside one artifact.
MAX_EXTRACT_CHARS: Final[int] = 2000

#: What `EvidenceUrl` accepts. Checked here rather than left to fail at
#: construction: real search results carry tracking URLs well past 2KB, and a
#: URL is the one field that must not be truncated to fit -- a cut URL is a
#: different address, not a shorter one. A source that cannot be recorded
#: faithfully is dropped instead.
MAX_URL_CHARS: Final[int] = 2048
_HTTP_URL = re.compile(r"^https?://[^\s]+$")

_SEARCH_SYSTEM_PROMPT: Final[str] = (
    "You are a research assistant with a web search tool. Search for the "
    "user's query and read the results. Answer briefly; a later turn will ask "
    "you to summarize each source."
)

_EXTRACT_SYSTEM_PROMPT: Final[str] = (
    "You summarize sources you have already read. Reply with JSON only, no "
    "prose and no code fence, in exactly this shape:\n"
    '{"results": [{"source": 1, "extract": "..."}]}\n'
    '"source" is the number of the source in the list you are given. Write one '
    "entry per source you can actually say something about, and omit the rest "
    "rather than guessing. Stay close to what that source itself said, and "
    f"keep each extract under {MAX_EXTRACT_CHARS} characters."
)


@dataclass(frozen=True, slots=True)
class _Source:
    """One page the search tool reported fetching."""

    url: str
    title: str


def _extract_request(sources: list[_Source]) -> str:
    listing = "\n".join(
        f"[{index}] {source.title} — {source.url}"
        for index, source in enumerate(sources, start=1)
    )
    return (
        "Here are the sources you just read, numbered. Summarize what each one "
        "says about my question, referring to them by number.\n\n" + listing
    )


class WebSearchUnavailableError(RuntimeError):
    """The provider could not be reached, or refused the search tool."""


class HttpClient(Protocol):
    """The slice of ``httpx.AsyncClient`` this adapter uses.

    Narrow on purpose: a Protocol here is what lets the tests drive the whole
    request-and-cross-check path with no network and no provider account.
    """

    async def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class DeepSeekWebSearch:
    """``ExternalSearchPort`` over DeepSeek's Anthropic-compatible endpoint."""

    http: HttpClient
    api_key: str
    model: str
    base_url: str = "https://api.deepseek.com/anthropic"
    #: Ceiling on searches per call. The model may search several times for one
    #: query, and each search is work the provider bills for.
    max_uses: int = 5
    max_tokens: int = 8192

    async def search(
        self,
        *,
        query: str,
        limit: int,
        cancellation: CancellationToken,
    ) -> tuple[ExternalSearchHit, ...]:
        cancellation.raise_if_cancelled()
        searched = await self._post(
            system=_SEARCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": query}],
            tools=[
                {
                    "type": WEB_SEARCH_TOOL_TYPE,
                    "name": WEB_SEARCH_TOOL_NAME,
                    "max_uses": self.max_uses,
                }
            ],
        )
        cancellation.raise_if_cancelled()

        # A refusal is not an adapter fault and not a failed search: report no
        # evidence and let the caller decide, exactly as an empty result would.
        if searched.get("stop_reason") == "refusal":
            return ()

        sources = _searched_sources(searched)[:limit]
        if not sources:
            return ()

        # Second turn, and the reason there are two. Asked to report sources in
        # one turn, the model cites a canonical URL for the page rather than the
        # one the tool fetched -- measured against the live endpoint, exact-URL
        # agreement swung between 5-of-6 and 0-of-6 across runs, so matching on
        # the URL it writes is a coin flip. Here it picks an *index* into a list
        # this adapter built from the tool's own results, so the URL and title
        # can only come from the search tool and the model only supplies text.
        # The first turn's reply is echoed back, so it still has the pages it
        # read; `max_uses` applies to that turn, so this one cannot search again.
        extracts = await self._post(
            system=_EXTRACT_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": query},
                {"role": "assistant", "content": searched.get("content", [])},
                {"role": "user", "content": _extract_request(sources)},
            ],
            tools=[],
        )
        cancellation.raise_if_cancelled()

        by_index = {
            index: text
            for index, text in _reported_extracts(extracts).items()
            if 1 <= index <= len(sources)
        }
        hits: list[ExternalSearchHit] = []
        for position, source in enumerate(sources, start=1):
            extract = by_index.get(position, "")
            if extract == "":
                continue
            hits.append(
                ExternalSearchHit(
                    url=source.url,
                    title=source.title[:200] or source.url,
                    text=extract[:MAX_EXTRACT_CHARS],
                )
            )
        return tuple(hits)

    async def _post(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
        response = await self.http.post(
            f"{self.base_url.rstrip('/')}{MESSAGES_PATH}",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=body,
        )
        status = getattr(response, "status_code", 200)
        if status >= 400:
            # The likeliest 4xx here is "this endpoint does not know that tool",
            # which is the vendor-undocumented risk this adapter is built
            # around. Say so with the status rather than raising something the
            # caller has to guess at.
            raise WebSearchUnavailableError(
                f"the provider refused the web-search request (HTTP {status})"
            )
        return _payload(response)


def _payload(response: Any) -> dict[str, Any]:
    body: Any = response.json() if hasattr(response, "json") else response
    return cast("dict[str, Any]", body) if isinstance(body, dict) else {}


def _searched_sources(payload: dict[str, Any]) -> list[_Source]:
    """Every page the search tool reported, in the order it reported them.

    This is the only place a URL or title may come from. The model never gets
    to supply either, which is what makes "this page was fetched" a fact rather
    than a claim.
    """

    sources: list[_Source] = []
    seen: set[str] = set()
    for block in _blocks(payload):
        if block.get("type") != "web_search_tool_result":
            continue
        content: Any = block.get("content")
        # An errored search reports a single object carrying an `error_code`
        # where a successful one reports a list. Indexing without this check is
        # how a search error becomes a TypeError.
        if not isinstance(content, list):
            continue
        for result in cast("list[Any]", content):
            if not isinstance(result, dict):
                continue
            entry = cast("dict[str, Any]", result)
            if entry.get("type") != "web_search_result":
                continue
            url = _clean(entry.get("url"))
            if url in seen or not _recordable(url):
                continue
            seen.add(url)
            sources.append(_Source(url=url, title=_clean(entry.get("title"))))
    return sources


def _reported_extracts(payload: dict[str, Any]) -> dict[int, str]:
    """The model's per-source text, keyed by the index it was given.

    Salvaged entry by entry rather than parsed as one document. Two reasons,
    both measured against the live endpoint: there is no structured-output
    guarantee to lean on (`output_config.format` is an Anthropic feature and
    this endpoint is DeepSeek's), so the JSON arrives wrapped in whatever the
    model felt like; and a reply long enough to hit `max_tokens` is cut
    mid-object, which made whole-document parsing throw away the four complete
    extracts that arrived before the cut. Scanning for objects keeps those.
    """

    decoder = json.JSONDecoder()
    for block in _blocks(payload):
        if block.get("type") != "text":
            continue
        text = _clean(block.get("text"))
        extracts: dict[int, str] = {}
        position = 0
        while (position := text.find("{", position)) != -1:
            try:
                decoded, offset = decoder.raw_decode(text, position)
            except json.JSONDecodeError:
                position += 1
                continue
            position = offset
            _collect_extracts(decoded, extracts)
        if extracts:
            return extracts
    return {}


def _collect_extracts(decoded: Any, into: dict[int, str]) -> None:
    """Record any ``{"source": int, "extract": str}`` this value contains."""

    if isinstance(decoded, list):
        for item in cast("list[Any]", decoded):
            _collect_extracts(item, into)
        return
    if not isinstance(decoded, dict):
        return
    entry = cast("dict[str, Any]", decoded)
    results = entry.get("results")
    if isinstance(results, list):
        _collect_extracts(results, into)
    index = entry.get("source")
    extract = _clean(entry.get("extract"))
    if isinstance(index, int) and not isinstance(index, bool) and extract:
        into.setdefault(index, extract)


def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    content: Any = payload.get("content")
    if not isinstance(content, list):
        return []
    return [
        cast("dict[str, Any]", block)
        for block in cast("list[Any]", content)
        if isinstance(block, dict)
    ]


def _recordable(url: str) -> bool:
    """Whether this URL can be stored as evidence exactly as the tool gave it."""

    return len(url) <= MAX_URL_CHARS and _HTTP_URL.match(url) is not None


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "ANTHROPIC_VERSION",
    "MAX_EXTRACT_CHARS",
    "MAX_URL_CHARS",
    "MESSAGES_PATH",
    "WEB_SEARCH_TOOL_NAME",
    "WEB_SEARCH_TOOL_TYPE",
    "DeepSeekWebSearch",
    "HttpClient",
    "WebSearchUnavailableError",
]
