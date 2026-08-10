# Agent Workbench

Agent Workbench is a clean-room portfolio project whose target is a general
Agent platform with two product modes:

- Chat Mode: multi-turn conversation and authorized RAG;
- Task Mode: recoverable LangGraph workflows and controlled multi-Agent work.

The custom Agent runtime remains framework-neutral. LlamaIndex, LangGraph,
LangChain and later comparison adapters stay behind explicit ports.

## Current status

As of 2026-08-09, the current tree carries the Task/HITL/side-effect-ledger baseline, the
three fencing fixes from PR #68, the React Chat/Work console (PR #69), the
LlamaIndex retrieval adapter and routed-threshold evaluation (PR #72, #73), and
Chat web search with the tool-ceiling semantics (PR #74). ADR-018 through 022 —
ungrounded chat shape, run-step transparency, external search, Chat's routed
fallback going online, and what a spent tool allowance means — are all on
`main`. WP14-01 now adds the MCP adapter and takes the config schema to `1.8`.
WP15's first three stages have landed: the Task workspace
([ADR-028](docs/adr/0028-task-workspace.md)), the ephemeral sandbox
([ADR-029](docs/adr/0029-ephemeral-sandbox.md), schema `1.9`), and reading
outward ([ADR-027](docs/adr/0027-read-outward-write-inward.md), schema `1.10`,
PR #83–#86). [ADR-032](docs/adr/0032-the-external-researcher-is-an-agent.md)
then closed the one segment of that line which was declared but never wired:
`researcher_external` now actually runs an agent loop when it holds tools
(PR #87). Current evidence and historical
increments are separated in
[the implementation status](docs/status.md).

Implemented with test evidence:

- a framework-neutral `ClaudeLikeAgentRuntime` with a policy-gated Tool loop,
  budgets, cancellation, deterministic parallel scheduling and hooks;
- fixed two-step RAG Chat with PostgreSQL conversations, idempotent turns,
  ACL/source-revision release fencing, durable events and crash recovery;
- BGE-M3 dense/sparse adapters, authorized Qdrant dense/hybrid retrieval,
  reranking after authorization, ingestion/outbox workers and startup checks;
- a Task API and CLI, durable Task input artifacts, a standalone Task Worker,
  strict structured plan/critic handlers and an explicit demo composition;
- tenant-scoped Task idempotency and an explicit successful/failed workflow
  disposition with exact revision-budget semantics;
- PostgreSQL `SKIP LOCKED` claiming, leases, heartbeats, epochs, stale reclaim,
  retry/dead-letter transitions, a session advisory execution guard and a
  fenced LangGraph PostgreSQL checkpointer;
- deterministic failpoints for claim, graph, checkpoint and final-settlement
  crash windows, plus atomic durable Task lifecycle events;
- three fences that are no longer satisfied by the thing they check (PR #68):
  an `intended` row from an older epoch is handed to a human rather than read
  as permission to dispatch, graph nodes write under the immutable
  `ExecutionLease` obtained at claim time instead of re-reading whichever epoch
  is live, and `knowledge_search` journals the passages that were rendered to
  the model rather than everything retrieval returned;
- durable HITL approval through a LangGraph interrupt, an authoritative ledger,
  a versioned decision API and cross-process resume;
- OpenTelemetry traces and metrics behind a port, with the core importing no
  SDK, and a LangChain `BaseTool` adapter that enters the same tool gateway;
- an optional, off-by-default MCP adapter using the official Python SDK v2:
  explicit deployment allowlists are frozen into Task authority, Worker
  discovery narrows them again, and only `writer/synthesize` receives the
  resulting tools through the existing Runtime and Tool Gateway;
- a Task workspace: mutable names over immutable bytes. Writing a name stores
  new bytes and produces a new manifest, the manifest is itself an artifact, and
  so "which version of the workspace" is one id a checkpoint can hold — a
  replayed node sees the version pinned at its entry, not the half-finished
  writes of the attempt that died. Only `writer/synthesize` holds the three
  tools;
- an optional, off-by-default ephemeral sandbox: one throwaway container per
  call, files in and files out, with no network, a read-only root, a non-root
  user, dropped capabilities and memory/CPU/process/wall-clock ceilings. The
  isolation is constants rather than configuration — the absent network is the
  premise the replay guarantees rest on, not a hardening option. The Task-side
  `sandbox_run` reads its inputs from the workspace and writes the outputs back;
  the sandbox process itself knows nothing of workspaces, tenants or owners. A
  deployment with no container runtime has one fewer capability, not a Worker
  that will not start;
- an optional, off-by-default read-outward lab: `web_mcp`'s `fetch_page` and
  `download_document` are both GETs carrying no `operation_key`. Every fetch
  passes an address guard that checks the **resolved** destination: only
  globally routable addresses proceed, and everything else — multicast and
  `169.254.169.254` included — falls in the complement of `is_global`, because a
  denylist is a list somebody has to remember to extend and a complement is not.
  Redirects are followed hop by hop inside the adapter rather than handing the
  choice of destination to the HTTP client. **DNS rebinding is explicitly out of
  scope**; closing it means changing the transport. Two tools rather than one
  tool with a mode: a PDF run through HTML extraction yields garbage that reads
  like a successful read;
- which server's tools reach which Agent is declared in configuration:
  `[[mcp.servers]].audience` names a purpose (`research` / `synthesis`) rather
  than a protocol, so adding a reader or a renderer is a config change, not a
  profile-code change. Audience does **not** widen the authorization envelope —
  the envelope is the Task's ceiling, audience is which Agent can reach into it
  — and profiles widen by the tools actually **registered**, never by config,
  since otherwise a node would request a tool the Gateway cannot resolve;
- an external researcher that is an Agent when it holds tools (ADR-032). The
  change is purely additive: the original deterministic search stays, and only
  when this Worker really registered research-audience tools does a second,
  tool-carrying agent run happen, with both halves' evidence merged by the
  graph's own fan-in reducer. A deployment with an empty catalogue takes no
  extra step. Its output must be JSON evidence items rather than prose, because
  `synthesize` reads an `EvidenceBundle`; `{"items":[]}` is a permitted answer,
  while unparseable output fails the node — demoting "could not read" to "found
  nothing" lets the next node write a plausible report on top of silence;
- a React Chat/Work console served same-origin by FastAPI, described in
  [the frontend baseline](docs/frontend-design.md);
- a local-only Docker Compose topology for PostgreSQL, Qdrant, migrations, the
  API and explicitly opted-in synthetic workers.

Validation for the current tree, measured on `main@a4dea2b`:

- Ruff format and lint: passed (421 files);
- Pyright: 0 errors and 0 warnings;
- tests without external services, ignoring `tests/e2e`: 1784 passed, 597
  environment-gated skips;
- the E2E directory: 3 passed, 11 PostgreSQL-gated skips;
- architecture and configuration guards: 114 passed;
- lock-file and dependency-license policy gates: passed;
- frontend ESLint, strict TypeScript and production build: passed;
- Vitest: 45 passed; Playwright desktop/mobile smoke: 2 passed.

PostgreSQL, Qdrant and local BGE weights were not running for this check.
Environment-gated skips are reported rather than represented as stateful
verification; the earlier stateful evidence remains dated in
[the implementation status](docs/status.md), which also records the real Task
acceptance run behind ADR-032 — a separate run, on a machine with those
services, that must not be merged with the numbers above.

External search now has a real provider
([ADR-020](docs/adr/0020-external-web-search.md)): DeepSeek's server-side
`web_search`, over the provider's Anthropic-compatible endpoint, introducing no
second API key. [ADR-021](docs/adr/0021-chat-web-search.md) extends it to Chat's
routed fallback as a tool the model may decline, and an answer that used the web
does not count as grounded. `research.enabled` stays **off by default**: the
field also decides how wide the Task authorization envelope is, and that
envelope is stored with the Task and re-applied on every resume. Its tests all
run against a fake port — nothing exercises the real endpoint.

The sandbox does not reach the network, keeps no state between calls, offers no
GPU, and does not promise byte-identical replay — a script may call `time.time()`
or `random`, and ADR-029 §3.4 says so rather than pretending otherwise. Reading
outward does no form filling, no clicking, no POST of any kind, and drives no
desktop software; JS-rendered pages and screenshots need a browser engine and
are explicitly out of scope per ADR-027 §3.5, so **an SPA yielding no article
text is a known boundary rather than a bug**. WP15 stages four (cost and
deadline as the governing budget, `workspace_edit`, `workspace_grep`) and five
(the second graph `v2_general`) have not started. The plan is explicit that
stage four is not optional: with the tools complete and the budget unchanged, a
node that genuinely iterates hits the wall at twelve steps, and that symptom
reads like a weak model.

One cost boundary found by measurement: a node that reads web pages does not fit
the default token ceiling. An article runs 20–50 KB, two reads come to roughly
28000 tokens, and the default 16000 of
`multi_agent.max_tokens_per_agent_invocation` stops the run mid-JSON — every tool
succeeded and the node still failed. The default is unchanged; only
`config/config.web-local.toml` raises it to 120000.

The remaining boundaries are explicit: physical deletion of stale Qdrant points,
context compaction, EventLog upcasters/poison-row handling, the CrewAI
comparison, dynamic multi-Agent supervision, Langfuse, production identity and
production deployment are not complete. LlamaIndex is the selected primary RAG
integration and RAGAS the offline evaluation baseline
([ADR-017](docs/adr/0017-llamaindex-primary-rag.md)). The retrieval adapter is
built and contract-tested, but `rag.llama_index.enabled` is `false` — what is
missing is not the implementation but a measurement able to tell the two
retrieval paths apart (ADR-017 step 3). Ingestion is not migrated and the RAGAS
runner does not exist, so both stay Planned in the capability table: an adapter
existing is not the finished framework integration.

> **Security warning:** the current identity adapter trusts request headers, so
> `agent-api` is for controlled local development only and must not be exposed to
> a LAN or the Internet. The API and local Compose mapping are forced to
> loopback, but that prevents accidental exposure; it is not authentication,
> and a real identity provider is still unimplemented.

See [the implementation status](docs/status.md) for the complete increment
history, test evidence, known defects and remaining scope.

## Try it

```bash
uv run agent-cli demo
```

The scripted model runs offline and the output is byte identical on every run.
To see a denied call, where the handler never runs at all:

```bash
uv run agent-cli demo --deny
```

## Local configuration check

Prerequisites are Python 3.12 and `uv`.

1. Install the locked development environment with
   `uv sync --frozen --group dev --no-editable`.
2. Copy `.env.example` to `.env` and replace local-only placeholders.
3. Run:

```bash
uv run agent-config-check --profile development
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

The configuration check validates structure and security invariants. It does
not connect to PostgreSQL, Qdrant or an online model. Once dependencies are
synchronized, tests and static checks can run offline.

## Design sources

- [Documentation index (Chinese)](docs/README.md)
- [Architecture and technology baseline v1.3 (Chinese)](docs/architecture-baseline.md)
- [Code implementation plan v1.0 (Chinese)](docs/implementation-plan.md)
- [Configuration contract](docs/configuration.md)

## License

Released under the [Apache License 2.0](LICENSE). Keep [NOTICE.md](NOTICE.md)
with any copy or derivative work — Apache-2.0 section 4(d) requires it.

Dependencies keep their own licenses; the gate that decides which are
acceptable is described in [docs/compliance.md](docs/compliance.md).

See [NOTICE.md](NOTICE.md) and [docs/compliance.md](docs/compliance.md) for the
clean-room boundary.

Current implementation evidence is tracked in [docs/status.md](docs/status.md).
