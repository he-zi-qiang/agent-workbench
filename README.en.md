# Agent Workbench

Agent Workbench is a clean-room portfolio project whose target is a general
Agent platform with two product modes:

- Chat Mode: multi-turn conversation and authorized RAG;
- Task Mode: recoverable LangGraph workflows and controlled multi-Agent work.

The custom Agent runtime remains framework-neutral. LlamaIndex, LangGraph,
LangChain and later comparison adapters stay behind explicit ports.

## Current status

As of 2026-07-28, the main-branch baseline is `main@4d03f69`. The current
development branch completes the PR-035 through PR-043 slices for secure
answer release, multi-turn context, evolvable EventLog replay, idempotent
Chat turns, atomic authorization fencing, traffic-independent recovery and
atomic fixed-lease expiry.
Implemented with test evidence:

- framework-neutral domain contracts, ports, fake adapters and a reproducible
  CLI demo;
- the custom `ClaudeLikeAgentRuntime`, including its tool loop, schema/policy
  gateway, budgets and deadlines, cancellation, parallel read scheduling,
  exclusive barriers and Hook Bus;
- a DeepSeek OpenAI-compatible streaming adapter, configuration projection and
  API assembly;
- PostgreSQL conversations and Alembic migrations, document/version/ACL
  storage, a transactional outbox, competitive `SKIP LOCKED` claiming and an
  ingestion-worker component;
- a local artifact store and FastAPI upload, artifact, health, Chat and SSE
  APIs;
- PostgreSQL EventLog replay with per-stream gap-free sequences, an explicit
  envelope schema version, the producer timestamp and stream-local durable
  `event_key` idempotency;
- BGE-M3 dense embeddings, Qdrant dense/hybrid retrieval and offline RAG
  evaluation;
- fixed two-step Chat with two ACL checks, an answer-release gate, a source
  revision read barrier, multi-turn replay and a PostgreSQL `chat_turns` fact
  ledger;
- one PostgreSQL transaction for the final source-revision/ACL check, answer
  event, assistant history and terminal Turn state, with document-row locks
  linearizing answer publication against revocation;
- a required API `Idempotency-Key`, non-interleaving active turns, no model
  rerun for a completed retry, and re-authorization of persisted evidence on a
  `release_pending` retry;
- a fixed execution lease for `running` turns; ordinary terminal writers
  re-check the database clock under the Turn lock, while claim and late
  prepare/cleanup never write an expiry fact;
- a single `ChatExpirationCoordinator` that uses PostgreSQL `SKIP LOCKED` and
  one transaction per Turn to commit the failed Turn and durable
  `ChatTurnExpired` together, with poison-candidate isolation and a stable
  cross-round scan cursor;
- one bounded SHA-256 terminal key shared by answer publication and expiry;
  `ChatTurnExpired` is a Chat-ledger observation, not another Runtime
  `RunFailed`;
- background recovery for prepared answers that re-runs the final ACL/revision
  fence and publishes atomically without relying on the original client; it
  remains active even when the embedding/model stack is unavailable;
- a `knowledge_search` Tool adapter backed by the same `RetrievalService` as
  fixed retrieval.

The remaining boundaries are explicit:

- `IngestionWorker` is still an invocable component rather than a reliable
  resident process: heartbeat, retry/dead-letter handling and fencing of
  external side effects across multiple workers are missing, and the product
  upload-to-search E2E is not yet connected.
- The source-revision barrier prevents stale Qdrant points from being read, but
  physical replacement/deletion of old points is not yet implemented.
- A history token window/compaction and validation of the citations actually
  used by the model remain to be built.
- EventLog rejects an unknown schema version, but version upcasters,
  poison-row isolation and skip semantics are not yet implemented.
- `knowledge_search` is not yet assembled into an agentic retrieval mode, and
  that path still needs a final evidence-revision gate before an answer may be
  released.
- LlamaIndex and LangChain adapters, LangGraph Task workflows, the Task
  Registry, multi-Agent execution, the CrewAI comparison, UI, production
  authentication and deployment remain planned.

> **Security warning:** the current identity adapter trusts request headers, so
> `agent-api` is for controlled local development only and must not be exposed to
> a LAN, a published container port or the Internet. The bind address is now
> forced to loopback (default `127.0.0.1`, checked in settings and again at
> assembly), but that prevents accidental exposure; it is not authentication,
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
