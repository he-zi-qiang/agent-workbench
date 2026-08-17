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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from agent_workbench.adapters.documents.docx import (
    DocxTooLargeError,
    extract_docx_preview,
)
from agent_workbench.adapters.documents.fidelity import (
    LayoutUnavailableError,
    render_docx_to_pdf,
)
from agent_workbench.apps.api.downloads import content_disposition
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
    """A rendered document as text, for showing beside the run that made it.

    The counts are not decoration on the text: they are the only form in which
    "this preview is missing four pictures" reaches a reader. The extraction
    already knows (``adapters/documents/docx.py``), and until they crossed this
    boundary the knowledge stopped at the process that had it.

    Every field is required and none has a default, because a count that could
    be absent would arrive as "no pictures" -- which is the claim the preview is
    least entitled to make. The browser's mirror of this model is hand-written
    (``web/src/api/types.ts``), so the two move together.
    """

    text: str
    truncated: bool
    #: Of the whole document, as every count below it is. It used to be of the
    #: part the extraction's walk reached, and this model carried a note saying
    #: so rather than smoothing it over -- which was the right thing to do with
    #: a seam and the wrong thing to leave it as. A truncated preview whose
    #: table sat below the cut lost it from the text and from this number at
    #: once, and the panel renders "no non-zero counts" as the statement that
    #: nothing is missing. The seam is closed in the extraction
    #: (``adapters/documents/docx.py``); this field just reports it.
    table_count: int
    image_count: int
    header_count: int
    footer_count: int
    numbered_paragraph_count: int
    footnote_count: int
    flattened_paragraph_count: int


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
            "content-disposition": content_disposition(described.filename),
            "content-length": str(described.size_bytes),
            "x-artifact-sha256": described.sha256,
            # The stored media type is the whole answer. These bytes came from
            # a tool or an upload, so a browser second-guessing the label is a
            # browser promoting text/plain to something executable.
            "x-content-type-options": "nosniff",
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
        image_count=extracted.image_count,
        header_count=extracted.header_count,
        footer_count=extracted.footer_count,
        numbered_paragraph_count=extracted.numbered_paragraph_count,
        footnote_count=extracted.footnote_count,
        flattened_paragraph_count=extracted.flattened_paragraph_count,
    )


@router.get("/{artifact_id}/pdf")
async def preview_pdf(artifact_id: str, request: Request) -> Response:
    """The same .docx laid out, for the reader who needs to see the document.

    Everything before the conversion is the text preview's route, reached the
    same way and refusing on the same terms -- ``head`` first so an id that is
    not this principal's is a 404 before any bytes are read, 415 for anything
    that is not Word, 413 on the same source ceiling. Two views of one document
    must not be two authorizations, and the way to keep them one is to make the
    second copy the first line for line.

    The one status this route has and the text one does not is 503: a
    deployment without LibreOffice cannot lay anything out, and that is a fact
    about the host rather than about the document. It is separated from the
    422 below on purpose -- the console shows the text preview instead and says
    why, where a 422 would tell a reader their file is broken.
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
            detail="a layout preview is available for Word documents only",
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
        rendered = await render_docx_to_pdf(content)
    except LayoutUnavailableError as error:
        # Ordered before the handlers below because it is a RuntimeError and
        # they would swallow it. Nothing about this deployment is broken; it
        # simply has no converter, and the caller is told so as its own status.
        raise HTTPException(
            status_code=503,
            detail="this deployment cannot lay documents out; read the text preview",
        ) from error
    except DocxTooLargeError as error:
        raise HTTPException(
            status_code=413,
            detail="the document is too large to preview; download it instead",
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail="the document could not be laid out as a page",
        ) from error

    return Response(
        content=rendered,
        media_type="application/pdf",
        headers={
            # Addressed by content: the same document always converts to the
            # same PDF, so a cached copy can never be stale for this URL. The
            # artifact id is likewise immutable, which is what makes the pair
            # safe to cache privately for a long time.
            "cache-control": "private, max-age=86400, immutable",
        },
    )


__all__ = ["router"]
