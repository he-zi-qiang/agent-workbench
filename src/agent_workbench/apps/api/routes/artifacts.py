"""Reading artifacts back, scoped to the principal that stored them.

The store answers identically for "not yours", "not your tenant's" and "not
there", and this route does nothing to undo that: all become the same 404. A
different status, a different body or a different latency would each confirm
that an object somebody guessed at exists.

The body is streamed in pieces. It used to read the whole object -- up to the
100 MiB ceiling -- and hand a single ``bytes`` to a ``StreamingResponse``,
which is a streaming response in name only: the peak memory was one whole
artifact per concurrent download, and a slow client held all of it.

Both store calls are made before the response begins. A refusal has to happen
while a status code can still change; discovering it mid-body would mean a 200
that stops partway, which is indistinguishable from a network failure.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent_workbench.apps.api.docx_preview import (
    DocxTooLargeError,
    extract_docx_preview,
)
from agent_workbench.apps.api.state import dependencies_of

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])

DOCX_MEDIA_TYPE: Final[str] = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

#: Refused above this, rather than streamed into a parser. A .docx is a zip, so
#: the in-memory cost of reading one is its *expanded* size -- and the 100 MiB
#: artifact ceiling is a ceiling on the compressed bytes. Preview is a
#: convenience; the download has no such limit and is what a large document is
#: for.
MAX_PREVIEW_SOURCE_BYTES: Final[int] = 20 * 1024 * 1024


class DocumentPreview(BaseModel):
    """A rendered document as text, for showing beside the run that made it."""

    text: str
    truncated: bool
    table_count: int


def _content_disposition(filename: str | None) -> str:
    """Encode display metadata without letting it become response syntax."""

    resolved = filename or "artifact"
    fallback = "".join(
        character
        if character.isascii()
        and (character.isalnum() or character in {".", "_", "-", " "})
        else "_"
        for character in resolved
    ).strip(" .")
    if not fallback or fallback in {".", ".."}:
        fallback = "artifact"
    encoded = quote(resolved, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


@router.get("/{artifact_id}")
async def download(artifact_id: str, request: Request) -> StreamingResponse:
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    described = await dependencies.artifacts.head(
        tenant_id=principal.tenant_id,
        artifact_id=artifact_id,
        principal_id=principal.principal_id,
    )
    chunks = dependencies.artifacts.iter_chunks(
        tenant_id=principal.tenant_id,
        artifact_id=artifact_id,
        principal_id=principal.principal_id,
    )

    return StreamingResponse(
        chunks,
        media_type=described.media_type,
        headers={
            "content-disposition": _content_disposition(described.filename),
            "content-length": str(described.size_bytes),
            "x-artifact-sha256": described.sha256,
        },
    )


@router.get("/{artifact_id}/preview")
async def preview(artifact_id: str, request: Request) -> DocumentPreview:
    """A .docx as text, for the panel beside the run.

    The same authorization as the download, reached the same way: ``head``
    first, so an id that is not this principal's is a 404 before any bytes are
    read, and unknown/wrong-tenant/wrong-principal stay indistinguishable.

    Only .docx. Text and JSON are already previewable by fetching them, so a
    general "preview anything" endpoint would be a second path to bytes the
    client can read directly -- one more place for the authorization to be
    written slightly differently.
    """

    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    described = await dependencies.artifacts.head(
        tenant_id=principal.tenant_id,
        artifact_id=artifact_id,
        principal_id=principal.principal_id,
    )
    if described.media_type != DOCX_MEDIA_TYPE:
        raise HTTPException(
            status_code=415,
            detail="preview is available for Word documents only",
        )
    if described.size_bytes > MAX_PREVIEW_SOURCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="the document is too large to preview; download it instead",
        )

    content = await dependencies.artifacts.get(
        tenant_id=principal.tenant_id,
        artifact_id=artifact_id,
        principal_id=principal.principal_id,
    )
    try:
        extracted = extract_docx_preview(content)
    except DocxTooLargeError as error:
        # The same answer as the compressed-size ceiling above, because it is
        # the same refusal reached later: this one is about what the package
        # weighs *opened*, which the stored size does not bound. The reason is
        # deliberately not echoed back -- it would describe the archive's
        # internals to whoever supplied it.
        raise HTTPException(
            status_code=413,
            detail="the document is too large to preview; download it instead",
        ) from error
    except Exception as error:
        # A stored artifact that will not parse is a fact about the file, not a
        # fault the caller can act on beyond downloading it. Deliberately not a
        # 500: nothing here is broken, and the download still works.
        raise HTTPException(
            status_code=422,
            detail="the document could not be read as Word content",
        ) from error

    return DocumentPreview(
        text=extracted.text,
        truncated=extracted.truncated,
        table_count=extracted.table_count,
    )


__all__ = ["router"]
