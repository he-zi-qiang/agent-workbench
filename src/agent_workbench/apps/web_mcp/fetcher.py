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
import zlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

from agent_workbench.adapters.research.address_guard import (
    AddressResolver,
    DestinationRefusedError,
    resolve_addresses,
)
from agent_workbench.adapters.research.guarded_fetch import (
    GuardedStreamClient,
    guarded_stream,
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

#: Narrower than what the HTTP client would advertise on its own, and the
#: narrowing is a memory bound rather than a preference. ``_body_of`` enforces
#: the ceiling against decompressed bytes, so it has to drive the decompressor
#: itself; ``zlib`` is what it can drive, and gzip is what ``zlib`` reads.
#: Leaving httpx to offer brotli and zstd as well would put back the case this
#: cannot bound -- a single small read of zstd expands without a documented
#: ceiling, so the first chunk is the whole attack.
_ACCEPT_ENCODING: Final[str] = "identity, gzip"

#: ``zlib`` reading gzip's framing rather than raw deflate. Spelled once here
#: because getting it wrong fails as "corrupt body", which reads like the
#: server's fault.
_GZIP_WBITS: Final[int] = 31

_PAGE_HEADERS: Final[dict[str, str]] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Accept-Encoding": _ACCEPT_ENCODING,
}

_DOCUMENT_HEADERS: Final[dict[str, str]] = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Encoding": _ACCEPT_ENCODING,
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

    http: GuardedStreamClient
    timeout_seconds: float = 20.0
    resolve: AddressResolver = resolve_addresses

    async def fetch_page(self, request: FetchPageRequest) -> str:
        async with self._open(request.url, _PAGE_HEADERS) as response:
            media_type = _media_type_of(response)
            if not any(media_type.startswith(kind) for kind in _TEXTUAL_TYPES):
                # Decided from the headers, so a PDF answered here costs the
                # headers and nothing else: the connection closes with the
                # file still on the far side.
                raise WebFetchError(
                    "not_text",
                    f"that URL serves {media_type}, which has no readable text; "
                    "use download_document to retrieve it as a file",
                )
            body = await _body_of(response, MAX_PAGE_BYTES, "the page")
            text = _decoded(body, response)
        # Extraction happens outside the block on purpose: it is work on bytes
        # this process already holds, and leaving it inside would hold a
        # connection open for the length of a parse.
        return page_text(text, limit=request.max_chars)

    async def download_document(self, request: DownloadRequest) -> Document:
        async with self._open(request.url, _DOCUMENT_HEADERS) as response:
            return Document(
                content=await _body_of(response, MAX_DOCUMENT_BYTES, "the document"),
                media_type=_media_type_of(response) or DEFAULT_MEDIA_TYPE,
            )

    @asynccontextmanager
    async def _open(self, url: str, headers: dict[str, str]) -> AsyncGenerator[Any]:
        """The response with its body still arriving, or a coded refusal.

        Opened rather than fetched. A ceiling can only refuse a body it has not
        already accepted, so the response has to reach the caller while it is
        still on the wire; a helper that returned a finished response would put
        the decision after the damage, which is where it used to be.
        """

        try:
            async with guarded_stream(
                self.http,
                url,
                headers=headers,
                timeout=self.timeout_seconds,
                resolve=self.resolve,
            ) as response:
                status = int(getattr(response, "status_code", 200))
                if status >= 400:
                    raise WebFetchError("http_error", f"the server answered {status}")
                yield response
        except WebFetchError:
            # Raised here or by the caller inside the block -- either way it
            # already carries the code a caller branches on, and relabelling it
            # "unreachable" below would claim the request failed when what
            # failed was this process's willingness to accept the answer.
            raise
        except DestinationRefusedError as error:
            raise WebFetchError("refused_destination", str(error)) from None
        except Exception as error:
            # Third-party exception text can carry proxy hostnames and local
            # paths. The type is enough for a caller to retry or give up. This
            # now also covers a connection that dies mid-body, which is a
            # failure of the request and reported as one.
            raise WebFetchError(
                "unreachable", f"the request failed ({type(error).__name__})"
            ) from None


def _media_type_of(response: Any) -> str:
    headers: Any = getattr(response, "headers", None)
    getter: Any = getattr(headers, "get", None) if headers is not None else None
    raw = str(getter("content-type") or "") if getter is not None else ""
    return raw.split(";")[0].strip().lower()


async def _body_of(response: Any, ceiling: int, subject: str) -> bytes:
    """The body, counted as it arrives and refused rather than cut when too large.

    ADR-027 §3.1 and ADR-028 §3.4 agree on this, and streaming does not soften
    it: a truncated document is a broken document, and the step that reads it
    next has no way to tell. So crossing the ceiling ends the read *and* the
    request. What has already been collected is dropped on the floor; it is
    never handed back as a short document.

    Counted chunk by chunk rather than measured afterwards. ``len(content)``
    can only be taken once the whole response is in memory, which means a
    server that answers 200 and then sends four gigabytes has already won by
    the time the ceiling is consulted -- the check was real and the protection
    was not.

    Counted on the *decompressed* side, and read from ``aiter_raw`` so that
    this module is the thing decompressing. Both halves are load-bearing and
    the reason is the gap the first streaming version still left open: a
    client that content-decodes for you hands over chunks that have already
    expanded, so the ceiling is consulted after the allocation again -- just
    one read later instead of one response later. A 64 KiB read of gzip can
    reach roughly 64 MiB that way, which is not a ceiling, it is a delay.
    Driving ``zlib`` here means the budget is passed *into* the decompressor,
    which stops at it. Peak cost is the ceiling plus one raw read.

    The refusal names the ceiling rather than the size, and that is the honest
    form of it: the exact size is the one thing a reader that stopped early
    genuinely does not know -- and against a compressed body nobody knows it,
    because the rest of the stream was never expanded.
    """

    expansion = _Expansion(_content_encoding_of(response), subject)
    collected: list[bytes] = []
    total = 0
    async for chunk in response.aiter_raw():
        # One byte of headroom: a budget of exactly what is left cannot tell
        # "filled the ceiling exactly" apart from "there is more after this".
        piece = expansion.push(chunk, ceiling - total + 1)
        total += len(piece)
        if total > ceiling or expansion.withheld:
            raise WebFetchError(
                "too_large",
                f"{subject} is larger than the {ceiling}-byte limit",
            )
        collected.append(piece)
    return b"".join(collected)


def _content_encoding_of(response: Any) -> str:
    headers: Any = getattr(response, "headers", None)
    getter: Any = getattr(headers, "get", None) if headers is not None else None
    return str(getter("content-encoding") or "").strip().lower() if getter else ""


class _Expansion:
    """Raw chunks in; at most a caller-set budget of decoded bytes out.

    The budget is the whole point, so it is an argument to every call rather
    than state: what is left of the ceiling changes with each chunk, and a
    decompressor told the wrong number is a decompressor that has already
    allocated the thing the ceiling exists to prevent.

    ``withheld`` reports that the decoder is sitting on input it could not
    expand inside the budget. That is a distinct fact from "the total went
    over": it is how a body whose *last* chunk explodes is caught, and reading
    it is what keeps a bounded read from quietly becoming a truncating one.
    """

    __slots__ = ("_decompressor", "_identity")

    def __init__(self, encoding: str, subject: str) -> None:
        self._identity = encoding in ("", "identity")
        self._decompressor = (
            None if self._identity else _gzip_decompressor(encoding, subject)
        )

    def push(self, raw: bytes, budget: int) -> bytes:
        if self._decompressor is None:
            # Cut to the budget rather than returned whole: the caller checks
            # the total straight after, so an over-budget slice becomes a
            # refusal on the next line. Nothing here is ever handed back.
            return raw[:budget]
        return self._decompressor.decompress(raw, budget)

    @property
    def withheld(self) -> bool:
        return bool(getattr(self._decompressor, "unconsumed_tail", b""))


def _gzip_decompressor(encoding: str, subject: str) -> Any:
    """The decoder for an encoding this module offered, or a refusal.

    Reached only when a server answered with a ``Content-Encoding`` other than
    identity. ``_ACCEPT_ENCODING`` asked for gzip and nothing else, so anything
    else here is a server ignoring what was offered -- and the refusal is not
    pedantry: brotli and zstd are exactly the encodings whose expansion this
    module cannot bound with ``zlib``, which is why they were never offered.
    """

    if encoding != "gzip":
        raise WebFetchError(
            "bad_encoding",
            f"{subject} came back {encoding}-encoded, which was not offered",
        )
    return zlib.decompressobj(_GZIP_WBITS)


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
