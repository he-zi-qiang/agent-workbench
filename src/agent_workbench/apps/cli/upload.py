"""HTTP control-plane command for putting a document into the index.

Three calls, because uploading is three decisions and the server makes all of
them. The client declares what it is about to send and gets an intent; it
transfers the bytes; it asks for them to become a document version. Nothing
here shortcuts that -- a CLI that wrote to the artifact store or the document
tables directly would be a second way around upload authorization, and it is
exactly the convenient path somebody would reach for later.

The digest is computed here and sent *before* the bytes. That is what makes the
transfer checkable: the server compares what arrived against what was promised,
so a truncated or altered upload fails at completion rather than becoming a
document whose text nobody can account for.

Like the Task commands, this renders no server error body. A response body can
carry details that belong in server logs rather than a terminal transcript.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any, TextIO

import httpx

# Same package, and deliberately the same implementations: a second renderer
# or a second response reader would eventually disagree with the Task
# commands about what a caller is allowed to see.
from agent_workbench.apps.cli.task import (
    DEFAULT_API_URL,
    DEFAULT_TIMEOUT_SECONDS,
    HttpClientFactory,
    TaskCliError,
    default_http_client,
    render_error,
    render_result,
    response_json,
)

#: What a file is declared as when its name says nothing. The server decides
#: whether it can parse it; guessing something more specific here would be this
#: CLI asserting a fact about bytes it only forwarded.
FALLBACK_MEDIA_TYPE = "application/octet-stream"


def run_upload(
    args: Any,
    stream: TextIO,
    *,
    http_client_factory: HttpClientFactory = default_http_client,
) -> int:
    """Transfer one file and complete it into a document version."""

    source = Path(args.path)
    try:
        content = source.read_bytes()
    except OSError:
        # The path is the caller's own argument, so echoing it back discloses
        # nothing they did not type.
        render_result(
            {"error": "unreadable_file", "path": str(source)},
            as_json=args.json,
            stream=stream,
        )
        return 1

    media_type = args.media_type or mimetypes.guess_type(source.name)[0]
    headers = {"x-tenant-id": args.tenant_id, "x-principal-id": args.principal_id}

    client = http_client_factory(args.api_url, args.timeout_seconds)
    try:
        with client:
            payload = _upload(
                client,
                headers=headers,
                content=content,
                filename=source.name,
                media_type=media_type or FALLBACK_MEDIA_TYPE,
                document_id=args.document_id,
                knowledge_base_id=args.knowledge_base_id,
                granted_principals=tuple(args.grant or ()),
            )
    except TaskCliError as error:
        return render_error(error, stream, as_json=args.json)
    except httpx.HTTPError:
        return render_error(
            TaskCliError(code="transport_error"), stream, as_json=args.json
        )

    render_result(payload, as_json=args.json, stream=stream)
    return 0


def _upload(
    client: httpx.Client,
    *,
    headers: dict[str, str],
    content: bytes,
    filename: str,
    media_type: str,
    document_id: str,
    knowledge_base_id: str,
    granted_principals: tuple[str, ...],
) -> dict[str, Any]:
    """Declare, transfer, complete -- in that order and no other."""

    intent = response_json(
        client.post(
            "/v1/uploads",
            headers=headers,
            json={
                "declared_size_bytes": len(content),
                # Promised before the bytes move, so the server can refuse a
                # transfer that does not match what it was told to expect.
                "declared_sha256": hashlib.sha256(content).hexdigest(),
                "media_type": media_type,
                "filename": filename,
            },
        )
    )
    stored = response_json(
        client.put(
            str(intent["content_path"]),
            headers={**headers, "content-type": media_type},
            content=content,
        )
    )
    version = response_json(
        client.post(
            f"/v1/uploads/{intent['upload_id']}/complete",
            headers=headers,
            json={
                "artifact_id": stored["artifact_id"],
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "granted_principals": list(granted_principals),
            },
        )
    )
    return {
        "upload_id": intent["upload_id"],
        "artifact_id": stored["artifact_id"],
        "size_bytes": stored["size_bytes"],
        "sha256": stored["sha256"],
        "document_id": version.get("document_id", document_id),
        "document_version": version.get("source_revision"),
    }


__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "FALLBACK_MEDIA_TYPE",
    "run_upload",
]
