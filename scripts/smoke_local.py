"""Drive a running local workbench and report what actually happened.

This is a walkthrough, not a test. It talks to the HTTP API the way any client
would -- no database handle, no in-process shortcut -- so what it proves is that
the deployed surface works, rather than that the code does. The suite already
covers the code.

Two slices run, and they are the two that need no model provider:

* a document is uploaded, becomes a version, and is indexed by the ingestion
  worker with real BGE-M3 vectors;
* a Task is submitted, claimed by the worker, run through the fixed LangGraph
  and settled, with its whole lifecycle readable from the event timeline.

Chat is deliberately absent. Answering requires a provider, and a process
without one refuses to start rather than serving a route that fails every call.

Everything it reports is read back from the running system. Where it cannot
confirm something -- an ingestion worker that is not running, a Task nobody
claimed -- it says so and exits non-zero, because a walkthrough that printed
"OK" for work nobody did would be worse than no walkthrough.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from typing import Any

import httpx

#: How long to wait for a worker to pick something up. Generous, and measured
#: rather than guessed: on this project's own machine the ingestion worker takes
#: about two minutes per document, because it loads BGE-M3's dense and sparse
#: heads around each batch rather than once per process. A smoke run that timed
#: out at ninety seconds reported "never indexed" for work that was in progress
#: and completed a minute later, which sent the reader to look at the wrong
#: thing entirely.
WORKER_TIMEOUT_SECONDS = 300.0
POLL_SECONDS = 2.0

SAMPLE = """Qdrant performs one dense and sparse fusion per query.

Reciprocal rank fusion runs inside the database rather than in the application,
so both arms propose a full candidate set and the ranking is decided once. A
retrieval that fused in two places would be two retrievers, and the one with
less attention would be the one that drifted.

Deletions are tombstoned and reconciled rather than removed in place.
"""


def _headers(args: argparse.Namespace) -> dict[str, str]:
    return {"x-tenant-id": args.tenant_id, "x-principal-id": args.principal_id}


def _step(label: str) -> None:
    print(f"\n\033[1m{label}\033[0m", flush=True)


def _fact(label: str, value: object) -> None:
    print(f"  {label:<24} {value}", flush=True)


def _fail(reason: str, hint: str) -> int:
    print(f"\n\033[31mstopped\033[0m {reason}\n  try: {hint}\n", flush=True)
    return 1


def _upload(client: httpx.Client, args: argparse.Namespace) -> dict[str, Any]:
    """The same three calls `agent-cli upload` makes."""

    import hashlib

    document_id = f"doc_smoke_{uuid.uuid4().hex[:16]}"
    content = SAMPLE.encode("utf-8")
    intent = client.post(
        "/v1/uploads",
        headers=_headers(args),
        json={
            "declared_size_bytes": len(content),
            "declared_sha256": hashlib.sha256(content).hexdigest(),
            "media_type": "text/plain",
            "filename": "fusion-note.txt",
        },
    )
    intent.raise_for_status()
    opened = intent.json()

    stored = client.put(
        opened["content_path"],
        headers={**_headers(args), "content-type": "text/plain"},
        content=content,
    )
    stored.raise_for_status()
    transferred = stored.json()

    completed = client.post(
        f"/v1/uploads/{opened['upload_id']}/complete",
        headers=_headers(args),
        json={
            "artifact_id": transferred["artifact_id"],
            "document_id": document_id,
            "knowledge_base_id": args.knowledge_base_id,
            "granted_principals": [args.principal_id],
        },
    )
    completed.raise_for_status()
    return {"document_id": document_id, **completed.json()}


def _qdrant_points(url: str, collection: str) -> int | None:
    """How many points the index holds, or None if it cannot be asked."""

    try:
        response = httpx.get(f"{url}/collections/{collection}", timeout=5.0)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return int(response.json()["result"]["points_count"] or 0)


def _await_indexing(args: argparse.Namespace, before: int | None) -> int | None:
    """Wait for the ingestion worker to drain the outbox into Qdrant."""

    if before is None:
        return None
    deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        current = _qdrant_points(args.qdrant_url, args.collection)
        if current is not None and current > before:
            return current
        time.sleep(POLL_SECONDS)
    return before


def _await_task(
    client: httpx.Client, args: argparse.Namespace, task_id: str
) -> dict[str, Any]:
    """Wait for a Worker to claim and settle the Task."""

    deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS
    task: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/tasks/{task_id}", headers=_headers(args))
        response.raise_for_status()
        task = response.json()
        if task["status"] in {"succeeded", "failed", "cancelled", "dead_letter"}:
            return task
        time.sleep(POLL_SECONDS)
    return task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_local",
        description="Drive a running local workbench and report what happened.",
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--collection", default="knowledge_bge_m3_v1")
    parser.add_argument("--tenant-id", default="tenant_local")
    parser.add_argument("--principal-id", default="user_local")
    parser.add_argument("--knowledge-base-id", default="kb_local")
    args = parser.parse_args(argv)

    client = httpx.Client(base_url=args.api_url, timeout=30.0)
    with client:
        _step("health")
        try:
            ready = client.get("/health/ready")
        except httpx.HTTPError:
            return _fail(
                f"the API is not answering on {args.api_url}",
                "scripts/dev.sh api",
            )
        _fact("ready", ready.status_code)

        _step("upload -> document version")
        try:
            version = _upload(client, args)
        except httpx.HTTPStatusError as error:
            return _fail(
                f"upload failed with HTTP {error.response.status_code}",
                "check the API log; the request bodies are in it",
            )
        _fact("document_id", version["document_id"])
        _fact("source_revision", version.get("source_revision"))

        _step("ingestion worker -> Qdrant")
        before = _qdrant_points(args.qdrant_url, args.collection)
        if before is None:
            return _fail(
                f"no collection {args.collection!r} in Qdrant",
                "scripts/dev.sh ingest   (it creates the index on startup)",
            )
        after = _await_indexing(args, before)
        _fact("points before", before)
        _fact("points after", after)
        if after is None or after <= before:
            return _fail(
                f"the document was accepted but not indexed within "
                f"{WORKER_TIMEOUT_SECONDS:.0f}s",
                "check that scripts/dev.sh ingest is running and past its model "
                "load; the first start reads several GB of weights",
            )

        _step("task -> LangGraph -> settled")
        submitted = client.post(
            "/v1/tasks",
            headers={**_headers(args), "Idempotency-Key": f"smoke-{uuid.uuid4().hex}"},
            json={
                "objective": "Summarise how fusion is performed.",
                "max_revisions": 1,
            },
        )
        submitted.raise_for_status()
        task_id = submitted.json()["task_id"]
        _fact("task_id", task_id)

        task = _await_task(client, args, task_id)
        _fact("status", task.get("status"))
        if task.get("status") != "succeeded":
            return _fail(
                f"the Task ended {task.get('status')!r} rather than succeeded",
                "scripts/dev.sh worker   (is the Task worker running?)",
            )

        timeline = client.get(
            f"/v1/tasks/{task_id}/timeline",
            headers=_headers(args),
            params={"limit": 50},
        )
        timeline.raise_for_status()
        kinds = [event["event_type"] for event in timeline.json()["events"]]
        _fact("timeline", " -> ".join(kinds))

    print("\n\033[32mall three slices ran\033[0m", flush=True)
    print(
        "  not covered here: chat and generation. This deployment has no model\n"
        "  provider, so the API serves no chat route at all.\n",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
