"""External search over Anthropic's ``web_search`` server tool (ADR-020).

The search runs on Anthropic's side: the request declares the tool, the model
issues queries, and the response carries ``web_search_tool_result`` blocks. This
is the same mechanism Claude Code's WebSearch uses.

Two sources of truth, deliberately kept apart. ``web_search_result`` blocks
carry ``url`` and ``title`` but their page content is ``encrypted_content``,
which a client cannot read -- and ``ExternalSearchHit.text`` has to be real
text, because it becomes the evidence an agent later reads. So the extract is
written by the model under a JSON schema, and every extract is matched back to
a URL the search tool actually returned. A URL the tool never produced is
dropped rather than trusted: "this page exists" is the tool's guarantee, "this
summarizes it" is the model's, and only the first one can be checked here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from agent_workbench.domain.evidence import ExternalSearchHit
from agent_workbench.ports.cancellation import CancellationToken

#: The dynamic-filtering variant. Results are filtered by server-side code
#: before they reach the model's context, which is why `code_execution` must
#: NOT also be declared -- a second execution environment confuses the model.
WEB_SEARCH_TOOL_TYPE: Final[str] = "web_search_20260209"
WEB_SEARCH_TOOL_NAME: Final[str] = "web_search"

#: Bounded well under EvidenceText's 8192 ceiling. The extract is a summary of
#: one source, not a copy of it: a bound this size is enough to be useful as
#: evidence and small enough that `limit` sources stay inside one artifact.
MAX_EXTRACT_CHARS: Final[int] = 2000

_RESULT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "title", "extract"],
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "extract": {"type": "string"},
                },
            },
        }
    },
}

_SYSTEM_PROMPT: Final[str] = (
    "You are a research assistant. Search the web for the user's query, then "
    "report what you found. For every source you actually visited, give its "
    "exact URL, its title, and a factual extract of what that page says about "
    "the query. Do not include a source you did not retrieve, and do not "
    "invent a URL. Keep each extract under "
    f"{MAX_EXTRACT_CHARS} characters and stay close to the source's own claims."
)


class MessagesClient(Protocol):
    """The slice of the Anthropic client this adapter uses.

    Narrow on purpose: a Protocol here is what lets the tests drive the whole
    parse-and-cross-check path without the SDK installed, which is the reason
    `anthropic` can stay an optional extra at all.
    """

    async def create(self, **kwargs: Any) -> Any: ...


class AnthropicWebSearchUnavailableError(RuntimeError):
    """The extra is missing, or no API key was configured."""


@dataclass(frozen=True, slots=True)
class AnthropicWebSearch:
    """``ExternalSearchPort`` over the Anthropic web-search server tool."""

    messages: MessagesClient
    model: str
    #: Ceiling on searches per request. The model may search more than once for
    #: one query; without this an ambiguous query can fan out and be billed per
    #: search.
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
        response = await self.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_SYSTEM_PROMPT,
            tools=[
                {
                    "type": WEB_SEARCH_TOOL_TYPE,
                    "name": WEB_SEARCH_TOOL_NAME,
                    "max_uses": self.max_uses,
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": _RESULT_SCHEMA}},
            messages=[{"role": "user", "content": query}],
        )
        cancellation.raise_if_cancelled()

        # The model declining is not the same as the search failing, and it is
        # not an adapter fault either: report no evidence and let the caller
        # decide, exactly as an empty result set would.
        if _attr(response, "stop_reason") == "refusal":
            return ()

        retrieved = _searched_urls(response)
        reported = _reported_results(response)
        hits: list[ExternalSearchHit] = []
        seen: set[str] = set()
        for item in reported:
            url = _clean(item.get("url"))
            extract = _clean(item.get("extract"))
            title = _clean(item.get("title")) or url
            # The cross-check. `retrieved` is what the search tool returned;
            # anything else the model wrote down did not come from a search.
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


def _searched_urls(response: Any) -> frozenset[str]:
    """Every URL the search tool itself returned, across all of its uses."""

    urls: set[str] = set()
    for block in _blocks(response):
        if _attr(block, "type") != "web_search_tool_result":
            continue
        content = _attr(block, "content")
        # An errored search reports a single object with an `error_code`, where
        # a successful one reports a list. Indexing without this check is the
        # documented way to turn a search error into a TypeError.
        if not isinstance(content, list):
            continue
        for result in cast("list[Any]", content):
            if _attr(result, "type") != "web_search_result":
                continue
            url = _clean(_attr(result, "url"))
            if url != "":
                urls.add(url)
    return frozenset(urls)


def _reported_results(response: Any) -> list[dict[str, Any]]:
    """The model's structured answer, or nothing if it did not produce one."""

    for block in _blocks(response):
        if _attr(block, "type") != "text":
            continue
        text = _clean(_attr(block, "text"))
        if text == "":
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        results = cast("dict[str, Any]", payload).get("results")
        if isinstance(results, list):
            return [
                item for item in cast("list[Any]", results) if isinstance(item, dict)
            ]
    return []


def _blocks(response: Any) -> list[Any]:
    content = _attr(response, "content")
    return cast("list[Any]", content) if isinstance(content, list) else []


def _attr(value: Any, name: str) -> Any:
    """Read a field from an SDK model or from the plain dict a test supplies."""

    if isinstance(value, dict):
        return cast("dict[str, Any]", value).get(name)
    return getattr(value, name, None)


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def build_anthropic_web_search(
    *,
    api_key: str | None,
    model: str,
    max_uses: int,
    base_url: str | None = None,
) -> AnthropicWebSearch:
    """Construct the adapter, or refuse with the reason.

    Refusing is the point: the caller turns this into
    ``provider_unavailable`` and the Task records that no evidence was
    gathered, which is a better outcome than a Worker that cannot start.
    """

    if not api_key:
        raise AnthropicWebSearchUnavailableError(
            "external search needs an Anthropic API key; "
            "set AW_SECRETS__ANTHROPIC_API_KEY"
        )
    return AnthropicWebSearch(
        messages=_sdk_messages(api_key=api_key, base_url=base_url),
        model=model,
        max_uses=max_uses,
    )


def _sdk_messages(*, api_key: str, base_url: str | None) -> MessagesClient:
    """Build the SDK client, keeping its unresolved types out of the caller.

    The import is unresolvable to the type checker by design: CI does not
    install the extra, and requiring it there would make an optional dependency
    mandatory for everyone running the gates. Narrowing to ``MessagesClient``
    here means that unknown-ness stops at this function instead of spreading
    into every call site.
    """

    try:
        import anthropic  # pyright: ignore[reportMissingImports]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise AnthropicWebSearchUnavailableError(
            "external search needs the 'research' extra; "
            "install it with: uv sync --extra research"
        ) from exc

    options: dict[str, Any] = {"api_key": api_key}
    if base_url is not None:
        options["base_url"] = base_url
    client = cast(
        "Any",
        anthropic.AsyncAnthropic(**options),  # pyright: ignore[reportUnknownMemberType]
    )
    return cast("MessagesClient", client.messages)


__all__ = [
    "MAX_EXTRACT_CHARS",
    "WEB_SEARCH_TOOL_NAME",
    "WEB_SEARCH_TOOL_TYPE",
    "AnthropicWebSearch",
    "AnthropicWebSearchUnavailableError",
    "MessagesClient",
    "build_anthropic_web_search",
]
