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

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM_PROMPT: Final[str] = (
    "You are a research assistant with a web search tool. Search for the "
    "user's query, then report what you found.\n\n"
    "Reply with JSON only, in this exact shape and with no prose around it:\n"
    '{"results": [{"url": "...", "title": "...", "extract": "..."}]}\n\n'
    "Include one entry per source you actually retrieved. Use the source's "
    "exact URL. Never invent a URL or include a page you did not retrieve. "
    f"Keep each extract under {MAX_EXTRACT_CHARS} characters and stay close to "
    "what the source itself says."
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
    max_tokens: int = 4096

    async def search(
        self,
        *,
        query: str,
        limit: int,
        cancellation: CancellationToken,
    ) -> tuple[ExternalSearchHit, ...]:
        cancellation.raise_if_cancelled()
        response = await self.http.post(
            f"{self.base_url.rstrip('/')}{MESSAGES_PATH}",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": _SYSTEM_PROMPT,
                "tools": [
                    {
                        "type": WEB_SEARCH_TOOL_TYPE,
                        "name": WEB_SEARCH_TOOL_NAME,
                        "max_uses": self.max_uses,
                    }
                ],
                "messages": [{"role": "user", "content": query}],
            },
        )
        cancellation.raise_if_cancelled()

        status = getattr(response, "status_code", 200)
        if status >= 400:
            # The likeliest 400 here is "this endpoint does not know that tool",
            # which is the vendor-undocumented risk this adapter is built
            # around. Say so with the status rather than raising something the
            # caller has to guess at.
            raise WebSearchUnavailableError(
                f"the provider refused the web-search request (HTTP {status})"
            )

        payload = _payload(response)
        # A refusal is not an adapter fault and not a failed search: report no
        # evidence and let the caller decide, exactly as an empty result would.
        if payload.get("stop_reason") == "refusal":
            return ()

        retrieved = _searched_urls(payload)
        hits: list[ExternalSearchHit] = []
        seen: set[str] = set()
        for item in _reported_results(payload):
            url = _clean(item.get("url"))
            extract = _clean(item.get("extract"))
            title = _clean(item.get("title")) or url
            # The cross-check. `retrieved` is what the search tool itself
            # returned; anything else the model wrote down was not searched for.
            if url not in retrieved or url in seen or extract == "":
                continue
            seen.add(url)
            hits.append(
                ExternalSearchHit(
                    url=url,
                    title=title[:200],
                    text=extract[:MAX_EXTRACT_CHARS],
                )
            )
            if len(hits) >= limit:
                break
        return tuple(hits)


def _payload(response: Any) -> dict[str, Any]:
    body: Any = response.json() if hasattr(response, "json") else response
    return cast("dict[str, Any]", body) if isinstance(body, dict) else {}


def _searched_urls(payload: dict[str, Any]) -> frozenset[str]:
    """Every URL the search tool itself returned, across all of its uses."""

    urls: set[str] = set()
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
            if url != "":
                urls.add(url)
    return frozenset(urls)


def _reported_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The model's answer, parsed leniently.

    Lenient because there is no structured-output guarantee to lean on here:
    `output_config.format` is an Anthropic feature and this endpoint is
    DeepSeek's, so the JSON arrives inside whatever the model wrapped it in --
    a code fence, a sentence of preamble. Finding the object beats demanding
    that the whole block parse.
    """

    for block in _blocks(payload):
        if block.get("type") != "text":
            continue
        text = _clean(block.get("text"))
        if text == "":
            continue
        match = _JSON_OBJECT.search(text)
        if match is None:
            continue
        try:
            decoded: Any = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue
        results: Any = cast("dict[str, Any]", decoded).get("results")
        if isinstance(results, list):
            return [
                cast("dict[str, Any]", item)
                for item in cast("list[Any]", results)
                if isinstance(item, dict)
            ]
    return []


def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    content: Any = payload.get("content")
    if not isinstance(content, list):
        return []
    return [
        cast("dict[str, Any]", block)
        for block in cast("list[Any]", content)
        if isinstance(block, dict)
    ]


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "ANTHROPIC_VERSION",
    "MAX_EXTRACT_CHARS",
    "MESSAGES_PATH",
    "WEB_SEARCH_TOOL_NAME",
    "WEB_SEARCH_TOOL_TYPE",
    "DeepSeekWebSearch",
    "HttpClient",
    "WebSearchUnavailableError",
]
