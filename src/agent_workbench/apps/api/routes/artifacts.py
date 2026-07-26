"""Reading artifacts back, tenant-scoped.

The store answers identically for "not yours" and "not there", and this route
does nothing to undo that: both become the same 404. A different status, a
different body or a different latency would each confirm that an object
somebody guessed at exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from agent_workbench.apps.api.state import dependencies_of

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}")
async def download(artifact_id: str, request: Request) -> StreamingResponse:
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    described = await dependencies.artifacts.head(
        tenant_id=principal.tenant_id,
        artifact_id=artifact_id,
    )
    content = await dependencies.artifacts.get(
        tenant_id=principal.tenant_id,
        artifact_id=artifact_id,
    )

    async def body() -> AsyncIterator[bytes]:
        yield content

    return StreamingResponse(
        body(),
        media_type=described.media_type,
        headers={
            "content-length": str(described.size_bytes),
            "x-artifact-sha256": described.sha256,
        },
    )


__all__ = ["router"]
