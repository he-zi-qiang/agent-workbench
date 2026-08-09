"""Reading the outside world, and the two shapes that reading comes in.

``fetch_page`` extracts readable text; ``download_document`` returns bytes. They
are two tools rather than one with a mode because the failure of collapsing them
is already recorded in ADR-027 §2: a PDF put through the HTML extractor becomes
a page of garbled text that reads like a successful read. So a page whose
content type is not text is refused here by name, with the other tool named in
the refusal, rather than extracted from.

Nothing in this module knows what a workspace, a tenant or an owner is. What it
reads goes back over the protocol and no further.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from agent_workbench.adapters.research.address_guard import (
    AddressResolver,
    DestinationRefusedError,
    resolve_addresses,
)
from agent_workbench.adapters.research.guarded_fetch import (
    GuardedHttpClient,
    guarded_get,
)
from agent_workbench.adapters.research.page_text import page_text
from agent_workbench.apps.web_mcp.contract import (
    MAX_DOCUMENT_BYTES,
    MAX_PAGE_BYTES,
    DownloadRequest,
    FetchPageRequest,
)

#: Said plainly rather than disguised as a browser, on the same reasoning the
#: search adapter states: a site that would rather not be read by a program can
#: see what this is and refuse, which is its call to make.
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (compatible; agent-workbench/1.0; +read-only document fetch)"
)

_PAGE_HEADERS: Final[dict[str, str]] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}

_DOCUMENT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
}

#: Content types ``page_text`` can honestly extract from. Anything else belongs
#: to ``download_document``.
_TEXTUAL_TYPES: Final[tuple[str, ...]] = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "application/xml",
    "text/xml",
)

_CHARSET = re.compile(r"charset=([\w-]+)", re.IGNORECASE)

DEFAULT_MEDIA_TYPE: Final[str] = "application/octet-stream"


class WebFetchError(RuntimeError):
    """The read did not happen, and the code says which kind of not-happening.

    Distinguishing them matters to the caller: a refused destination is a
    permanent answer about that URL, an oversized document is an answer about
    the ceiling, and an upstream 404 is an answer about the page.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Document:
    content: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class WebFetcher:
    """Both read-only operations, over one HTTP client."""

    http: GuardedHttpClient
    timeout_seconds: float = 20.0
    resolve: AddressResolver = resolve_addresses

    async def fetch_page(self, request: FetchPageRequest) -> str:
        response = await self._get(request.url, _PAGE_HEADERS)
        media_type = _media_type_of(response)
        if not any(media_type.startswith(kind) for kind in _TEXTUAL_TYPES):
            raise WebFetchError(
                "not_text",
                f"that URL serves {media_type}, which has no readable text; "
                "use download_document to retrieve it as a file",
            )
        body = _body_of(response, MAX_PAGE_BYTES, "the page")
        return page_text(_decoded(body, response), limit=request.max_chars)

    async def download_document(self, request: DownloadRequest) -> Document:
        response = await self._get(request.url, _DOCUMENT_HEADERS)
        return Document(
            content=_body_of(response, MAX_DOCUMENT_BYTES, "the document"),
            media_type=_media_type_of(response) or DEFAULT_MEDIA_TYPE,
        )

    async def _get(self, url: str, headers: dict[str, str]) -> Any:
        try:
            response = await guarded_get(
                self.http,
                url,
                headers=headers,
                timeout=self.timeout_seconds,
                resolve=self.resolve,
            )
        except DestinationRefusedError as error:
            raise WebFetchError("refused_destination", str(error)) from None
        except Exception as error:
            # Third-party exception text can carry proxy hostnames and local
            # paths. The type is enough for a caller to retry or give up.
            raise WebFetchError(
                "unreachable", f"the request failed ({type(error).__name__})"
            ) from None
        status = int(getattr(response, "status_code", 200))
        if status >= 400:
            raise WebFetchError("http_error", f"the server answered {status}")
        return response


def _media_type_of(response: Any) -> str:
    headers: Any = getattr(response, "headers", None)
    getter: Any = getattr(headers, "get", None) if headers is not None else None
    raw = str(getter("content-type") or "") if getter is not None else ""
    return raw.split(";")[0].strip().lower()


def _body_of(response: Any, ceiling: int, subject: str) -> bytes:
    """The response body, refused rather than cut when it is too large.

    ADR-027 §3.1 and ADR-028 §3.4 agree on this: a truncated document is a
    broken document, and the step that reads it next has no way to tell.
    """

    body: Any = getattr(response, "content", None)
    if not isinstance(body, bytes):
        text: Any = getattr(response, "text", "")
        body = text.encode("utf-8") if isinstance(text, str) else b""
    if len(body) > ceiling:
        raise WebFetchError(
            "too_large",
            f"{subject} is {len(body)} bytes, above the {ceiling}-byte limit",
        )
    return body


def _decoded(body: bytes, response: Any) -> str:
    """Text from bytes, with the charset the response declared if it declared one.

    The GB18030 retry is the search adapter's finding, kept because the cause
    has not changed: a Chinese site that declares no charset decodes as UTF-8
    into replacement characters rather than into an error, so the page looks
    read and says nothing.
    """

    declared = str(getattr(response, "encoding", "") or "") or _declared_charset(
        response
    )
    try:
        text = body.decode(declared or "utf-8")
    except (LookupError, UnicodeDecodeError):
        text = body.decode("utf-8", errors="replace")
    if text.count("�") > 8:
        try:
            return body.decode("gb18030")
        except UnicodeDecodeError:
            return text
    return text


def _declared_charset(response: Any) -> str:
    headers: Any = getattr(response, "headers", None)
    getter: Any = getattr(headers, "get", None) if headers is not None else None
    raw = str(getter("content-type") or "") if getter is not None else ""
    found = _CHARSET.search(raw)
    return found.group(1) if found is not None else ""


__all__ = [
    "DEFAULT_MEDIA_TYPE",
    "USER_AGENT",
    "Document",
    "WebFetchError",
    "WebFetcher",
]
