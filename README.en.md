# Agent Workbench

Agent Workbench is a clean-room portfolio project whose target is a general
Agent platform with two product modes:

- Chat Mode: multi-turn conversation and authorized RAG;
- Task Mode: recoverable LangGraph workflows and controlled multi-Agent work.

The custom Agent runtime remains framework-neutral. LlamaIndex, LangGraph,
LangChain and later comparison adapters stay behind explicit ports.

## Current status

As of 2026-07-25, `main@f071323` contains **PR-001 through PR-015** and the
ADR-012 identity decision. Implemented and tested today:

- framework-neutral domain contracts, ports, fake adapters and a reproducible
  CLI demo;
- the custom `ClaudeLikeAgentRuntime`, including its tool loop, schema/policy
  gateway, deadlines, cancellation, parallel reads, exclusive barriers and
  hooks;
- an offline contract-tested DeepSeek OpenAI-compatible streaming adapter;
- PostgreSQL conversations, migrations, documents, versions, ACLs,
  transactional outbox and `SKIP LOCKED` claiming;
- a local artifact store and FastAPI upload/artifact/health endpoints.

The boundaries are equally important:

- The DeepSeek adapter is not wired into Bootstrap, the API or the CLI, and
  there is no live-provider E2E. `agent-cli demo` still uses the scripted model.
- The implemented HTTP surface is an upload slice, not Chat or Task. RAG,
  LangGraph workflows, multi-Agent execution, SSE, approvals, UI, production
  identity and deployment remain planned.
- PostgreSQL task registry, leases, fencing, checkpoints and LISTEN/NOTIFY
  coordination are not implemented.

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
