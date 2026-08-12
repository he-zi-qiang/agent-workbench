"""The two read-only web tools (ADR-027 §3.1, WP14-02 PR-2).

Every refusal is paired with the accepted form of the same thing. The address
guard has its own suite; what is checked here is that this server actually goes
through it, that the two tools stay two, and that a ceiling is a refusal rather
than a quiet cut.

The HTTP client and the resolver are both stubs. A test that reached the real
web to prove a refusal would fail offline for reasons unrelated to what it
checks, and would stop testing anything the day the site it used changed.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import zlib
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final, cast

import pytest
from mcp import Client

from agent_workbench.apps.web_mcp.contract import (
    DEFAULT_PAGE_CHARS,
    DOWNLOAD_DOCUMENT_INPUT_SCHEMA,
    FETCH_PAGE_INPUT_SCHEMA,
    MAX_DOCUMENT_BYTES,
    MAX_PAGE_BYTES,
    MAX_PAGE_CHARS,
    MIN_PAGE_CHARS,
    WebRequestInputError,
    parse_fetch_page_request,
)
from agent_workbench.apps.web_mcp.fetcher import (
    DEFAULT_MEDIA_TYPE,
    WebFetcher,
    WebFetchError,
)
from agent_workbench.apps.web_mcp.server import (
    DOWNLOAD_DOCUMENT_TOOL,
    FETCH_PAGE_TOOL,
    create_server,
)
from agent_workbench.runtime.schema_validation import (
    SUPPORTED_KEYWORDS,
    assert_schema_supported,
)

PUBLIC_ADDRESS = "93.184.216.34"
PAGE_HTML = "<html><body><nav>menu</nav><p>The readable part.</p></body></html>"

#: How much of a body one stubbed read hands over. Only the size of the last
#: piece before a refusal depends on it, so it is small enough that "stopped at
#: the ceiling" is a tight statement rather than a generous one.
CHUNK_BYTES: Final[int] = 64 * 1024


class _Response:
    def __init__(
        self,
        content: bytes = b"",
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
        location: str | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.encoding = None
        self.headers: dict[str, str] = {"content-type": content_type}
        if location is not None:
            self.headers["location"] = location

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.content), CHUNK_BYTES):
            yield self.content[start : start + CHUNK_BYTES]

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        # Identical here because nothing this class serves is content-encoded,
        # which is the ordinary case and the reason both spellings can share
        # one body. ``_GzipBomb`` below is where the two come apart.
        async for chunk in self.aiter_bytes():
            yield chunk


class _Flood:
    """A server that answers 200 and then sends far more than anyone asked for.

    ``delivered`` is why this class exists rather than a big ``_Response``: it
    counts the bytes that actually crossed into this process, which is the only
    way to tell a reader that stops at the ceiling from one that stops when the
    server does. Both of them refuse; only one of them refuses in time.

    ``content`` is kept, and counts too, because the whole-response path still
    exists on the real client. Without it a fetcher that went back to reading
    ``response.content`` would fail with ``AttributeError`` -- a red bar for a
    reason that has nothing to do with the ceiling, which is no evidence at all.
    """

    def __init__(self, total: int, content_type: str) -> None:
        self.total = total
        self.status_code = 200
        self.encoding = None
        self.headers: dict[str, str] = {"content-type": content_type}
        self.delivered = 0

    @property
    def content(self) -> bytes:
        self.delivered = self.total
        return b"x" * self.total

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        remaining = self.total
        while remaining > 0:
            size = min(CHUNK_BYTES, remaining)
            remaining -= size
            self.delivered += size
            yield b"x" * size

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        async for chunk in self.aiter_bytes():
            yield chunk


class _GzipBomb:
    """A small compressed body that expands into a large one.

    The two iterators are deliberately different, and faithfully so: this is
    what ``httpx`` does. ``aiter_raw`` yields what crossed the wire, and
    ``aiter_bytes`` yields what that becomes after the client content-decodes
    it -- in one piece per raw read, because that is how a streaming
    decompressor works. Which of the two a reader chooses is therefore not a
    style question: taking ``aiter_bytes`` means the expansion has already
    been paid for by the time any ceiling is consulted.

    ``expanded`` counts the decoded bytes this stub was asked to produce. It
    is the measurement that matters, because both a bounded and an unbounded
    reader raise ``too_large`` here -- they differ only in what the process
    had to hold first.
    """

    def __init__(self, decompressed: int, content_type: str) -> None:
        self.plain = b"\0" * decompressed
        # gzip framing, not zlib's: ``Content-Encoding: gzip`` names the
        # former, and a stub that produced the latter would test the reader's
        # tolerance for a malformed body instead of its bound.
        self.raw = gzip.compress(self.plain, 9)
        self.status_code = 200
        self.encoding = None
        self.headers: dict[str, str] = {
            "content-type": content_type,
            "content-encoding": "gzip",
        }
        self.delivered = 0
        self.expanded = 0

    @property
    def content(self) -> bytes:
        self.expanded = len(self.plain)
        return self.plain

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.raw), CHUNK_BYTES):
            chunk = self.raw[start : start + CHUNK_BYTES]
            self.delivered += len(chunk)
            yield chunk

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        # One raw read in, its whole expansion out. No chunking of the result:
        # inventing one would hide the very allocation this stub exists to
        # expose.
        decompressor = zlib.decompressobj(31)
        async for chunk in self.aiter_raw():
            piece = decompressor.decompress(chunk)
            if piece:
                self.expanded += len(piece)
                yield piece


@dataclass
class _StubHttp:
    pages: dict[str, _Response | _Flood | _GzipBomb] = field(default_factory=dict)
    default: _Response | Exception | None = None
    fetched: list[str] = field(default_factory=list)
    delegated_redirects: list[bool] = field(default_factory=list)

    async def _answer(self, url: str, *, follow_redirects: bool) -> Any:
        """What this server has at ``url``, recorded the same way either call.

        Shared so a redirect chain is logged identically whichever half of the
        Protocol drove it -- otherwise the SSRF tests below would still pass
        while checking a path the fetcher no longer takes.
        """

        self.fetched.append(url)
        self.delegated_redirects.append(follow_redirects)
        found = self.pages.get(url, self.default)
        if isinstance(found, Exception):
            raise found
        return found if found is not None else _Response(PAGE_HTML.encode("utf-8"))

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
        timeout: float,
    ) -> Any:
        """A client that buffers, because ``httpx.AsyncClient.get`` is one.

        Touching ``content`` here is the whole point rather than an oversight:
        the real call does not return until the body has been read, so a stub
        that handed the response back untouched would make a fetcher that
        buffers indistinguishable from one that streams, and every assertion
        about *when* a limit bites would pass either way.
        """

        del headers, timeout
        response = await self._answer(url, follow_redirects=follow_redirects)
        _ = response.content
        return response

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
        timeout: float,
    ) -> AsyncGenerator[Any]:
        """The streaming half: headers now, body only if someone asks for it."""

        del method, headers, timeout
        yield await self._answer(url, follow_redirects=follow_redirects)


_RESOLVED: dict[str, tuple[str, ...]] = {
    "internal.example": ("10.0.0.5",),
    "metadata.example": ("169.254.169.254",),
}


async def _resolve(host: str) -> tuple[str, ...]:
    return _RESOLVED.get(host, (PUBLIC_ADDRESS,))


def _fetcher(http: _StubHttp | None = None) -> WebFetcher:
    return WebFetcher(http=http or _StubHttp(), timeout_seconds=5.0, resolve=_resolve)


def _call(fetcher: WebFetcher, name: str, arguments: dict[str, Any]) -> Any:
    async def scenario() -> Any:
        async with Client(
            create_server(fetcher), cache=None, raise_exceptions=True
        ) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(scenario())


def _text(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def test_both_schemas_stay_inside_the_validator_this_project_has() -> None:
    """A tool this repository ships must pass the gate a third-party one does.

    It does not merely fail politely otherwise: an unsupported schema is
    dropped from the registry at Worker startup, so the capability would go
    missing rather than misbehave.
    """

    assert_schema_supported(FETCH_PAGE_INPUT_SCHEMA, origin="fetch_page")
    assert_schema_supported(DOWNLOAD_DOCUMENT_INPUT_SCHEMA, origin="download_document")
    assert len(SUPPORTED_KEYWORDS) == 17


def test_neither_contract_names_a_path_tenant_owner_or_artifact() -> None:
    for schema in (FETCH_PAGE_INPUT_SCHEMA, DOWNLOAD_DOCUMENT_INPUT_SCHEMA):
        properties = schema["properties"]
        assert isinstance(properties, dict)
        assert set(properties) <= {"url", "max_chars"}


def test_the_protocol_lists_both_tools() -> None:
    """One real `tools/list` over the official SDK's in-memory transport."""

    async def scenario() -> Any:
        async with Client(
            create_server(_fetcher()), cache=None, raise_exceptions=True
        ) as client:
            return await client.list_tools()

    tools = asyncio.run(scenario()).tools

    assert sorted(tool.name for tool in tools) == [
        DOWNLOAD_DOCUMENT_TOOL,
        FETCH_PAGE_TOOL,
    ]
    for tool in tools:
        annotations = tool.annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        # Honest rather than flattering: what a URL answers is not this
        # process's to predict, so the world it reaches is not a closed one.
        assert annotations.open_world_hint is True


def test_a_page_comes_back_as_readable_text_not_markup() -> None:
    result = _call(
        _fetcher(), FETCH_PAGE_TOOL, {"url": "https://research.example/article"}
    )

    assert result.is_error is False
    assert "The readable part." in _text(result)
    assert "<p>" not in _text(result)


def test_a_non_text_page_is_refused_and_names_the_other_tool() -> None:
    """The mistake ADR-027 §2 recorded: a PDF through the HTML extractor
    becomes garbled text that reads like a successful read."""

    http = _StubHttp(
        pages={
            "https://research.example/paper.pdf": _Response(
                b"%PDF-1.7\nbinary", content_type="application/pdf"
            )
        }
    )

    refused = _call(
        _fetcher(http), FETCH_PAGE_TOOL, {"url": "https://research.example/paper.pdf"}
    )

    assert refused.is_error is True
    assert "download_document" in _text(refused)

    # Control: the same bytes through the other tool come back untouched.
    downloaded = _call(
        _fetcher(http),
        DOWNLOAD_DOCUMENT_TOOL,
        {"url": "https://research.example/paper.pdf"},
    )
    assert downloaded.is_error is False
    resource = cast(Any, downloaded.content[0]).resource
    assert base64.b64decode(resource.blob) == b"%PDF-1.7\nbinary"
    assert resource.mime_type == "application/pdf"


def test_a_non_text_page_is_refused_before_its_bytes_are_read() -> None:
    """The refusal above costs the headers and nothing else.

    Content type is knowable from the response line; the file is not needed to
    decide it. A fetcher that read first and judged afterwards would download
    a whole video to say it is not readable text, which is the same mistake as
    a ceiling applied after the fact, wearing a different hat.
    """

    video = _Flood(total=MAX_DOCUMENT_BYTES * 8, content_type="video/mp4")
    http = _StubHttp(pages={"https://research.example/clip.mp4": video})

    refused = _call(
        _fetcher(http), FETCH_PAGE_TOOL, {"url": "https://research.example/clip.mp4"}
    )

    assert refused.is_error is True
    assert "download_document" in _text(refused)
    assert video.delivered == 0


def test_a_document_without_a_content_type_still_gets_one() -> None:
    http = _StubHttp(
        pages={"https://research.example/f": _Response(b"bytes", content_type="")}
    )

    result = _call(
        _fetcher(http), DOWNLOAD_DOCUMENT_TOOL, {"url": "https://research.example/f"}
    )

    assert cast(Any, result.content[0]).resource.mime_type == DEFAULT_MEDIA_TYPE


@pytest.mark.parametrize(
    "url",
    [
        "http://internal.example/admin",
        "http://metadata.example/latest/meta-data/",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/",
        "http://[::1]/",
    ],
    ids=str,
)
@pytest.mark.parametrize("tool", [FETCH_PAGE_TOOL, DOWNLOAD_DOCUMENT_TOOL])
def test_a_private_destination_is_refused_before_any_request(
    tool: str, url: str
) -> None:
    http = _StubHttp()

    result = _call(_fetcher(http), tool, {"url": url})

    assert result.is_error is True
    # Not merely an error result: nothing was sent.
    assert http.fetched == []


@pytest.mark.parametrize("tool", [FETCH_PAGE_TOOL, DOWNLOAD_DOCUMENT_TOOL])
def test_a_public_destination_is_requested(tool: str) -> None:
    """The control group. A server that refused everything would satisfy every
    refusal above on its own."""

    http = _StubHttp()

    result = _call(_fetcher(http), tool, {"url": "https://research.example/doc"})

    assert result.is_error is False
    assert http.fetched == ["https://research.example/doc"]


def test_a_redirect_into_a_private_address_is_refused_at_the_second_hop() -> None:
    http = _StubHttp(
        pages={
            "https://research.example/go": _Response(
                b"", status_code=302, location="http://169.254.169.254/latest/"
            )
        }
    )

    result = _call(
        _fetcher(http), FETCH_PAGE_TOOL, {"url": "https://research.example/go"}
    )

    assert result.is_error is True
    assert http.fetched == ["https://research.example/go"]
    # The line that makes the one above mean something: "only the first URL was
    # requested" is equally true of a server that let the client follow the
    # redirect itself, and in that case the client would have reached the
    # metadata address without this stub ever seeing the second URL.
    assert http.delegated_redirects == [False]


def test_a_redirect_to_a_public_address_is_followed() -> None:
    http = _StubHttp(
        pages={
            "https://research.example/go": _Response(
                b"", status_code=301, location="/final"
            ),
            "https://research.example/final": _Response(PAGE_HTML.encode("utf-8")),
        }
    )

    result = _call(
        _fetcher(http), FETCH_PAGE_TOOL, {"url": "https://research.example/go"}
    )

    assert result.is_error is False
    assert http.fetched == [
        "https://research.example/go",
        "https://research.example/final",
    ]
    assert "The readable part." in _text(result)


def test_an_oversized_document_is_refused_rather_than_truncated() -> None:
    """A truncated document is a broken document, and the step that reads it
    next has no way to tell (ADR-027 §3.1, ADR-028 §3.4)."""

    over = _StubHttp(
        pages={
            "https://research.example/big": _Response(
                b"x" * (MAX_DOCUMENT_BYTES + 1), content_type="application/pdf"
            )
        }
    )

    refused = _call(
        _fetcher(over), DOWNLOAD_DOCUMENT_TOOL, {"url": "https://research.example/big"}
    )
    assert refused.is_error is True
    assert "too_large" in _text(refused)

    at_limit = _StubHttp(
        pages={
            "https://research.example/big": _Response(
                b"x" * MAX_DOCUMENT_BYTES, content_type="application/pdf"
            )
        }
    )
    accepted = _call(
        _fetcher(at_limit),
        DOWNLOAD_DOCUMENT_TOOL,
        {"url": "https://research.example/big"},
    )
    assert accepted.is_error is False
    resource = cast(Any, accepted.content[0]).resource
    assert len(base64.b64decode(resource.blob)) == MAX_DOCUMENT_BYTES


@pytest.mark.parametrize(
    ("tool", "ceiling", "content_type"),
    [
        (FETCH_PAGE_TOOL, MAX_PAGE_BYTES, "text/html"),
        (DOWNLOAD_DOCUMENT_TOOL, MAX_DOCUMENT_BYTES, "application/pdf"),
    ],
    ids=[FETCH_PAGE_TOOL, DOWNLOAD_DOCUMENT_TOOL],
)
def test_a_body_past_the_ceiling_stops_arriving_instead_of_being_measured_after(
    tool: str, ceiling: int, content_type: str
) -> None:
    """The refusal has to happen while the body is still on the wire.

    A ceiling read off a finished response is not a defence: a server that
    claims 200 and then sends gigabytes has already spent this process's memory
    by the time the size is known, and the refusal it eventually produces is a
    report on a machine that has already fallen over. The assertion that
    distinguishes the two is not the error code -- both versions raise
    ``too_large`` -- it is how many bytes the server got to hand over.
    """

    flood = _Flood(total=ceiling * 8, content_type=content_type)
    http = _StubHttp(pages={"https://research.example/flood": flood})

    refused = _call(_fetcher(http), tool, {"url": "https://research.example/flood"})

    assert refused.is_error is True
    assert "too_large" in _text(refused)
    assert flood.delivered <= ceiling + CHUNK_BYTES
    # Control: it stopped *at* the ceiling, not before reading anything. A
    # fetcher that refused every large content-length up front would satisfy
    # the line above while never having read a byte.
    assert flood.delivered > ceiling

    # The message can no longer state a size, so it must not pretend to. The
    # old wording read "is 33554432 bytes, above the ..." -- a number a reader
    # that stopped early never saw.
    assert str(ceiling) in _text(refused)
    assert str(flood.total) not in _text(refused)


@pytest.mark.parametrize(
    ("tool", "ceiling", "content_type"),
    [
        (FETCH_PAGE_TOOL, MAX_PAGE_BYTES, "text/html"),
        (DOWNLOAD_DOCUMENT_TOOL, MAX_DOCUMENT_BYTES, "application/pdf"),
    ],
    ids=[FETCH_PAGE_TOOL, DOWNLOAD_DOCUMENT_TOOL],
)
def test_a_compressed_body_cannot_spend_the_memory_the_ceiling_refuses(
    tool: str, ceiling: int, content_type: str
) -> None:
    """The ceiling has to bind what the body *becomes*, not what it arrives as.

    Streaming alone does not give this. A reader that streams but lets the
    HTTP client content-decode for it is handed one expansion per raw read,
    already allocated, and only then counts -- so 64 KiB of gzip buys the
    sender tens of megabytes before a single comparison happens. The ceiling
    is still enforced and the memory is still spent, which is the shape of a
    check that runs after the damage rather than instead of it.

    The error code proves nothing here: both readers refuse. ``expanded`` is
    the assertion, because it counts the decoded bytes the sender got this
    process to materialise. ``delivered`` -- the measurement that separates
    the two readers in the uncompressed case above -- says nothing here, and
    deliberately is not asserted on: a bomb's whole point is that the wire
    side is already small, so both readers read all of it and only one of them
    pays for what it becomes.
    """

    bomb = _GzipBomb(decompressed=ceiling * 2, content_type=content_type)
    # The premise, stated as an assertion so this fails loudly rather than
    # passing vacuously if zlib's ratio on zeros ever changes: a body small
    # enough that no wire-side limit would ever have looked at it twice.
    assert len(bomb.raw) < CHUNK_BYTES

    http = _StubHttp(pages={"https://research.example/bomb": bomb})

    refused = _call(_fetcher(http), tool, {"url": "https://research.example/bomb"})

    assert refused.is_error is True
    assert "too_large" in _text(refused)
    # Nothing was decoded on this process's behalf: the fetcher drove the
    # decompressor itself, under the budget the ceiling leaves it.
    assert bomb.expanded == 0


def test_a_compressed_body_under_the_ceiling_is_still_read() -> None:
    """The control: bounding the expansion must not mean refusing compression.

    Without this, ``test_...cannot_spend_the_memory...`` is satisfiable by a
    reader that rejects every ``Content-Encoding`` it sees, which would buy
    the bound by breaking most of the web.
    """

    page = _GzipBomb(decompressed=0, content_type="text/html; charset=utf-8")
    page.plain = PAGE_HTML.encode("utf-8")
    page.raw = gzip.compress(page.plain, 9)
    http = _StubHttp(pages={"https://research.example/zipped": page})

    result = _call(
        _fetcher(http), FETCH_PAGE_TOOL, {"url": "https://research.example/zipped"}
    )

    assert result.is_error is not True
    assert "The readable part." in _text(result)
    assert page.expanded == 0


def test_an_encoding_that_was_never_offered_is_refused_by_name() -> None:
    """Brotli and zstd are the ones this module cannot bound, so it asks for
    neither -- and a server that sends one anyway is answering a request that
    was not made. Refusing is the only honest end: decoding it would need the
    unbounded expansion the ceiling above exists to prevent, and passing the
    bytes through undecoded would hand the caller compressed data labelled as
    a page.
    """

    encoded = _Response(content=b"\x1b\x2a\x00\x00", content_type="text/html")
    encoded.headers["content-encoding"] = "br"
    http = _StubHttp(pages={"https://research.example/brotli": encoded})

    refused = _call(
        _fetcher(http), FETCH_PAGE_TOOL, {"url": "https://research.example/brotli"}
    )

    assert refused.is_error is True
    assert "bad_encoding" in _text(refused)
    assert "br" in _text(refused)


def test_max_chars_bounds_the_text_and_defaults_when_absent() -> None:
    body = "<p>" + ("政" * 3000) + "</p>"
    http = _StubHttp(
        pages={
            "https://research.example/long": _Response(
                f"<html><body>{body}</body></html>".encode()
            )
        }
    )

    bounded = _call(
        _fetcher(http),
        FETCH_PAGE_TOOL,
        {"url": "https://research.example/long", "max_chars": MIN_PAGE_CHARS},
    )
    assert len(_text(bounded)) == MIN_PAGE_CHARS

    whole = _call(
        _fetcher(http), FETCH_PAGE_TOOL, {"url": "https://research.example/long"}
    )
    assert len(_text(whole)) == 3000
    assert parse_fetch_page_request({"url": "https://a.example/"}).max_chars == (
        DEFAULT_PAGE_CHARS
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"url": "ftp://research.example/doc"},
        {"url": "file:///etc/passwd"},
        {"url": "https://research.example/", "max_chars": MAX_PAGE_CHARS + 1},
        {"url": "https://research.example/", "max_chars": MIN_PAGE_CHARS - 1},
        {"url": "https://research.example/", "follow_redirects": True},
        {},
    ],
    ids=str,
)
def test_an_argument_object_outside_the_contract_is_refused(
    arguments: dict[str, Any],
) -> None:
    http = _StubHttp()

    result = _call(_fetcher(http), FETCH_PAGE_TOOL, arguments)

    assert result.is_error is True
    assert http.fetched == []


def test_an_upstream_error_status_is_reported_as_one() -> None:
    http = _StubHttp(
        pages={"https://research.example/gone": _Response(b"", status_code=404)}
    )

    result = _call(
        _fetcher(http), FETCH_PAGE_TOOL, {"url": "https://research.example/gone"}
    )

    assert result.is_error is True
    assert "404" in _text(result)


def test_a_transport_failure_does_not_leak_its_own_message() -> None:
    """Exception text from a client can carry proxy hostnames and local paths.

    The type is what a caller can act on; the rest reaches the model's context
    and an operator's log without adding anything they can use.
    """

    http = _StubHttp(default=OSError("proxy at /Users/someone/.config refused"))

    result = _call(
        _fetcher(http), FETCH_PAGE_TOOL, {"url": "https://research.example/doc"}
    )

    assert result.is_error is True
    assert "/Users/someone" not in _text(result)
    assert "OSError" in _text(result)


def test_an_unknown_tool_is_refused() -> None:
    result = _call(_fetcher(), "render_document", {})

    assert result.is_error is True


def test_a_declared_charset_that_lies_still_reads_a_chinese_page() -> None:
    """Kept from the search adapter because the cause has not changed.

    A page that declares nothing decodes as UTF-8 into replacement characters
    rather than into an error, so it looks read and says nothing.
    """

    body = "<html><body><p>今天 晴 23°/36°</p></body></html>".encode("gb18030")
    http = _StubHttp(
        pages={"https://research.example/cn": _Response(body, content_type="text/html")}
    )

    result = _call(
        _fetcher(http), FETCH_PAGE_TOOL, {"url": "https://research.example/cn"}
    )

    assert "今天 晴 23°/36°" in _text(result)


def test_the_fetcher_raises_a_coded_error_the_server_turns_into_a_result() -> None:
    """The code is the part a caller can branch on, so it has to survive.

    A refused destination is a permanent answer about that URL; an oversized
    document is an answer about the ceiling; a 404 is an answer about the page.
    """

    fetcher = _fetcher()

    async def scenario() -> str:
        try:
            await fetcher.fetch_page(
                parse_fetch_page_request({"url": "http://internal.example/admin"})
            )
        except WebFetchError as error:
            return error.code
        return "no error"

    assert asyncio.run(scenario()) == "refused_destination"

    with pytest.raises(WebRequestInputError):
        parse_fetch_page_request({"url": "gopher://research.example/"})
