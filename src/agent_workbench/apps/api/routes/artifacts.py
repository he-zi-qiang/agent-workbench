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
            "content-length": str(described.size_bytes),
            "x-artifact-sha256": described.sha256,
        },
    )


__all__ = ["router"]
