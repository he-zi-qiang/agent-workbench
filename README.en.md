# Agent Workbench

Agent Workbench is a clean-room portfolio project whose target is a general
Agent platform with two product modes:

- Chat Mode: multi-turn conversation and authorized RAG;
- Task Mode: recoverable LangGraph workflows and controlled multi-Agent work.

The custom Agent runtime remains framework-neutral. LlamaIndex, LangGraph,
LangChain and later comparison adapters stay behind explicit ports.

## Current status

As of 2026-08-02, the React increment is on `feat/react-chat-work-ui`, based on
the completed Task/HITL/side-effect-ledger baseline at `9d05bbd`. Current
evidence and historical increments are separated in
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
- a local-only Docker Compose topology for PostgreSQL, Qdrant, migrations, the
  API and explicitly opted-in synthetic workers.

Validation for this frontend increment:

- Ruff format and lint: passed;
- Pyright: 0 errors and 0 warnings;
- tests without external services: 1264 passed, 568 environment-gated skips;
- frontend ESLint, strict TypeScript and production build: passed;
- Vitest: 45 passed; Playwright desktop/mobile smoke: 2 passed.

The latest full PostgreSQL/Qdrant evidence is recorded separately in
`docs/status.md`; results from the two environments are not added together.

The remaining boundaries are explicit: durable HITL approval, a real external
search provider, physical deletion of stale Qdrant points, verifiable
citations, context compaction, EventLog upcasters/poison-row handling,
LlamaIndex/LangChain business adapters, the CrewAI comparison, dynamic
multi-Agent supervision, OpenTelemetry/Langfuse, production identity and
production deployment are not complete. A React Chat/Work console is now
implemented; LlamaIndex remains the selected primary RAG integration and RAGAS
the offline evaluation baseline, but both adapters are still pending evidence.

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

See [NOTICE.md](NOTICE.md) and [docs/compliance.md](docs/compliance.md) for the
clean-room boundary.

Current implementation evidence is tracked in [docs/status.md](docs/status.md).
