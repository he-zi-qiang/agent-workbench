# Agent Workbench

Agent Workbench is a clean-room portfolio project whose target is a general
Agent platform with two product modes:

- Chat Mode: multi-turn conversation and authorized RAG;
- Task Mode: recoverable LangGraph workflows and controlled multi-Agent work.

The custom Agent runtime remains framework-neutral. LlamaIndex, LangGraph,
LangChain and later comparison adapters stay behind explicit ports.

## Current status

As of 2026-08-08, `main` carries the Task/HITL/side-effect-ledger baseline, the
three fencing fixes from PR #68, the React Chat/Work console (PR #69), the
LlamaIndex retrieval adapter and routed-threshold evaluation (PR #72, #73), and
Chat web search with the tool-ceiling semantics (PR #74). ADR-018 through 022 —
ungrounded chat shape, run-step transparency, external search, Chat's routed
fallback going online, and what a spent tool allowance means — are all on
`main`, taking the config schema to `1.6`. Current evidence and historical
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
- a React Chat/Work console served same-origin by FastAPI, described in
  [the frontend baseline](docs/frontend-design.md);
- a local-only Docker Compose topology for PostgreSQL, Qdrant, migrations, the
  API and explicitly opted-in synthetic workers.

Validation for the current tree:

- Ruff format and lint: passed;
- Pyright: 0 errors and 0 warnings;
- tests without external services: 1264 passed, 568 environment-gated skips;
- the same tree against real PostgreSQL and Qdrant: 1821 passed, 11 skipped
  (the 11 need BGE weights);
- frontend ESLint, strict TypeScript and production build: passed;
- Vitest: 45 passed; Playwright desktop/mobile smoke: 2 passed.

The two environments are quoted separately and never added together.

External search now has a real provider
([ADR-020](docs/adr/0020-external-web-search.md)): DeepSeek's server-side
`web_search`, over the provider's Anthropic-compatible endpoint, introducing no
second API key. [ADR-021](docs/adr/0021-chat-web-search.md) extends it to Chat's
routed fallback as a tool the model may decline, and an answer that used the web
does not count as grounded. `research.enabled` stays **off by default**: the
field also decides how wide the Task authorization envelope is, and that
envelope is stored with the Task and re-applied on every resume. Its tests all
run against a fake port — nothing exercises the real endpoint.

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
