"""The layout view: the same document, drawn rather than transcribed.

Two things are under test and they are not the same thing. One is the route --
that it authorizes exactly as the text preview does, refuses on the same terms,
and separates "this host has no converter" from "this document is broken",
because the console shows a reader a different thing for each. The other is the
converter itself, which is only exercised where LibreOffice is actually
installed and is skipped, loudly, where it is not.

The skip matters. A test that quietly passed without a converter would report
that this works on a machine where it has never once run.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_workbench.adapters.documents import fidelity
from agent_workbench.apps.api.routes import artifacts
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.apps.word_mcp.contract import DocumentRequest, DocumentSection
from agent_workbench.apps.word_mcp.renderer import render_document
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.policies import PrincipalContext

OWNER = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}

DOCX_MEDIA_TYPE = artifacts.DOCX_MEDIA_TYPE

_HAS_SOFFICE = fidelity.find_soffice() is not None
_NEEDS_SOFFICE = pytest.mark.skipif(
    not _HAS_SOFFICE,
    reason="LibreOffice is not installed on this host, so nothing can be laid out",
)


def _document(title: str = "季度报告") -> bytes:
    return render_document(
        DocumentRequest(
            title=title,
            subtitle=None,
            sections=(
                DocumentSection(
                    heading="背景",
                    paragraphs=("第一段正文。", "第二段正文。"),
                    bullets=(),
                    table=None,
                ),
            ),
        )
    )


@pytest.fixture(autouse=True)
def _isolated_cache(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache directory per test, because the real one outlives the suite.

    ``_cache_dir`` is a fixed path under the system temp directory, which is
    the right answer in production and the wrong one here: the first run of
    this file would populate it and every run afterwards would measure a cache
    hit while believing it had measured a conversion. That is a test which
    passes for the wrong reason on every machine that has already run it once
    -- found exactly that way, by restoring a deliberate break and watching the
    suite stay red.
    """

    monkeypatch.setattr(fidelity, "_cache_dir", lambda: tmp_path)


class _Principals:
    def resolve(self, request: Any) -> PrincipalContext:
        return PrincipalContext(
            tenant_id=request.headers["x-tenant-id"],
            principal_id=request.headers["x-principal-id"],
        )


class _Artifacts:
    """Records the order it was asked, which is the authorization under test."""

    def __init__(self, content: bytes, *, media_type: str = DOCX_MEDIA_TYPE) -> None:
        self.content = content
        self.media_type = media_type
        self.calls: list[tuple[str, str, str]] = []

    async def head(
        self, *, tenant_id: str, artifact_id: str, principal_id: str
    ) -> ArtifactRef:
        self.calls.append(("head", tenant_id, principal_id))
        return ArtifactRef(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            kind="tool_result",
            media_type=self.media_type,
            size_bytes=len(self.content),
            sha256="a" * 64,
            filename="report.docx",
        )

    async def get(
        self, *, tenant_id: str, artifact_id: str, principal_id: str
    ) -> bytes:
        self.calls.append(("get", tenant_id, principal_id))
        return self.content


class _Dependencies:
    def __init__(self, store: _Artifacts) -> None:
        self.artifacts = store
        self.principals = _Principals()


def _app(store: _Artifacts) -> FastAPI:
    app = FastAPI()
    setattr(app.state, STATE_ATTRIBUTE, _Dependencies(store))
    app.include_router(artifacts.router)
    return app


def _get(app: FastAPI, path: str = "/v1/artifacts/art_1/pdf") -> Any:
    async def call() -> Any:
        transport = ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        async with AsyncClient(transport=transport, base_url="http://api.test") as c:
            return await c.get(path, headers=OWNER)

    return asyncio.run(call())


# --------------------------------------------------------------------------
# The deployment without a converter
# --------------------------------------------------------------------------


def test_a_host_without_libreoffice_answers_503_and_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503, because nothing here is broken and the text preview still works.

    This is the branch CI runs: no image this project builds today installs
    LibreOffice, so if this answered 500 the console would show every reader a
    failure for every document.
    """

    monkeypatch.setattr(fidelity, "find_soffice", lambda: None)
    store = _Artifacts(_document())

    response = _get(_app(store))

    assert response.status_code == 503
    assert "text preview" in response.json()["detail"]


def test_the_text_preview_still_answers_where_the_layout_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fidelity, "find_soffice", lambda: None)
    store = _Artifacts(_document())
    app = _app(store)

    assert _get(app).status_code == 503
    text = _get(app, "/v1/artifacts/art_1/preview")
    assert text.status_code == 200
    assert "第一段正文。" in text.json()["text"]


# --------------------------------------------------------------------------
# The refusals it shares with the text preview
# --------------------------------------------------------------------------


def test_anything_that_is_not_word_is_refused_before_conversion() -> None:
    store = _Artifacts(b"%PDF-1.7", media_type="application/pdf")

    response = _get(_app(store))

    assert response.status_code == 415
    # Refused on what the store said, without ever reading the bytes.
    assert [call[0] for call in store.calls] == ["head"]


def test_a_document_past_the_source_ceiling_is_refused_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifacts, "MAX_PREVIEW_SOURCE_BYTES", 16)
    store = _Artifacts(_document())

    response = _get(_app(store))

    assert response.status_code == 413
    assert [call[0] for call in store.calls] == ["head"]


def test_the_owner_is_established_before_any_bytes_are_read() -> None:
    """``head`` first, with the caller's principal, exactly as the text route.

    Two views of one document must not be two authorizations. This pins the
    order rather than the status, because the status for "not yours" is the
    store's to decide and is the same 404 for both routes.
    """

    monkeypatch_free_store = _Artifacts(_document())

    _get(_app(monkeypatch_free_store))

    assert monkeypatch_free_store.calls[0] == ("head", "tenant_a", "user_1")
    assert monkeypatch_free_store.calls[1] == ("get", "tenant_a", "user_1")


# --------------------------------------------------------------------------
# The conversion, where there is something to convert with
# --------------------------------------------------------------------------


@_NEEDS_SOFFICE
def test_a_word_document_comes_back_as_a_pdf() -> None:
    response = _get(_app(_Artifacts(_document())))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF")
    assert len(response.content) > 1000


@_NEEDS_SOFFICE
def test_the_second_reader_of_one_document_does_not_convert_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache is addressed by content, so the second request is free.

    Counted at the subprocess rather than at the response, because both
    requests answer 200 either way -- what this pins is that the second one did
    not spend two seconds of LibreOffice to do it.
    """

    content = _document(title="缓存验证 " + "x" * 40)
    conversions = 0
    # Reaching past the underscore on purpose: what needs pinning is that the
    # subprocess did not start a second time, and only the subprocess knows.
    real = fidelity._run_soffice  # pyright: ignore[reportPrivateUsage]

    async def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal conversions
        conversions += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(fidelity, "_run_soffice", counting)
    app = _app(_Artifacts(content))

    first = _get(app)
    second = _get(app)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.content == second.content
    assert conversions == 1


@_NEEDS_SOFFICE
def test_a_document_that_will_not_convert_is_not_the_hosts_fault() -> None:
    """422 rather than 503: a converter ran, so the host is not the problem."""

    store = _Artifacts(b"PK\x03\x04 not really a document")

    response = _get(_app(store))

    assert response.status_code == 422


# --------------------------------------------------------------------------
# The converter, directly
# --------------------------------------------------------------------------


def test_the_converter_reports_absence_rather_than_raising_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fidelity, "find_soffice", lambda: None)

    with pytest.raises(fidelity.LayoutUnavailableError):
        asyncio.run(fidelity.render_docx_to_pdf(_document()))


def test_the_cache_key_separates_two_formats_holding_the_same_bytes() -> None:
    same = b"the same bytes"

    assert fidelity.cache_key(same, ".docx") != fidelity.cache_key(same, ".rtf")
