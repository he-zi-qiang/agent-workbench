# Agent Workbench

Agent Workbench is a clean-room portfolio project for building a general Agent
platform with two product modes:

- Chat Mode: multi-turn conversation and authorized RAG;
- Task Mode: recoverable LangGraph workflows and controlled multi-Agent work.

The custom Agent runtime remains framework-neutral. LlamaIndex, LangGraph,
LangChain and later comparison adapters stay behind explicit ports.

## Current status

**PR-001 Bootstrap**, **PR-002 Config CI**, **PR-003 Domain**, **PR-004 Ports +
Fakes**, **PR-005 CLI Skeleton** and **PR-006 Runtime Serial Loop** have been
implemented and validated locally. The tool gateway (schema validation and
hooks), the parallel read scheduler, RAG, workflow, multi-Agent, API and UI
capabilities remain planned and must not be described as implemented.

PR-003 delivers the framework-neutral domain contracts -- messages, tools,
events, context, run budgets, policy decisions and error codes -- using nothing
but the standard library and Pydantic. Invariants such as one result per tool
call, event durability and grounded citations are enforced at construction time
rather than described in comments.

PR-004 adds the model, tool, agent, event and store protocols on top, together
with dependency-free implementations: a scripted model, in-memory event log,
conversation store and artifact store, two side-effect-free tools and a
deny-by-default policy engine. Contract tests therefore run offline and
deterministically, without a database, a vector store or a live model.

PR-005 connects those pieces into the first runnable vertical slice: input,
model, unified events, output. The CLI consumes only events and the returned
outcome. Streamed text comes from transient deltas while the timeline is
replayed from the durable log, and the difference between them is the
durability rule itself.

PR-006 adds the custom `ClaudeLikeAgentRuntime`: a serial `model -> tool ->
result -> model` loop whose state machine is an executable transition table
rather than a diagram, so an illegal phase change raises instead of producing a
plausible-looking run. Every exposed `tool_call_id` ends with exactly one
`ToolResult` -- unknown tool, denied call, raising handler, timeout, mid-batch
cancellation -- and results are always submitted in the model's own call order.

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
