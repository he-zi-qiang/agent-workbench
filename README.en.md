# Agent Workbench

Agent Workbench is a clean-room portfolio project for building a general Agent
platform with two product modes:

- Chat Mode: multi-turn conversation and authorized RAG;
- Task Mode: recoverable LangGraph workflows and controlled multi-Agent work.

The custom Agent runtime remains framework-neutral. LlamaIndex, LangGraph,
LangChain and later comparison adapters stay behind explicit ports.

## Current status

**PR-001 Bootstrap**, **PR-002 Config CI**, **PR-003 Domain**, **PR-004 Ports +
Fakes**, **PR-005 CLI Skeleton**, **PR-006 Runtime Serial Loop**, **PR-007
Policy + Tool Gateway**, **PR-008 Runtime Budgets**, **PR-009 Parallel Reads**
**PR-010 Hook Bus**, **PR-011 DeepSeek Provider Contract** and **PR-012
DeepSeek Model Adapter** have been implemented and validated locally. RAG,
workflow, multi-Agent, API and UI capabilities remain planned and must not be
described as implemented.

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

PR-007 moves those checks into the one tool gateway: a handler runs only after
its *final* arguments have passed both schema validation and an authorization
decision. When a policy answers `allow_with_modified_input`, the rewritten
arguments are validated again and re-submitted for a decision -- rewriting
after the checks would be a way past both. JSON Schema support is a documented
subset, and a schema reaching beyond it is refused when the gateway is
assembled rather than silently unenforced at call time.

PR-008 collapses the layered time limits into a single bound. One model call is
allowed `min(runtime envelope, time left in the run)` -- the model profile's own
timeout is applied by the adapter, one level further in -- and one tool call is
allowed `min(its declared timeout, time left in the run)`, because a tool
granted an hour should not outlive the run that authorized it. Cancellation
takes effect at the next stream event and reaches the adapter by closing the
generator; a model that goes silent instead is bounded by the deadline.

PR-009 runs consecutive read-only tools in one batch concurrently, while write,
external and destructive tools each take a group of their own -- "side effects
cross a barrier" stops being a sentence and becomes a shape in memory. The
grouping is a pure function, checkable without an event loop, and execution
order still says nothing about submission order: results reach the model in the
order it asked for them.

PR-010 adds hooks: deployment-supplied code that inspects, rewrites or blocks a
proposed tool call before anything judges it. Whatever a hook rewrites goes back
through schema validation and authorization, because editing arguments after
the checks would be a way around them, and a hook that raises or hangs blocks
the call -- a broken safety rule must never become permission.

The model provider is DeepSeek, over its OpenAI-compatible protocol. PR-011
changes only the configuration contract -- provider, base URL, secret field and
ownership entries, with the config schema moving to `1.2` -- and not a line of
the runtime, which is what `ModelPort` exists for. The endpoint must be HTTPS
unless it is loopback, and it stays out of task recovery snapshots.

PR-012 lands the adapter itself, over the provider's OpenAI-compatible
streaming API. A tool call is emitted whole -- the provider streams its
arguments as JSON fragments, and partial JSON must never reach schema
validation or policy -- and an HTTP error carries a status code and nothing
else, because a chat completion error body can echo the prompt. Tests drive it
with `httpx.MockTransport` over real wire-format bytes, so CI stays offline;
one of them asserts end to end that the runtime cannot tell this adapter from
the scripted one.

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
