# Agent Workbench

[中文](README.md) | English

A clean-room general Agent platform offering two product shapes: **Chat**
(knowledge-base Q&A with permission checks) and **Task** (recoverable,
approvable automation workflows).

Architecturally there is one claim: **the custom Agent Runtime owns the only Tool
Loop.** LangGraph, LlamaIndex and MCP all enter through Ports/Adapters, each
doing its own segment, and none takes over the core loop.

| If you are | Start here |
|---|---|
| Judging the substance | [**The ten-minute version**](docs/HIGHLIGHTS.md) (Chinese) — a real event stream, gate numbers, four engineering judgements |
| Trying to run it | [Quick start](#quick-start) — one command, no network, no database |
| Asking what is **missing** | [**Known gaps**](docs/known-gaps.md) — four categories, each with a location and a criterion for "done" |
| Reading the rationale | [Documentation map](docs/README.md), [architecture baseline](docs/architecture-baseline.md), [ADR index](docs/adr/) |

---

## 1. Features

### 1.1 Chat: knowledge-base Q&A with permission checks

- **Multi-turn conversation**, with sessions and messages persisted in
  PostgreSQL; `chat_turns` is the idempotent source of truth.
- **Retrieval-grounded answers**: fixed two-step retrieval
  (`chat.retrieval_shape` also accepts `agentic`, but the **default is `fixed`** —
  only the fixed shape makes evaluation reproducible). Every citation carries
  `chunk_id`, `document_id` and `document_version`.
- **Permissions run the whole length**: candidates are ACL-filtered, and the
  source revision plus authorization are **re-checked before publication**;
  revocation and answer publication are linearized by a document row lock. The
  reranker runs after authorization, so it cannot introduce a passage the asker
  may not read.
- **If it cannot answer, it says so** rather than producing something that merely
  looks like an answer. This is scored as its own item in evaluation.
- **Web fallback**: when the corpus cannot answer, an external search may be
  called (off by default). An answer that used the web **does not count as
  grounded**, and the console distinguishes the two.
- **Every turn lists the tools authorized for it**, highlighting those actually
  called; a tool call shows the tool name plus what it was called with (e.g.
  `web_search · 北京今天天气`), and failures show the error message rather than a
  code.
- **Streaming over SSE**, resumable by cursor after a disconnect.

### 1.2 Task: recoverable, approvable workflows

Submit an objective; the Agent decomposes it, retrieves, does the work and
produces files, pausing for human approval where required.

**Two graphs, chosen at submission and then frozen:**

| Graph | Node path | Purpose |
|---|---|---|
| Fixed research graph | `understand → plan → route →`{`research_internal`\|`research_external`}`→ synthesize → critic → quality_gate → approval → export` | Retrieval, synthesis, self-critiquing research reports |
| `v2_general` | `understand → work → review →`(`approval`)`→ export` | General work: read tools, write workspace, render a document |

- **Triage at submission**: `POST /v1/tasks/triage` lets the model decide which
  graph applies, asks a human when it cannot, and falls back to a default on
  failure.
- **Human in the loop**: external side effects such as `export` require
  approval. The graph stops at a LangGraph interrupt, the decision is written to
  an authoritative ledger, and it is re-applied after cross-process recovery.
- **Task workspace**: mutable names over immutable bytes, scoped to one Task.
  Writing a name produces a new manifest, and the manifest is itself an artifact —
  so "which version of the workspace" is an id a checkpoint can hold, and a
  replayed node sees the version at its own entry.
- **Ephemeral sandbox** (off by default): one container per call, files in and
  files out, no network, read-only root, non-root, capabilities dropped, with
  memory/CPU/process/wall-clock ceilings.
- **Read-only outward access** (off by default): `fetch_page` and
  `download_document` are both GETs and pass a **post-resolution** address gate —
  only globally routable addresses are allowed, and redirects are gated hop by hop.
- **Artifact export**: `.docx` and similar land in the ArtifactStore and can be
  read (text preview) and downloaded from the console.
- **Everything leaves a trace**: each tool call records
  `ToolProposed → PermissionResolved → ToolStarted → ToolCompleted`, and **a
  refused call is recorded too** rather than vanishing.

### 1.3 Knowledge bases and ingestion

Create a knowledge base → upload files → asynchronous ingestion (parse, chunk,
embed, write to Qdrant) → retrievable. PDF, Word, Markdown and plain text.

- Documents are versioned by **revision**; revisions and revocation take effect
  through the revision fence.
- The ingestion Worker claims work with PostgreSQL `SKIP LOCKED`, with
  lease/heartbeat/fencing.
- **Ingestion failure is stated out loud**: `documents` records
  `failed_revision` + `failure_code` per revision and has a `failed` state,
  instead of showing "indexing" forever.
- A knowledge base **declares up front whether it is read-only**, and hides the
  upload area entirely when it is.

### 1.4 Web console

React + TypeScript, six pages: **Chat**, **Work** (task timeline and lifecycle),
**Knowledge**, **Approvals**, **Evaluation** and **System**.

### 1.5 Interfaces and tools

**HTTP API** (FastAPI): `/v1/chat` (sessions, messages, SSE), `/v1/tasks`
(submit, query, timeline, cancel, triage), `/v1/knowledge-bases`, `/v1/uploads`,
`/v1/search`, `/v1/approvals`, `/v1/artifacts` (including `/preview`),
`/health/live|ready`.

**CLI**: `agent-cli`, `agent-api`, `agent-task-worker`,
`agent-ingestion-worker`, `agent-config-check`, `agent-evidence`, plus three
project-owned MCP servers: `agent-word-mcp`, `agent-web-mcp`,
`agent-sandbox-mcp`.

**Tools available to Agents**: `knowledge_search`, `web_search`,
`external_search`, `workspace_list/read/write/edit/grep`, `sandbox_run`,
`export_artifact`, and over MCP `mcp_web_fetch_page`,
`mcp_web_download_document`, `mcp_word_render_document`. Which server's tools
reach which Agent is declared by config `audience` (`research` / `synthesis`),
so adding a reader is a config change rather than a code change.

**Observability**: OpenTelemetry traces and metrics (Port + OTLP Adapter; the
core never imports the SDK).

---

## 2. Architecture

### 2.1 Layers and dependency direction

```
┌─ core ───────────────── importing any framework is forbidden (CI-enforced) ─┐
│  runtime/     ClaudeLikeAgentRuntime — Tool Loop, Policy Gateway, budget    │
│               and deadline, cancellation, parallel read-only scheduling     │
│  domain/      invariants encoded in types                                   │
│  workflows/   control flow as a declaration (nodes, edges, reducers)        │
│  application/ use-case orchestration (Chat publication, Task lifecycle)     │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │ depends on
                  ┌────────▼────────┐
                  │     ports/      │  Protocol contracts — the only seam
                  └────────▲────────┘
                           │ implemented by
┌──────────────────────────┴────────────── frameworks live only here ─────────┐
│  langgraph/   compiles the control-flow declaration, never the Tool Loop    │
│  llama_index/ retrieval only, never answer generation                       │
│  mcp/         official SDK v2, frozen into plain ToolBindings at startup    │
│  persistence/ PostgreSQL — sessions, tasks, events, checkpoints, outbox     │
│  vector/      Qdrant — dense / hybrid, with RRF fusion done in-process      │
│  embedding/ reranking/ models/ telemetry/ tools/ …                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

That boundary is a test that **turns CI red**
([`tests/architecture/test_dependency_boundaries.py`](tests/architecture/test_dependency_boundaries.py)),
and it forbids **method calls** as well as imports — the reasoning is in
[the ten-minute version §3.1](docs/HIGHLIGHTS.md).

### 2.2 How one Chat answer flows

```
question ──► idempotent Turn creation (Idempotency-Key; no interleaved active turns)
         ──► retrieval: dense + sparse arms ──► in-process RRF fusion, ordered by (-score, chunk_id)
         ──► PostgreSQL ACL filter          ← authorization happens here
         ──► reranker over authorized candidates (returns scores, not passages)
         ──► top_k ──► rendered to the model ──► answer and citations
         ──► publication gate: re-check source revision + ACL
         ──► committed with assistant history and the Turn's terminal state in ONE transaction
```

The publication gate is the last one: if revocation happens after generation but
before publication, the answer is withheld (`AnswerWithheld`) rather than sent.

### 2.3 How one Task run flows

```
submit (tenant-scoped idempotency key + input fingerprint; the authorization
        envelope is stored with the Task and re-applied on every recovery)
  └─► TaskSubmitted ──► Worker claims with SKIP LOCKED ──► TaskClaimed(epoch)
        └─► LangGraph executes the frozen graph version
              each node: AgentExecutor ──► Tool Gateway (envelope + Policy)
                                       ──► tool execution ──► events + checkpoint
              when approval is needed: interrupt ──► waiting_approval ──► human decides
        └─► crash/timeout ──► lease expires ──► another Worker reclaims with a new epoch
                           ──► resumes from checkpoint rather than starting over
  └─► TaskSucceeded / TaskFailed (explicit terminal states; no "looked successful")
```

**Reliability mechanisms**: execution lease + heartbeat + epoch fencing,
transactional Outbox, a project-owned fenced PostgreSQL checkpointer, retry /
dead-letter, an advisory execution guard, and a per-stream gap-free event
sequence with idempotent `event_key`.

Nodes write under the immutable `ExecutionLease` obtained **at claim time** — not
by asking the Registry for the latest epoch each time, which would let a Worker
that had lost its lease pass the ledger fence using its replacement's epoch.

### 2.4 Technology choices

| Layer | Choice | Boundary |
|---|---|---|
| Agent Runtime | **custom** | Tool Loop, Policy, budget, cancellation — **not outsourced** |
| Workflow control plane | LangGraph | compiles the control-flow declaration; `TaskState` fields are graph channels |
| Retrieval | custom + LlamaIndex adapter | LlamaIndex implements retrieval contracts only; **not enabled by default** |
| Vector store | Qdrant | dense / sparse storage; fusion happens in-process |
| Embedding | BGE-M3 | dense + lexical; **refuses to construct** without weights |
| Reranking | BGE reranker | runs after authorization, returns position-aligned scores |
| Model | DeepSeek (OpenAI-compatible) | streaming; server-side `web_search` adds no second API key |
| Persistence | PostgreSQL 16 + Alembic | sessions, tasks, events, checkpoints, outbox |
| Tool protocol | MCP SDK v2 | Streamable HTTP, frozen into local bindings at startup |
| Frontend | React + TypeScript + Vite | Chat/Work/Knowledge/Approvals/Evaluation/System |
| Observability | OpenTelemetry | Port + OTLP adapter; the core never imports the SDK |

Configuration is a **single schema (currently `1.14`)** validated across domains
at startup: a config that claims a capability the code does not have **fails at
load time** instead of sitting there unread.

---

## 3. Quick start

Prerequisites: Python 3.12 and `uv`.

**Zero-dependency demo** — no database, no network, no API key, byte-for-byte
reproducible output:

```bash
uv run agent-cli demo
```

To watch a policy denial keep the handler from being called at all:

```bash
uv run agent-cli demo --deny
```

**Local gates** — first `uv sync --frozen --group dev --no-editable`, copy
`.env.example` to `.env` and replace the placeholders, then:

```bash
uv run agent-config-check --profile development && uv run ruff format --check . && uv run ruff check . && uv run pyright && uv run pytest
```

**The full local topology** (PostgreSQL, Qdrant, API, workers, console) is in
[Running locally](docs/running-locally.md) (Chinese); the containerized demo is in
[Compose deployment](docs/deployment.md) — the API is mapped only to
`127.0.0.1:8000`.

---

## 4. Boundaries

> [!WARNING]
> **The current Identity Adapter trusts request headers**, so `agent-api` is only
> usable for controlled local development and must not be exposed to a LAN or the
> public internet. The listen address and Compose ports are restricted to
> loopback, but that is a mechanism against accidental exposure, **not
> authentication** ([ADR-044](docs/adr/0044-no-remote-no-production-identity.md)).

Capability status is promoted only along `Planned → Implemented → Tested →
Demonstrated`, and **no promotion happens without linkable test or demo
evidence**. Explicitly incomplete: production authentication and remote
deployment, the RAGAS runner, Langfuse, the CrewAI comparison benchmark, the
dynamic multi-Agent supervisor and agent spawn, Chat history compaction, and
physical cleanup of superseded Qdrant points. LlamaIndex retrieval, MCP, the
sandbox, outward reads and web search are **all off by default**, each for a
stated reason.

The current Compose topology is for local demonstration only and is not evidence
of a production deployment or of production-grade multi-Worker operation.

**Per-item categories, repository locations and "done" criteria are in
[Known gaps](docs/known-gaps.md)**; measured gate numbers and real-run evidence
are in [the ten-minute version](docs/HIGHLIGHTS.md).

### Gates

<!-- Maintenance rule: these numbers are maintained in exactly two places, this
     table and docs/HIGHLIGHTS.md section 2, because a translation cannot link
     its way out of needing the values. HIGHLIGHTS is the source of record:
     update it first, then mirror here in the same commit. -->

These mirror [the ten-minute version, §2](docs/HIGHLIGHTS.md), which is the
source of record. Four environments; they may be cited separately and **must not
be added together** — the two backend environments have overlapping skip sets.
The backend rows were measured at `main@921dda5` (2026-08-12); that hash records
*the tree the measurement ran on*, not "the current baseline".

| Environment | Result |
|---|---|
| Backend, real PostgreSQL + Qdrant (local) | `2758 passed / 11 skipped` |
| Backend, no external services (local) | `2065 passed / 704 skipped` |
| Backend, the CI service-backed directories (`contracts`/`persistence`/`api`/`vector`) | `1012 passed / 2 skipped` |
| Frontend Vitest (CI, 22 files) / Playwright (desktop + mobile, CI) | `171 passed` / `4 passed` |

Static gates all pass: `ruff format --check .` (493 files),
`ruff check src tests`, Pyright strict `0 errors / 0 warnings / 0 informations`,
ESLint `--max-warnings 0`, `tsc -b`, production build. Config schema `1.14`;
single Alembic head `0025_agent_invocation_count` (25 migrations).

Scale: 55,114 lines of Python, 68,952 lines of tests, 15,271 lines of frontend
TypeScript; 45 ADRs (11 in the baseline document plus 34 written during
implementation, numbered 0012–0045 without gaps). **More test code than source
code is deliberate** — the rule is that a test must first be shown red, and **a
test without a control case does not count**.

---

## 5. Documentation

| Document | Purpose |
|---|---|
| [The ten-minute version](docs/HIGHLIGHTS.md) | Real event stream, gate numbers, engineering judgements |
| [Known gaps](docs/known-gaps.md) | **What is not built**, in four categories |
| [Implementation status](docs/status.md) | Implementation and test evidence, PR by PR |
| [Architecture baseline](docs/architecture-baseline.md) | Product boundaries, layering, reliability protocol |
| [Configuration contract](docs/configuration.md) | Config sources, secret rules, snapshot semantics |
| [Running locally](docs/running-locally.md) / [Compose deployment](docs/deployment.md) | How to run it |
| [ADR index](docs/adr/) | 34 decision records (0012–0045) |
| [Full documentation map](docs/README.md) | Layered index and reading paths by role |

Most documentation is written in Chinese; this page and
[Compose deployment](docs/deployment.md) are in English.

---

## License and provenance

Released under the [Apache License 2.0](LICENSE). Retain [NOTICE.md](NOTICE.md)
when using or distributing — Apache-2.0 §4(d) requires it. Dependency licenses
are unaffected by this repository's license; the rules are in
[compliance.md](docs/compliance.md).

This repository is a clean-room implementation; the boundary is described in
[NOTICE.md](NOTICE.md) and the [compliance notes](docs/compliance.md).
