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

Chat is not exercised. Whether it is *served* is a fact about the deployment
answering, not about this script, so the closing note asks rather than asserts:
the same run against `config.local.toml` and against the console profile ends
with two different true sentences.

Everything it reports is read back from the running system. Where it cannot
confirm something -- an ingestion worker that is not running, a Task nobody
claimed, a Task that settled with its tool calls refused -- it says so and exits
non-zero, because a walkthrough that printed "OK" for work nobody did would be
worse than no walkthrough.
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

#: The Task wait, kept apart from the ingestion one above because the two are
#: measuring different work. 300s is what a demo graph costs; the console
#: profile runs the real research graph -- understand -> plan -> route ->
#: research -> synthesize -> critic -> quality_gate -- and the run this number
#: was measured against settled `succeeded` after 301 seconds. One second past
#: the shared deadline, and what that one second printed was not "slow": it was
#: "the Task ended 'running'", under a hint asking whether the Task worker was
#: running, about a Worker that had claimed the Task and was two nodes from
#: finishing it. Sharing a constant made the cheaper of the two waits the
#: ceiling for the more expensive one.
TASK_TIMEOUT_SECONDS = 900.0

#: Nothing moves these on its own. `waiting_approval` is a Worker that released
#: its lease because a human decision is owed; `waiting_migration` is a graph
#: this deployment cannot run, which is also a human's call. Polling either
#: until the deadline buys nothing and spends the reader's whole wait before
#: saying anything, so the wait ends the moment one is seen.
PARKED_TASK_STATUSES = frozenset({"waiting_approval", "waiting_migration"})

#: The Task's verdict. No Worker resumes one of these.
SETTLED_TASK_STATUSES = frozenset({"succeeded", "failed", "cancelled", "dead_letter"})

POLL_SECONDS = 2.0

SAMPLE = """The application performs one dense and sparse fusion per query.

Qdrant returns the dense and sparse candidate arms separately. The application
orders each arm deterministically by score and chunk identifier, then performs
reciprocal rank fusion once. A retrieval that fused in two places would be two
retrievers, and the one with less attention would be the one that drifted.

Deletions are tombstoned and reconciled rather than removed in place.
"""


#: What the browser console asks for, kept the same here on purpose -- see the
#: list and its reasoning in `web/src/app/IdentityContext.tsx`. The envelope and
#: the principal's scopes are two separate gates, so a tool inside the profile's
#: envelope is still denied `missing_permission_scope` when the submitter holds
#: no scope for it. This script sent none at all, which cost nothing against
#: `config.local.toml` (whose envelope has no MCP tool) and everything against
#: the console profile: one run there proposed `external_search`,
#: `mcp_web_fetch_page`, `mcp_web_download_document` and `workspace_write` and
#: had all seven calls denied, while still settling `succeeded` -- a walkthrough
#: reporting a green Task whose every tool was refused.
#:
#: Holding a scope for a tool the deployment never registered costs nothing: the
#: envelope is narrowed to what the Worker discovered (ADR-025), so an unused
#: scope authorises no tool that exists. Identity here is self-declared and
#: unvalidated by design (ADR-044 §1.1).
CONSOLE_SCOPES: tuple[str, ...] = (
    "artifact:export",
    "external:search",
    "workspace:write",
    "mcp:web",
    "mcp:word",
    # `sandbox_run` (ADR-057). Added here in the same commit that added it to
    # the console, because the parity test between the two is the thing that
    # noticed the last time they drifted.
    "sandbox:run",
    # `project_run` (ADR-077), same commit as the console for the same reason.
    # It buys this script nothing today -- the walkthrough drives Tasks, and
    # `project_run` never enters a Task envelope -- and it is here anyway,
    # because the parity test compares the two lists rather than asking which
    # entries either side uses.
    "project:run",
)


def _headers(args: argparse.Namespace) -> dict[str, str]:
    return {
        "x-tenant-id": args.tenant_id,
        "x-principal-id": args.principal_id,
        "x-principal-scopes": ",".join(args.scopes),
    }


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


def _ensure_knowledge_base(
    client: httpx.Client, args: argparse.Namespace
) -> dict[str, Any]:
    """Create the walkthrough KB when a fresh database does not have it yet."""

    response = client.get(
        f"/v1/knowledge-bases/{args.knowledge_base_id}", headers=_headers(args)
    )
    if response.status_code == 200:
        return response.json()
    if response.status_code != 404:
        response.raise_for_status()

    created = client.post(
        "/v1/knowledge-bases",
        headers=_headers(args),
        json={
            "name": "Local walkthrough",
            "description": "Created idempotently by scripts/smoke_local.py.",
        },
    )
    created.raise_for_status()
    record = created.json()
    # IDs are server-owned. Carry the returned identifier through the rest of
    # this one walkthrough instead of asking a reader to edit and rerun it.
    args.knowledge_base_id = record["knowledge_base_id"]
    return record


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


def _chat_is_served(client: httpx.Client) -> bool:
    """Whether this deployment actually mounts the chat routes.

    Asked rather than assumed. The closing note used to state flatly that the
    API serves no chat route, which was true of the keyless profile this script
    was written against and false of the console profile it is now also run
    against -- so the walkthrough ended by describing a deployment it was not
    talking to. Read off the served schema, which is the same surface any client
    would see; a deployment that cannot be asked is reported as not serving it,
    which is the conservative direction for a sentence claiming a capability.
    """

    try:
        response = client.get("/openapi.json")
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    return "/v1/chat/sessions" in response.json().get("paths", {})


def _await_task(
    client: httpx.Client, args: argparse.Namespace, task_id: str
) -> dict[str, Any]:
    """Wait until the Task settles, parks, or outlasts the wait.

    Returns whatever the last poll saw, status included. Turning that into a
    verdict is the caller's job, and there are three answers to tell apart
    rather than one -- see the branch that reads it.
    """

    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    stop = SETTLED_TASK_STATUSES | PARKED_TASK_STATUSES
    task: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/tasks/{task_id}", headers=_headers(args))
        response.raise_for_status()
        task = response.json()
        if task["status"] in stop:
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
    parser.add_argument(
        "--scopes",
        default=",".join(CONSOLE_SCOPES),
        type=lambda raw: tuple(part for part in raw.split(",") if part),
        help="comma-separated principal scopes; defaults to what the console asks for",
    )
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

        _step("knowledge base")
        try:
            knowledge_base = _ensure_knowledge_base(client, args)
        except httpx.HTTPStatusError as error:
            return _fail(
                f"knowledge-base setup failed: {error}",
                "create a writable knowledge base and pass its id with "
                "--knowledge-base-id",
            )
        _fact("knowledge_base_id", knowledge_base["knowledge_base_id"])

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
        status = task.get("status")
        _fact("status", status)
        # Three different things used to print as one sentence -- "the Task
        # ended 'running' rather than succeeded", under a hint pointing at the
        # Worker. Only one of the three is about the Worker, and `running` is
        # not even a state a Task can end in. What the reader was sent to check
        # was, every time, the thing that was already working.
        if status in PARKED_TASK_STATUSES:
            return _fail(
                f"the Task is parked at {status!r}; nothing is executing it",
                f"a decision is owed, not a restart -- GET /v1/tasks/{task_id}"
                f"/timeline for where it stopped",
            )
        if status not in SETTLED_TASK_STATUSES:
            # The wait ran out while the Task was still moving, which is not a
            # verdict at all. Which non-terminal state it is in decides who to
            # suspect: `queued` means nothing ever claimed it, `running` means
            # something did and is still at it.
            return _fail(
                f"the Task was still {status!r} after "
                f"{TASK_TIMEOUT_SECONDS:.0f}s, which is not a failure",
                "scripts/dev.sh worker   (nothing claimed it)"
                if status == "queued"
                else f"a Worker has it and has not finished; ask again: "
                f"GET /v1/tasks/{task_id}",
            )
        if status != "succeeded":
            return _fail(
                f"the Task settled {status!r} rather than succeeded",
                f"GET /v1/tasks/{task_id}/timeline for the event that ended it",
            )

        timeline = client.get(
            f"/v1/tasks/{task_id}/timeline",
            headers=_headers(args),
            params={"limit": 50},
        )
        timeline.raise_for_status()
        events = timeline.json()["events"]
        kinds = [event["event_type"] for event in events]
        _fact("timeline", " -> ".join(kinds))
        # A settled Task is not a working one. `succeeded` is about the graph
        # reaching its end, and a run whose every tool call was refused reaches
        # it too -- by writing an answer out of the model's own memory. That is
        # the one thing this walkthrough exists to not print a green line for,
        # and it is what it printed before the scopes above were sent.
        refused = kinds.count("ToolFailed")
        if refused:
            # The codes themselves, counted, rather than a sentence guessing at
            # one of them. This used to name `missing_permission_scope` as the
            # thing to go and check, which is the failure this guard was
            # written for -- and on the console profile the ordinary answer is
            # `tool_failed` from `fetch_page`, because the open web is full of
            # pages that do not come back. Sending the reader to widen
            # `--scopes` over a page that 403'd is the same wrong turn the
            # Task-status branch above used to send them on.
            codes: dict[str, int] = {}
            for event in events:
                if event["event_type"] != "ToolFailed":
                    continue
                payload = event.get("payload") or {}
                error = payload.get("error") or {}
                code = str(error.get("code") or "unknown")
                codes[code] = codes.get(code, 0) + 1
            _fact("tool calls that failed", refused)
            _fact(
                "their error codes",
                ", ".join(f"{code} x{count}" for code, count in sorted(codes.items())),
            )
            return _fail(
                f"the Task settled {task.get('status')!r} with {refused} failed "
                "tool calls",
                "`missing_permission_scope` means --scopes is narrower than this "
                "profile's envelope; `tool_failed` is the tool's own refusal and "
                "for `fetch_page` usually means the page did not come back",
            )

        chat_served = _chat_is_served(client)

    print("\n\033[32mall three slices ran\033[0m", flush=True)
    if chat_served:
        print(
            "  not exercised here: chat and generation. This deployment does\n"
            "  serve them -- ask it something from /ui, or POST a message to\n"
            "  /v1/chat/sessions.\n",
            flush=True,
        )
    else:
        print(
            "  not covered here: chat and generation. This deployment has no\n"
            "  model provider, so the API serves no chat route at all.\n",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
