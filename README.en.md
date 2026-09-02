# Agent Workbench

[中文](README.md) | English

A clean-room Agent platform in two product shapes: **Chat** (ACL-checked
knowledge-base Q&A) and **Task** (resumable, human-approvable workflows).

The architecture makes one claim: **the self-built Agent Runtime owns the only
tool loop.** LangGraph, LlamaIndex and MCP enter through Ports/Adapters, each
doing its own part, and none of them takes over the core loop.

| Who you are | Where to start |
|---|---|
| You want the whole project in ten minutes | [**The local architecture panel**](#0-see-the-whole-thing-first) — one command, an offline page, every number computed at build time |
| You want evidence | [**The ten-minute version**](docs/HIGHLIGHTS.md) (Chinese) — a real event stream, gate numbers, four technical judgements |
| You want to understand how the agent actually runs | [Agent Harness](#2-the-agent-harness-what-wraps-one-run) → [Agent Runtime](#3-the-agent-runtime-the-only-tool-loop) → [Tool Gateway](#4-the-tool-gateway-what-one-tool-call-passes-through) |
| You want to run it now | [Quick start](#9-quick-start) — one command, no network, no database |
| You want to know what is **not** done | [**Known gaps**](docs/known-gaps.md) (Chinese) — five categories, each with a location and a "done" criterion |
| You want the design reasoning | [Documentation map](docs/README.md), [architecture baseline](docs/architecture-baseline.md), [ADR index](docs/adr/) |

> Most documents under `docs/` are written in Chinese. This file and the
> repository's code comments are in English; where a link leads to a Chinese
> document it is marked.

---

## 0. See the whole thing first

### 0.1 One command, one offline architecture panel

**macOS / Linux:**

```bash
scripts/dev.sh panel
```

**Windows** (`dev.sh` is bash, so there is no route through it there):

```bat
scripts\panel.cmd
```

Double-clicking it in Explorer works too. It runs from both cmd and PowerShell,
and takes the same arguments in either (`--port 9000`, `--no-open`, `--check`).
To skip the launcher, use `py` rather than `python`:

```bat
py -3 scripts\architecture_panel.py --serve
```

Half the launcher's reason for existing is in that difference: a Windows machine
with **no Python installed** still has a `python.exe` on PATH — the Microsoft
Store's execution alias, which opens a shop and exits. The launcher probes each
candidate by *running* it rather than by asking whether the name resolves, so it
never hands the panel to that stub.

It builds a self-contained HTML page and serves it on `127.0.0.1:8770`.
**No database, no Qdrant, no API key, no network** — it reads the working tree.
Twelve sections:

| Section | What is in it |
|---|---|
| Overview | Scale numbers, the launch commands, the whole-picture layer diagram |
| Layers and guards | What each of the seven layers may depend on, plus the **actual contents** of the core third-party allowlist, the named-refusal table and the model-stream owner list |
| Agent Runtime | The loop diagram, every step of one turn, where each of the five gates lives, every module in `runtime/` |
| Tool Gateway | The four phases, the three answers, every refusal exit |
| The two request paths | Chat and Task, plus sub-agent delegation |
| Workflow graphs | Both graphs' nodes and edges, **drawn from `_STATIC_EDGES` and the compiler's conditional-edge target lists** |
| Module browser | 320 modules, searchable by path, summary or symbol name; each line's summary is the first line of that module's own docstring |
| HTTP surface | 75 endpoints, parsed from the route decorators |
| Tool catalogue | In-process and MCP tools, read from the constant that declares each name |
| Config profiles | Ten profiles, and the 82 invariants written as single-valued `Literal`s |
| Decision records | 87 ADRs, searchable |
| Gates and scale | Test directories, console features, process entry points |

**Every number on that page is counted at build time; not one of them is typed
into it.** That is not fastidiousness: this repository has already been bitten by
a number written beside an unrelated fact — `458/458` survived in `CLAUDE.md` for
months after the suite passed 800, because a number written somewhere else has
nothing that fails when it goes stale. A panel is a far larger surface for that
failure than a paragraph, so it is not allowed a single hand-written figure.

The other half — "the five gates on the loop", "the four phases of the gateway",
the statements that are **about architecture rather than about files** — lives in
`NARRATIVE` inside `scripts/architecture_panel.py`. Every entry there names a
real path and symbol, and:

```bash
uv run python scripts/architecture_panel.py --check
```

fails when something it names stops existing. So the hand-written half cannot rot
quietly either.

**It imports nothing outside the standard library**, so it opens on a machine
that has only Python — no `uv sync`, no virtualenv, none of the repository's
services. That property is maintained deliberately rather than by accident: the
panel is what someone opens when they **do not yet know what the repository
is**, and a first step that requires the environment to be built first puts that
backwards. `tests/deployment/test_architecture_panel.py` guards it, along with
the mistakes that only show up on Windows — path separators, the console code
page, and a batch file's encoding and line endings.

> **An honest note about Windows.** This repository's suite runs on POSIX, so
> those are **assertions about the rules** that make the Windows behaviour hold,
> not a record of a run on Windows. A rule is weaker evidence than a run, and
> the tests say so in as many words.

Other uses: `--build DIR` emits the static page only, `--json` prints the scanned
data (useful for other checks), `--port` changes the port. Call the Python
script directly for those three: both launchers append `--serve` themselves, so
`--build` through one of them builds *and* serves. **The listen address is
hard-coded to `127.0.0.1`** — the page spreads the source tree's docstrings out
for a reader, and `python -m http.server` defaults to every interface.

### 0.2 The thirty-second version

<img src="docs/assets/arch-layers.svg" alt="Agent Workbench layering: web and apps/adapters on the outside, ports as the only seam, and core's runtime/workflows/application/domain knowing no framework" width="100%">

Dependency arrows point **inward, always**. The core knows no framework — that is
not a convention, it is a test that turns CI red
([`tests/architecture/test_dependency_boundaries.py`](tests/architecture/test_dependency_boundaries.py)).

> **Two honest notes.** `workflows` and `application` are **mutually referencing
> neighbours**, not a strict upper/lower pair (each imports the other in three or
> four places); drawing a one-way arrow would be drawing it wrong. And
> `evaluation/` is a self-contained core-side package (it imports only itself),
> off the main chain, so it is not in the picture.

---

## 1. What this is: two product shapes

### 1.1 Chat: knowledge-base Q&A with permission checks

- **Multi-turn conversation**, sessions and messages persisted in PostgreSQL;
  `chat_turns` is the idempotent fact source.
- **Retrieval Q&A**: a fixed two-step retrieval (`chat.retrieval_shape` also
  accepts `agentic` and `routed`; the default is **`fixed`** — only a fixed shape
  is reproducibly evaluable). Answers carry citations, each with a `chunk_id`,
  `document_id` and `document_version`.
- **Authorization runs through the whole path**: candidates are filtered by ACL,
  and the source revision and grant are **re-checked once more** before the answer
  is published; revocation and publication are linearized by a document row lock.
  The reranker runs *after* authorization, so it cannot introduce a passage the
  asker may not read.
- **It says so when it cannot answer**, rather than producing something
  plausible-looking. This is scored separately in the evaluation set.
- **Web fallback**: when the corpus cannot answer, an external search may be
  called (off by default). An answer that used the web **does not count as
  grounded** and is displayed differently.
- **Every turn lists the tools it was authorized to use**, highlighting the ones
  actually called; a tool call shows "name + what this call was about" (e.g.
  `web_search · weather in Beijing today`), and a failure shows the error message,
  not an error code.
- **Streaming** is SSE, resumable by cursor after a disconnect.

### 1.2 Task: recoverable, approvable workflows

Submit a goal; the agent decomposes it, retrieves, works, produces files, and can
stop mid-way to wait for a human.

- **Submission triage**: `POST /v1/tasks/triage` lets the model decide which graph
  to run, asks a human when it cannot, and falls back to a default on failure.
- **Human in the loop**: outward effects such as `export` require approval. The
  graph stops at a LangGraph interrupt, the decision goes to the authoritative
  ledger, and it is re-applied after cross-process recovery.
- **Task workspace**: mutable file names inside one Task, pressed onto immutable
  bytes. Writing a name produces a new manifest, and the manifest is itself an
  artifact — so "which version of the workspace" is an id a checkpoint can hold,
  and a replayed node sees the version it entered with.
- **A single-use sandbox** (off by default): one container per call, files in and
  files out, no network, read-only root, non-root, capabilities dropped, with
  memory, CPU, process-count and wall-clock ceilings.
- **Outward reads only** (off by default): `fetch_page` and `download_document`
  are both GETs, and both pass an address gate applied to the **resolved**
  address — only globally routable addresses pass, and redirects are gated hop by
  hop.
- **Artifact export**: `.docx` and friends go into the ArtifactStore, readable
  (text preview) and downloadable from the console.
- **Sub-agent spawning** (off by default): see
  [§6.3](#63-multi-agent-a-delegation-is-a-run-not-a-new-loop).
- **A full trail**: every tool call leaves `ToolProposed → PermissionResolved →
  ToolStarted → ToolCompleted`, **and a refused call leaves one too** rather than
  disappearing.

### 1.3 Knowledge bases and ingestion

Create a knowledge base → upload files → asynchronous ingestion (parse, chunk,
embed, write to Qdrant) → searchable. PDF, Word, Markdown and plain text.

- Documents are managed by **revision**; re-issues and revocations take effect
  through a revision fence.
- The ingestion worker claims work with PostgreSQL `SKIP LOCKED`, with
  lease/heartbeat/fencing.
- **Ingestion failure is said out loud**: the `documents` table records
  `failed_revision` + `failure_code` per revision, and a document status of
  `failed` exists — rather than showing "indexing" forever.
- A knowledge base **declares up front whether it is read-only**; when it is, the
  upload area is not rendered at all.

### 1.4 Web console

React + TypeScript + Vite. `HashRouter`; all eight page components are
`lazy()`-loaded:

| Route | Page | Notes |
|---|---|---|
| `/chat`, `/chat/:sessionId` | Chat | Sessions, citation review, SSE |
| `/work`, `/work/:taskId` | Tasks | Task timeline and lifecycle |
| `/code/:sessionId?` | Code | Coding sessions and file preview |
| `/knowledge` | Knowledge | Material and uploads |
| `/usage` | Usage | Tokens and money per mode |
| `/evaluation` | Evaluation | Evaluation reports |
| `/computer` | Computer | Screen-control boundary and session panel (ADR-095) |
| `/system` | System | Health and configuration projection |

`/code/:sessionId?` is **one** route with an optional parameter rather than two
sibling routes: written as two, the `/code → /code/:id` navigation on the first
send remounts the page and drops the `running` flag of a turn that is still open.

The avatar in the bottom-left opens **Settings**: local identity, model key,
appearance, usage, health. The model-key section stores a provider key in a file
**outside** the checkout ([ADR-101](docs/adr/0101-the-console-may-hand-over-a-key-it-can-never-read-back.md)),
and **it never comes back** — the endpoint returns four characters and has no
method that returns more. It also reports "stored" and "in use by this process"
as two separate facts: the model client is built once at composition, so a key
stored now takes effect at the next start, and a switch that says "saved" while
nothing changes reads as broken.

What a run did is folded into stages, and expanding one shows the raw events and
payloads — **folding renames, it never drops an event**. When a Task delegated,
an "agents involved" panel appears above the timeline: a tree of who spawned whom
with each run's status and spend, and selecting a row narrows the execution view
below to that one run.

The frontend's **only** network egress is two files under `web/src/api/`
(`client.ts` with 12 `fetch(` call sites, `sessionStream.ts` with one). SSE is
consumed with `fetch` + `response.body.getReader()`; there is not one
`EventSource` in the tree. A `fetch(` anywhere else means that boundary broke.

### 1.5 Interfaces and tools

**HTTP API** (FastAPI, **75 endpoints**): `/v1/chat` (sessions, messages, SSE),
`/v1/tasks` (submit, query, timeline, run tree, cancel, triage),
`/v1/knowledge-bases`, `/v1/uploads`, `/v1/search`, `/v1/approvals`,
`/v1/artifacts` (with `/preview` and `/pdf`), `/v1/projects`, `/v1/code`,
`/v1/usage`, `/v1/computer` (a read-only reverse proxy, ADR-095),
`/v1/evaluation`, `/v1/settings` (the model key, ADR-101),
`/v1/system` (what this deployment did not assemble, ADR-102), `/health/live|ready`. The itemized list is on the
[panel](#0-see-the-whole-thing-first)'s HTTP page, parsed from the route
decorators rather than transcribed.

**Command line**: `agent-cli` (demo and submit), `agent-api`,
`agent-task-worker`, `agent-ingestion-worker`, `agent-config-check`,
`agent-evidence`, plus four project-owned MCP servers: `agent-word-mcp`,
`agent-web-mcp`, `agent-sandbox-mcp`, `agent-computer-mcp` (all loopback-bound).

**Tools available to an agent** (17 in-process): `knowledge_search`,
`web_search`, `external_search`, `workspace_list/read/write/edit/grep`,
`project_list/read/write/edit/grep` (the project directory in a coding session,
ADR-072/074), `project_run` (runs a command on the host — **destructive, shown
before it runs**, ADR-077), `sandbox_run`, `export_artifact`, `delegate_agent`
(spawns a sub-agent, **off by default**); plus, through MCP,
`mcp_web_fetch_page`, `mcp_web_download_document` and
`mcp_word_render_document`.

Which server's tools reach which agent is declared by the config's `audience`
(`research` / `synthesis` / `sandbox` / `delegation`), so adding a reader is a
config change rather than a code change. That indirection is **required**, not
tasteful: an agent with tool names hard-coded into its own static table would ask
the tool gateway for one on a deployment that never installed it, and the gateway
raises on an unregistered name — so a "switch that is off" becomes "a node that
fails on every task".

**Observability**: OpenTelemetry traces and metrics (Port + OTLP Adapter; the
core imports no SDK).

---

## 2. The Agent Harness: what wraps one run

"Only one tool loop" bounds how many **implementations** exist, not how many
levels deep it may be entered. Each layer outside it does exactly one thing, and
every one of them satisfies the same `AgentExecutor` protocol in
[`ports/agent_executor.py`](src/agent_workbench/ports/agent_executor.py) — so
adding a layer changes no caller, and removing one does not either.

<img src="docs/assets/agent-harness.svg" alt="Agent Harness: caller → DelegationScopingExecutor → BudgetedAgentExecutor → BoundedParallelExecutor → ClaudeLikeAgentRuntime → ToolGateway → ToolExecutor → handler" width="100%">

### 2.1 The executor stack

| Layer | Where | The one thing it does |
|---|---|---|
| Caller | Graph node / Chat turn / Code session | Holds an `AgentExecutor` and does not know — or need to know — how many layers are underneath |
| `DelegationScopingExecutor` | [`application/delegation.py`](src/agent_workbench/application/delegation.py) | Enters a delegation scope around every run. It wraps the **executor** rather than the node: whether something may delegate is a property of a *run*, so a caller written later is covered without revisiting this file. A child run's depth is one greater than its parent's precisely because the ContextVar still holds the parent's context at that moment |
| `BudgetedAgentExecutor` | [`workflows/task_handlers.py`](src/agent_workbench/workflows/task_handlers.py) | ADR-040: charge the Task for each agent invocation **before taking a concurrency slot** — the Registry round trip should not happen while holding one. It records only; nothing refuses on the count yet |
| `BoundedParallelExecutor` | same file | How many agent invocations may run at once. Sub-agents get **their own second pool**: sharing the parent's deadlocks — a parent waiting inside a tool call holds its slot the whole time, and the child queues for a slot only the parent's return can free |
| `ClaudeLikeAgentRuntime` | [`runtime/agent_runtime.py`](src/agent_workbench/runtime/agent_runtime.py) | **The loop itself** ([§3](#3-the-agent-runtime-the-only-tool-loop)) |
| `ToolGateway` | [`runtime/tool_gateway.py`](src/agent_workbench/runtime/tool_gateway.py) | The four phases of one tool call ([§4](#4-the-tool-gateway-what-one-tool-call-passes-through)) |
| `ToolExecutor` | [`runtime/tool_executor.py`](src/agent_workbench/runtime/tool_executor.py) | Runs one **already authorized** handler under a timeout with a 5-second heartbeat, and guarantees exactly one `ToolResult` leaves |
| handler | `adapters/tools/` · `adapters/mcp/` | The 17 in-process tools plus whatever MCP brought in. None of them can see any layer above |

Three more implementations satisfy the same protocol: `DeferredExecutor` (a
one-slot holder that cuts the assembly cycle), `ArtifactPersistingExecutor`
(persists a completed text-only outcome without changing the port), and
`FakeAgentExecutor` (the scripted double the zero-dependency demo and the gates
run on).

### 2.2 What the composition root assembles

Assembly happens in `apps/*/composition.py` and `bootstrap/`. Three points are
worth stating:

- **The tool registry is immutable.** A running process does not gain a tool —
  otherwise "which tools were available" would have no answer for an event log
  already written. Revocation is live authorization, taking effect at the next
  decision, not a mutation of this table.
- **`DeferredExecutor` cuts the cycle.** The tool that starts a run has to be in
  the registry the gateway reads; the gateway is constructed into the runtime; and
  the runtime is what the tool needs in order to start anything. Something has to
  be named before it exists — and it is a one-slot holder rather than a closure so
  that the failure when nothing was bound can say who failed to bind it.
- **The MCP catalogue is frozen once, at process start.** A server started after a
  Worker leaves that Worker unable to see it for its whole life — a healthy Worker
  missing the tool it exists for. That is why `demo-worker` probes both servers
  before it starts.

### 2.3 One protocol, so "a delegation is a run" needs no discipline

Because every layer satisfies the same `AgentExecutor`, the delegation tool's
handler receives **the same executor from that same stack**. "A delegation is a
run, not a new loop" is therefore an assembly fact rather than a rule someone has
to remember ([ADR-082](docs/adr/0082-a-delegation-is-a-run-not-a-new-loop.md)).

---

## 3. The Agent Runtime: the only tool loop

`ClaudeLikeAgentRuntime._run` is one `while True`. Its body checks five gates,
streams **exactly one** model call, maps that call onto either a terminal outcome
or a batch of tool calls, takes that batch through the Tool Gateway's four
phases, realigns the results into the model's own call order, and loops.

<img src="docs/assets/agent-runtime-loop.svg" alt="One turn of the Agent Runtime loop: cancellation, budget, context compaction, model stream, terminal mapping, then admission, gateway, scheduling, execution, write-back, and around again" width="100%">

**Two invariants hold on every path**, and the module docstring states them first:

1. Every `tool_call_id` the model was shown ends with **exactly one**
   `ToolResult`. Unknown tool, denied call, handler exception, timeout,
   cancellation mid-batch — each of them produces a result rather than a gap,
   because the model is waiting on that id either way and a missing answer is a
   conversation that can never continue.
2. Results are submitted in the model's own call order even though execution is
   genuinely parallel (`plan_tool_batches` + `asyncio.gather`).

### 3.1 One turn, in order

| # | Step | Where | What happens when it trips |
|---|---|---|---|
| 1 | Cancellation check | `cancellation.cancelled` | Polled **six times** per turn; this is the first |
| 2 | Budget gate, before the turn | `domain/runs.py::halt_reason_for` | `budget_exceeded` plus the matching `StopReason`. `max_tool_calls` is **deliberately not asked here**: a run out of tool calls should still get a turn to write its answer |
| 3 | Context gate | `context_reason_for` (ADR-080) | It asks **how large the last request actually was**, not the cumulative token count — cumulative input grows roughly quadratically with turn count, and judging the window by it judges too early |
| 4 | Compaction, only if 3 tripped | `runtime/compaction.py` (ADR-081) | The head message always survives; the cut is advanced forward to a protocol boundary so a `tool_use` is never split from its result; the summary re-enters as an **assistant** message. If it cannot shorten, the run stops with `stop_reason="context_limit"` |
| 5 | Cancellation, again | — | Placed right after the compaction call: a cancelled summarizer must not be filed as "context limit" |
| 6 | Decide what to advertise | `budget.tool_allowance_spent` | When the allowance is spent the tools come **off** the request rather than staying on it to be refused — a tool the model cannot see is not proposed again and again |
| 7 | Model stream | `_stream_model` → `_consume` | One call, one stream, the whole consumption inside `asyncio.timeout(deadline)`. The only `async for` over a model stream in the repository |
| 8 | Meter the turn | `ledger.usage.merged(...)` | `last_input_tokens` is carried **beside** the cumulative usage, because step 3 asks about the former |
| 9 | Terminal mapping | `_terminal_for_turn` (8 branches) | No tool calls → completed; some → on to a batch |
| 10 | Admission | `gateway.propose` + two circuit breakers | Every proposed call leaves a trail first, **including the ones about to be refused**. Then the tool allowance cuts once; a third identical call is refused |
| 11 | Gateway | `prepare` → `authorize` | [§4](#4-the-tool-gateway-what-one-tool-call-passes-through) |
| 12 | Scheduling | `runtime/tool_scheduler.py` (pure, 70 lines) | Consecutive read calls group, up to 4; write / external / destructive calls are **exclusive groups of one** |
| 13 | Execution | `ToolExecutor` | The timeout is the minimum of the tool's declaration, the run's remaining time and the deployment ceiling; a heartbeat every 5 seconds |
| 14 | Align and write back | `domain/tools.py::align_results` | Refilled in the model's own call order; only **admitted** calls are charged |
| 15 | Circuit breaker settles | `repeat_refusals > 2` | Ends the run — **after** those refusals are written into the messages, so the run terminates still holding the record of what it was told |

Step 10's ordering is worth a look: **the signature counter increments before any
other check**. The reason is in the code — a refusal is cheap for a model (almost
no tokens, no effects at all), so a model re-proposing a refused call would burn
turns to the step ceiling; counting first is what lets the breaker close on the
third try.

### 3.2 The five gates on the loop

| Gate | What it stops | Where |
|---|---|---|
| **Budget** | Steps, tokens, cost, deadline. Three predicates guard three places: before a turn, after this turn's tokens are merged, and before dispatching tools. A budget is a **value**; a request may only narrow it. A cost ceiling with no price table is refused before the first call — an unenforceable ceiling is worse than none | `domain/runs.py` |
| **Deadline** | The inner of "the run deadline" and "the runtime envelope" wins, and the result **remembers which one won**: the former is `budget_exceeded`, the latter a retryable `provider_error`. The model profile's own timeout is deliberately elsewhere — in the adapter, nested inside this bound | `runtime/budgets.py` |
| **Context** | Compaction triggers past window × 0.75. Compaction is itself an ordinary model call (profile `compact`); its tokens and cost are merged **even when it fails** — the provider charged for it — but `steps` does not increase, because the loop did not advance | `runtime/compaction.py` |
| **Cancellation** | Polled six times per turn. On cancellation, prepared calls each become a `cancelled` `ToolResult` — they still owe the model an answer and cannot simply vanish | `agent_runtime.py::_refuse_cancelled` |
| **Duplicate calls** | **Two mechanisms.** A `tool_call_id` repeated within one turn fails the whole run (that is the provider's error, not the model's choice); the same name and arguments a third time across turns is refused, and more than two consecutive refusals ends the run | `agent_runtime.py` |

### 3.3 The state machine and the terminal states

A run's position is governed by a hard-coded transition table; an illegal edge
raises `InvalidStateTransition`:

```
building_context  → model_streaming
model_streaming   → validating_tools | completed
validating_tools  → authorizing | recording_results
authorizing       → executing_tools | recording_results
executing_tools   → recording_results
recording_results → model_streaming | compacting
compacting        → model_streaming
```

Every non-terminal state additionally has edges to `failed` and `cancelled`.
**There are exactly three terminal states** — `completed`, `failed`, `cancelled`
— and nine `StopReason` values: `completed`, `max_steps`, `max_tool_calls`,
`token_budget`, `cost_budget`, `context_limit`, `deadline`, `cancelled`, `error`.
**There is no "looked like it worked".**

Note that `building_context → compacting` is **not** a legal edge, and its being
unreachable is not luck: `context_reason_for` returns `None` while
`last_input_tokens <= 0`, so compaction can only fire from `recording_results`.

### 3.4 The eleven modules in `runtime/`

| Module | Lines | What it owns |
|---|---|---|
| `agent_runtime.py` | 1478 | The loop, the ledger, the five gates, terminal mapping, the compaction call |
| `tool_gateway.py` | 1184 | Everything one tool call is checked and dispatched by |
| `tool_executor.py` | 365 | One handler, its timeout, its heartbeat |
| `compaction.py` | 275 | The half of compaction that **needs no model**: where to cut, what the summarizer sees, how much was saved |
| `schema_validation.py` | 249 | The supported JSON Schema subset (17 keywords, 7 types) and argument validation |
| `hook_bus.py` | 156 | The deployment's own `before_tool` inspection; a timeout or a raise both count as blocked |
| `fake_executor.py` | 141 | The scripted double the zero-dependency demo and the gates reproduce byte-for-byte on |
| `budgets.py` | 136 | Deadline arithmetic: the inner one wins, and it is recorded which |
| `state.py` | 103 | The transition table above |
| `tool_scheduler.py` | 70 | Read-parallel / write-exclusive grouping, pure |
| `__init__.py` | 45 | Exports |

**Exclusivity is not the scheduler's judgement**: `ToolSpec.validate_risk_consistency`
refuses **at construction** to build a non-read spec that claims to be parallel,
and equally a write tool with no permission scope. The scheduler only reads it.

### 3.5 One guard, two shapes

`tests/architecture/test_dependency_boundaries.py::test_the_model_tool_loop_has_exactly_one_owner`
guards the same sentence in two non-overlapping ways:

- **By shape**: it walks the core's AST for an `async for` whose iterator is a
  `.stream(...)` call (or a name bound to one — the runtime keeps the iterator in
  a variable so it can close it) and asserts the result set is **exactly**
  `{"runtime/agent_runtime.py"}`.
- **By vocabulary**: across the whole product tree, every module importing
  `agent_workbench.ports.model` must be in `MODEL_STREAM_OWNERS` (seven entries),
  with a control assertion that `agent_runtime.py` **is** in the observed set, so
  a broken import extractor cannot make an empty scan look clean.

Its docstring states the distinction ADR-082 rests on: **"one tool loop" bounds
how many implementations exist, not how many levels deep it may be entered.**

---

## 4. The Tool Gateway: what one tool call passes through

Native handlers, MCP tools and LangChain tools all arrive as the same
`ToolBinding`, so "may this run, with these arguments, right now" has exactly one
implementation. The default is **deny**, and the basis is the authorization
envelope **frozen at submission**.

<img src="docs/assets/tool-gateway-pipeline.svg" alt="The Tool Gateway's four phases — propose, prepare, authorize, invoke — with each phase's refusal exits and the events it leaves" width="100%">

### 4.1 The four phases

| Phase | What it does | The part worth knowing |
|---|---|---|
| `advertise` | **Once per run**, not once per call | An unregistered name raises `UnknownToolError`; one carrying an `operation_key` (a ledgered effect) raises `PolicyDeniedError` — [ADR-075](docs/adr/0075-a-ledgered-effect-is-issued-not-proposed.md): that kind of tool is **issued** by a node, never put in front of a model to propose |
| ① `propose` | Records the argument byte count and SHA-256 | **Including the calls about to be refused.** A refused call vanishing from the event stream deletes the fact that someone tried |
| ② `prepare` | Resolve binding → arguments ≤ 65,536 bytes → JSON Schema validation → the `before_tool` hooks | If a hook rewrote the arguments, the size and schema checks run **again**; a hook may change arguments only, never the tool name and never the `tool_call_id`. Only a hook exception's **type name** crosses the boundary — a backend's exception message has carried a DSN |
| ③ `authorize` | At most three policy rounds | Each round emits a `PermissionResolved`. "Requires approval" is **sticky**: a later round that forgets to repeat it cannot lift it |
| ③b approval | Hold if there is a gate; refuse if there is not | The gate's answer must land in the legal vocabulary to count — an unrecognised word must not become permission by failing to match `deny`. Timeout, cancellation and a gate that raised are all recorded as `deny`, with a trail |
| ④ `invoke` | Dispatch, with one more step for ledgered tools | See [§4.4](#44-ledgered-effects-authorize-again-one-line-from-the-irreversible-act) |

### 4.2 Three answers, and there is no fourth

`ports/policy.py`'s `PolicyEngine` has one method returning one of three effects:

| Effect | Meaning | What follows |
|---|---|---|
| `allow` | Inside the envelope frozen at submission, with the permission scopes present | On to scheduling and execution; a ledgered tool is asked once more before dispatch |
| `deny` | Unknown tool / outside the submitted envelope / missing a permission scope | Leaves a `PermissionResolved` and a `ToolFailed`. **Which scope is missing** is deliberately not in the `reason_code` |
| `allow_with_modified_input` | The policy rewrote the arguments — a **decision**, not a side effect | Re-validate the rewritten arguments against the schema, then **ask again**. Three rounds without convergence is a refusal |

A rewrite must be re-validated and re-decided, otherwise "rewriting" would be the
one path past both checks.

There is currently one implementation:
`adapters/policy/envelope.py::EnvelopePolicyEngine`, 53 lines, three deny reasons
and one allow. Its docstring states plainly what it does **not** yet do — the full
deny-overrides intersection of envelope, settings policy floor, live ACL and live
registry.

### 4.3 Three layers of narrowing

```
The envelope frozen at submission        ⊇   node profile ∩ envelope       ⊇   sub-agent envelope
task_runs.submitted_authorization_envelope   permitted_tools(profile, …)       child_envelope(parent, …)
re-applied on every resume                   the profile only intersects       the risk ceiling may only drop
```

Every layer is an **intersection**; not one of them can widen. The defaults on
`AuthorizationEnvelope.permits` are deny-shaped too: `allowed_tools=()`,
`max_tool_risk="read"`,
`approval_required_risks=("write","external","destructive")`. It applies
`denied_tools` first (denial wins), then requires membership in `allowed_tools`,
then compares risk — **so raising a tool's risk withdraws it from every historical
Task without rewriting a single envelope.**

### 4.4 Ledgered effects: authorize again, one line from the irreversible act

A tool carrying an `operation_key` takes a longer path:

1. No ledger, no task or no lease epoch → refused outright ("nothing to record
   them against").
2. The `operation_key` is computed from the **final arguments**, never from the
   `tool_call_id` — a resent request has to recognise itself.
3. Record the intent. The same key with different arguments →
   `invalid_tool_input`; a ledger that says this was already done → `tool_failed`,
   and **it will not be performed again**.
4. **A second authorization**, one line from the irreversible act: only `allow`
   with approval no longer required may dispatch. A rewrite here is **not
   applied** — the recorded intent must describe the call that actually happened;
   and an approval required again is not asked again — that would let a flapping
   policy prompt a human twice. Any refusal records the operation as **failed**
   before refusing, because "nothing was dispatched" is itself knowledge.
5. Dispatch, record the result. The errors that cannot answer (timeout,
   cancellation, budget) are marked **for human reconciliation** — for an outward
   write, "no answer" does not mean "no effect".

### 4.5 Two things that fail at construction

- A tool whose `input_schema` uses an unsupported keyword → **the process does not
  start**, rather than that call failing. An unenforceable schema is a check that
  does not exist.
- A binding with an `operation_key` but no ledger → `ValueError`, **naming** the
  tools.

---

## 5. Layers and guards

### 5.1 What each layer is

| Layer | What it is | May depend on | Forbidden (guard in brackets) |
|---|---|---|---|
| **domain**<br/>`domain/` (25 modules) | Encodes "which states must not exist" into the types, so invariants hold by **construction failure** rather than by every caller remembering to check | stdlib, Pydantic, domain itself, **plus `regex`** (`domain/workspace.py` needs a matching engine with a timeout to back `GREP_TIMEOUT_SECONDS`; the stdlib `re` has none) | Any framework or SDK; any I/O; mutability or unknown fields (`DomainModel` is globally `frozen=True, extra="forbid"`); `TaskState` may not grow message logs or framework objects — it has to fit in a graph checkpoint |
| **ports**<br/>`ports/` (38 modules, 48 Protocols) | Uses `typing.Protocol` to separate "what capability is needed" from "who provides it" | domain, stdlib, Pydantic only | Any implementation (no SQL, no HTTP, no vector-store calls here); importing `ports/model.py` is gated by the `MODEL_STREAM_OWNERS` allowlist |
| **runtime**<br/>`runtime/` (11 modules) | The one tool loop: drives a run to a **terminal** outcome with budget, deadline, context, cancellation and repeat-call gates on it | domain + ports only | Importing any framework; **no module — adapters included — may write a second loop consuming a model stream**; treating "allow, pending approval" as allow |
| **workflows**<br/>`workflows/` (10 modules) | Control flow written as a **declaration** that can be read and tested on its own: edges are data, routing is pure functions, and what each agent may see and reach is a fixed table | domain, ports, application | Importing langgraph (compilation lives in `adapters/langgraph/`); widening a profile (`permitted_tools` only intersects, and no argument reverses that); asking the registry for the current epoch mid-run |
| **application**<br/>`application/` (34 modules) | Where one Q&A, one Task and one coding session have their steps, authorization fences and failure handling — depending on nothing but domain and ports | domain, ports, workflows | Importing frameworks; reading `os.environ`; **growing its own tool loop** — running an agent goes through `ports/agent_executor` |
| **adapters**<br/>`adapters/` (22 directories plus two loose modules) | One directory per outside concern, translating each vendor's dialect into the ports' contracts at its own edge | ports, domain, third-party frameworks | Importing langgraph or `workflows` outside `adapters/langgraph`; **LlamaIndex's agent / query_engine / response_synthesizer are banned across the whole source tree**, method calls like `as_query_engine()` included |
| **apps + bootstrap**<br/>`apps/` `bootstrap/` `workers/` | Turns one TOML file into several processes, each handed only its own slice and each able to be falsified at startup | all four core layers + adapters + frameworks | `os.environ` **only inside the bootstrap package**; the `Settings` type may not travel past `projections.py`; connection strings are forbidden in TOML; invariants written as single-valued `Literal`s cannot be changed — that takes an ADR first |
| **web**<br/>`web/src/` | Translates the backend's facts into something a person can check, rather than inventing a second execution model | `web/src/api/` (the only place that goes out), backend HTTP + SSE | Talking to the database or vector store directly (`fetch` appears in exactly two files); dropping events while folding them — the raw payload must stay reachable |

### 5.2 What the guards actually contain

The test that turns CI red is
[`tests/architecture/test_dependency_boundaries.py`](tests/architecture/test_dependency_boundaries.py).
It maintains four tables, and the [panel](#0-see-the-whole-thing-first)'s "layers
and guards" page prints their current contents:

- **The core third-party allowlist** has exactly two entries — **`pydantic`** and
  **`regex`** ([ADR-099](docs/adr/0099-a-denylist-cannot-say-no-to-what-nobody-listed.md))
  — plus the standard library and `agent_workbench` itself. **Everything else
  fails, whether or not anybody thought to ban it.**
- **The named-refusal table** (34 entries: `crewai`, `langchain*`, `langgraph`,
  `llama_index`, `fastapi`, `anthropic`, `docx`, …) does not do the refusing —
  the allowlist already did. It supplies the **diagnostic**: the error says "move
  this integration behind an adapter" rather than a generic boundary complaint. A
  test asserts the two tables never overlap.
- **The method-call guard** is two hard-coded attribute names: `as_query_engine`
  and `as_chat_engine`. They hang off the `VectorStoreIndex` this project does
  build and need no new import, so the import-shaped guards cannot see them. Every
  other guard is import-shaped.
- **The model-stream owners** are an allowlist of seven modules. Outside that list
  you cannot obtain a model stream, and so cannot write a second tool loop.

> The allowlist arrived on 2026-08-31. Before that it was a **denylist**, which is
> how the `regex` above walked in green — it has a good reason, and "has a good
> reason" and "is guarded" are different statements.

### 5.3 Which layer holds which capability

| Capability | Main home | Also involved |
|---|---|---|
| Re-check permission **before the answer ships**; withhold it if access was revoked | application `chat.py::_release` | adapters/persistence (revision + ACL re-checked in the same transaction) |
| Never pass off a guess as grounded, and never leak the sentence that would have to be retracted | application `answer_release.py` | domain (`live_text` is a closed pair) |
| Citations may only point at passages the model **was actually shown** | domain `context.py` + application `citations.py` | — |
| The vector store decides what is found; **PostgreSQL decides who may see it** | application `retrieval.py` | adapters/vector, adapters/persistence, adapters/reranking |
| Dense + sparse arms, fused **exactly once** | adapters `vector/fusion.py` (pure) | application (decides when to call it) |
| A tool call has **exactly one place** it can be stopped | runtime `tool_gateway.py` | ports/policy, ports/hooks |
| Stop by itself when a run takes too long, costs too much, or goes in circles | runtime `budgets.py` | domain (a request may narrow a budget, never widen it) |
| Compact a long conversation, **and say that it was compacted** | runtime `compaction.py` (ADR-081) | ports/model (that summary call is priced and observed like any other) |
| Reads run in parallel; writes and external effects take an exclusive turn | runtime `tool_scheduler.py` (pure) | domain/tools (risk/concurrency agreement checked at construction) |
| **Sub-agent delegation** — a delegation is a run, not a new loop (ADR-082, off by default) | runtime (the same AgentExecutor, one level down) | domain/agents (a child envelope is an intersection), adapters/tools/`delegate.py` |
| Re-sending one request does not perform a real side effect twice | application (idempotency key + input fingerprint) | ports/tool_executions (intent/result ledger) |
| A crashed Task carries on | application `task_recovery.py` (pure, no I/O) | adapters/langgraph (Postgres checkpoint), adapters/persistence (`SKIP LOCKED`, lease, epoch) |
| One Task is never run by two processes at once | ports/task_registry + ports/execution_guard | workflows `execution_scope.py` (the lease comes from claim time and is never re-asked) |
| A human approves the irreversible step, and "who approved" cannot be forged | workflows `approval.py` (one interrupt point) | ports/approvals (the ledger is the only source of truth) |
| Each agent is shown only what it is entitled to | workflows `agent_profiles.py`, the `admits` closed set | domain (authorization envelope) |
| Coding sessions (no turn ledger, not resumable, the product is files) | application `code_session.py` | adapters/filesystem, apps/api |
| Uploaded material becomes searchable | workers/ingestion + adapters/ingestion | adapters/embedding, adapters/vector |
| Outbound reads **judge the resolved address first**, deny by default | adapters `research/` (resolve-then-judge) | — |
| See what a run did, and expand to the raw events | domain/events (durability is a property of the type) | ports/event_log, web `stepGroups.ts` |
| See **who delegated whom, and how far the sub-agent got** | application `run_tree.py` (rebuilt from events, never stored twice) | web `RunPanel.tsx` (ADR-083) |
| A wrong config, or a capability claim the code does not back, **stops the process from starting** | bootstrap `settings.py` cross-domain validation + single-valued `Literal`s | every layer (`agent-config-check` runs three profiles offline) |

---

## 6. The two request paths

### 6.1 One Chat answer

<img src="docs/assets/chat-flow.svg" alt="The Chat path: idempotent turn claim, dense and sparse arms, RRF fusion, PostgreSQL ACL filter, reranking, generation, citation verification, publish fence" width="100%">

**Two positions define what this path is.**

The first is **step 4**: authorization happens at the PostgreSQL filter, and the
reranker runs after it. The ordering is the guarantee — the reranker cannot
introduce text the asker may not read, because it never saw those candidates.

The second is **the second-to-last step**: when a revocation lands after
generation but before publication, the system **withholds the answer**
(`AnswerWithheld`) instead of shipping it. The publish fence re-checks revision
and ACL inside **one transaction**, taking locks in the order session → turn →
documents (sorted by id) → event stream; the answer, the assistant history and
the turn's terminal state commit together.

Three terminal outcomes, each an event: `AnswerCommitted` (grounded, with
citations), `UngroundedAnswerCommitted` (not grounded, and **saying so**), and
`AnswerWithheld`. Two off-path recoveries exist as well: `ChatTurnReaper`
terminalizes expired `running` turns, and `ChatPendingReleaseRecovery` re-drives
turns stuck in `release_pending` row by row, long after the original HTTP client
has gone.

### 6.2 One Task run

<img src="docs/assets/task-flow.svg" alt="The Task path: submission freezes the envelope and graph version, SKIP LOCKED claim takes a lease and epoch, the graph executes, approval interrupts, and after a crash another Worker resumes from the checkpoint under a new epoch" width="100%">

**Two graphs, chosen and frozen at submission** (the nodes and edges below are
what the [panel](#0-see-the-whole-thing-first) draws from `_STATIC_EDGES` and the
compiler's conditional-edge target lists, not what someone transcribed):

| Graph | Node chain | Conditional nodes |
|---|---|---|
| `v1`, the fixed research graph (10 nodes) | `understand → plan → route ⇉ {research_internal ∥ research_external} → synthesize → critic → quality_gate → approval → export` | `route`, `quality_gate`, `approval` |
| `v2_general` (5 nodes) | `understand → work → review →` (`approval`) `→ export` | `review`, `approval` |

- `route`'s router **always returns both branches** — it is a fixed fan-out, not a
  choice. Both branches fan in at `synthesize` through a **sorted union**
  (`merge_refs`), which is therefore commutative, associative and idempotent.
- `quality_gate` is a four-target conditional edge: `approval` / `export` /
  `synthesize` (the revise back-edge) / `END`. The `END` arm means "no report
  wanted" or "out of revisions", and since ADR-060 it is a **success**, not a
  failure. `approval` has two targets, and its `END` arm — a human rejection — is
  the graph's only deliberate terminal failure.
- Both graphs' revise back-edges **share one revision budget**, not one each.
- **Conditional nodes run no agent**: `profile_for()` raises `KeyError` for a pure
  routing node.
- `approval` is the **only interrupt point in either graph**. When
  `workflow.export_requires_approval` is false the gate is **skipped**, not faked —
  no approval row is opened.

**Reliability machinery**: execution lease + heartbeat + epoch fencing, a
transactional outbox, a self-built PostgreSQL checkpointer (with fencing; the
checkpoint itself is versioned and has an upgrade path,
[ADR-100](docs/adr/)), retry / dead-letter, an advisory execution guard, and
per-stream gap-free event sequences with idempotent `event_key`s.

A node writes under the immutable `ExecutionLease` it received **at claim time**,
never by re-asking the registry for the current epoch — otherwise a Worker that
lost its lease would pass the ledger fence using its replacement's epoch, which is
the exact thing the fence exists to stop.

### 6.3 Multi-agent: a delegation is a run, not a new loop

<img src="docs/assets/delegation.svg" alt="Sub-agent delegation: the parent run calls delegate_agent, the child runs on the same AgentExecutor, and the three gates are tool intersection, removing the delegation tool at the depth limit, and an envelope that can only narrow" width="100%">

Since [ADR-082](docs/adr/0082-a-delegation-is-a-run-not-a-new-loop.md), a run may
spawn another run mid-loop. **Off by default**
(`multi_agent.delegation_enabled = false`).

The point is that it is **not** a second executor: the delegation tool's handler
calls **the same** `AgentExecutor` from the stack in
[§2](#2-the-agent-harness-what-wraps-one-run) — what recurses is call depth, not
the number of loops.

The three gates are written into the types rather than left to the caller:

| Gate | How it is done |
|---|---|
| A child cannot reach a tool the parent cannot | `permitted_child_tools` is an **intersection**, with no argument that reverses the direction |
| Recursion stops | At the depth limit the delegation tool is **removed from the child's toolbox** — the grandchild never sees it, rather than a counter being incremented correctly |
| Delegation cannot be used to escape the envelope | `child_envelope` may only **lower** the risk ceiling (to `read` by default); `denied_tools` and approval requirements travel down unchanged |

Sub-agents get **their own second concurrency pool**: sharing the parent's
deadlocks, and the reason is written in `apps/task_worker/composition.py`.
Assembly also refuses at startup a deployment where
`max_children_per_run ** max_delegation_depth > max_agent_invocation_attempts_per_task`
— a configuration that can exhaust itself should not wait for runtime to say so.

A child run writes into the **parent's own event stream** under its own `run_id`.
So "who spawned whom" is rebuilt from events (ADR-083) rather than stored twice:

- `GET /v1/tasks/{id}/runs` — the run tree, for navigation
- `GET /v1/tasks/{id}/timeline?run_id=…` — one run only, an indexed lookup
- The console's **"agents involved"** panel draws that tree, and selecting a row
  narrows the execution view below to that one run

---

## 7. Events: one protocol, four consumers

CLI output, SSE, the audit trail and OpenTelemetry consume **the same events**;
none of them invents its own callback. Events describe what happened, they do
**not** decide where execution stands: the conversation store owns chat history,
the LangGraph checkpointer owns workflow position, and this log owns observation.

**37 event types, of which exactly 3 are transient**: `ModelDelta`,
`ModelThinkingDelta`, `ToolProgress`.

> **Durability is a property of the event type, not a caller's choice**
> (`EVENT_DURABILITY` is a fixed table). A caller cannot promote a token delta
> into the durable log, and cannot demote a terminal state out of it. Per-token
> rows would turn a chat into a write-amplification problem; and **only durable
> events carry a sequence**, so an SSE cursor is `(stream_id, the last durable
> event's sequence)` and a reconnecting client resumes from there.

The four events one tool call leaves:

```
ToolProposed  →  PermissionResolved  →  ToolStarted  →  ToolCompleted / ToolFailed
(every proposed      (one per policy       (at dispatch)     (success or failure,
 call, including      round)                                  with duration_ms)
 the refused ones)
```

**A refused call leaves a trail too**: `refuse()` builds a failed `ToolResult` and
routes it through `_record` like any other, so what it leaves is "how far it got
before being stopped" rather than an absence. When approval is needed,
`PermissionRequested` + `RunPaused` + `ToolApprovalDecided` are inserted —
**including on a timeout**.

`ToolProgress` has two producers with different meanings: the handler's own
`report(message, percent)` (normalized on the way out — empty dropped, over 256
characters cut, percent clamped to 0..100, and never raising back into the
handler), and the executor's 5-second heartbeat — **the heartbeat carries no
percent**, because "elapsed time ÷ declared timeout" looks like a progress
fraction and is not one
([ADR-068](docs/adr/0068-a-running-tool-owes-the-reader-a-sign-of-life.md)).

`record_step_inputs` (ADR-019, off by default) gates three body previews:
`ModelStarted.prompt_preview`, `ToolProposed.argument_preview` and
`ToolCompleted.output_preview`. `ToolCompleted.truncated`, `workspace_writes` and
`project_writes` are deliberately **not** behind that flag — they are structure,
not body.

---

## 8. Processes, configuration and the local topology

<img src="docs/assets/process-topology.svg" alt="Local topology: browser and console, agent-api, two workers, shared PostgreSQL, Qdrant and model provider, and four loopback-bound MCP servers" width="100%">

### 8.1 Configuration is a contract, not a bag of values

One schema (currently **`1.19`**), cross-domain validation at startup: a
capability the config claims but the code does not have **fails at config load**,
rather than sitting there unread.

`config/config.<name>.toml`, selected by `AW_CONFIG_FILE`. **Ten of them**:
`local` (no MCP), `word-local`, `web-local`, `code-local`, `computer-local`,
`sandbox-local`, `demo-local` (the union — what the console runs), plus
`default`/`test`/`production`.

They are **separate files rather than one switch**: each freezes its own tool
names into every newly submitted Task's authorization envelope, so a wider
profile widens **every** Task on that deployment.

`agent-config-check --profile` accepts only three names — `development`, `test`,
`production`; the other seven are checked with `--config config/config.<name>.toml`.

**82 invariants are written as single-valued `Literal`s** in
`bootstrap/settings.py` — for example `registry_backend = "postgresql"`,
`claim_strategy = "skip_locked"`, `runtime.executor = "claude_like"`,
`max_parallel_write_tools = 1`. They have exactly one legal value in the type
system: **changing one is not a config edit, it is an ADR first.** The full list is
on the [panel](#0-see-the-whole-thing-first)'s config page.

`database.dsn`, `guard_dsn` and `listen_dsn` are in `FORBIDDEN_TOML_PATHS` and can
only come from the environment — a connection string is a credential even when
today's has no password. `os.environ` is allowed only inside the `bootstrap`
package.

### 8.2 A tour of the repository

| Directory | What is in it |
|---|---|
| `src/agent_workbench/domain/` | 25 modules. Invariants written into the types |
| `src/agent_workbench/ports/` | 38 modules, 48 Protocols. The only cross-layer seam |
| `src/agent_workbench/runtime/` | 11 modules. **The only tool loop**, and the Tool Gateway |
| `src/agent_workbench/workflows/` | 10 modules. Both graphs, agent profiles, the approval interrupt, the execution-lease scope |
| `src/agent_workbench/application/` | 34 modules. Chat turns, Task lifecycle, coding sessions, crash recovery, the run tree |
| `src/agent_workbench/adapters/` | 22 directories plus two loose modules. One directory per outside concern |
| `src/agent_workbench/apps/` | `agent-api`, three worker/CLI entry points, and four project-owned MCP servers |
| `src/agent_workbench/bootstrap/` | 16 modules. Settings, projections, the factories, startup validation |
| `tests/` | 20 directories. `architecture/` is the one that turns a boundary breach red, `contracts/` is "one contract, every implementation", `e2e/` is the one that kills a Worker and watches it recover |
| `web/src/` | Eight features, eight pages; network egress only in two files under `api/` |
| `config/` | Ten profiles |
| `migrations/` | 32 Alembic revisions, a single head |
| `evals/` | `chat` / `rag` / `triage` gold sets; runners in `scripts/run_*_eval.py` |
| `docs/adr/` | 89 decision records, numbered 0012–0102 (0050 and 0053 reserved but never written) |
| `docs/assets/` | The SVGs in this README; the panel inlines the same files — **one drawing, two readers** |
| `scripts/` | `dev.sh` (the one place that knows this machine; bash), `panel.cmd` (the panel's Windows entry point; ASCII + CRLF), `architecture_panel.py` (the panel itself; standard library only), evaluation and benchmark scripts |

---

## 9. Quick start

Prerequisites: Python 3.12 and `uv`.

**Look at the whole project first** — no database, no network, no key, and no
environment to build beforehand:

```bash
scripts/dev.sh panel          # macOS / Linux
```

```bat
scripts\panel.cmd             :: Windows (or double-click it)
```

**Zero-dependency demo** — byte-for-byte reproducible output:

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

`.env` is not optional: without it the first command above stops at `3 validation
errors for LoadedSettings`, which reads like a broken checkout and is not one.

**The full local topology** (PostgreSQL, Qdrant, API, workers, console) is in
[Running locally](docs/running-locally.md) (Chinese); the containerized demo is in
[Compose deployment](docs/deployment.md) — the API is mapped only to
`127.0.0.1:8000`.

---

## 10. Boundaries

> [!WARNING]
> **The current Identity Adapter trusts request headers**, so `agent-api` is only
> usable for controlled local development and must not be exposed to a LAN or the
> public internet. The listen address and Compose ports are restricted to
> loopback, but that is a mechanism against accidental exposure, **not
> authentication** ([ADR-044](docs/adr/0044-no-remote-no-production-identity.md)).
> The architecture panel binds `127.0.0.1` for the same reason: it spreads the
> source tree's docstrings out for a reader.

Capability status is promoted only along `Planned → Implemented → Tested →
Demonstrated`, and **no promotion happens without linkable test or demo
evidence**. Explicitly incomplete: production authentication and remote
deployment, the RAGAS runner, Langfuse, the CrewAI comparison benchmark, the
dynamic multi-Agent supervisor and inter-agent messaging (mailbox), and physical
cleanup of superseded Qdrant points. **Agent spawn is implemented**
([ADR-082](docs/adr/0082-a-delegation-is-a-run-not-a-new-loop.md)); the
orchestration skeleton is still a graph frozen at submission. LlamaIndex
retrieval, MCP, the sandbox, outward reads, web search and sub-agent delegation
are **all off by default**, each for a stated reason.

The `before_tool` hook boundary belongs here too: the protocol, the bus and the
timeout all exist, and **the repository ships no hook implementation at all** —
it is an extension point for a deployment, not a feature already in use.

The current Compose topology is for local demonstration only and is not evidence
of a production deployment or of production-grade multi-Worker operation.

**Per-item categories, repository locations and "done" criteria are in
[Known gaps](docs/known-gaps.md)** (Chinese); measured gate numbers and real-run
evidence are in [the ten-minute version](docs/HIGHLIGHTS.md) (Chinese).

### Gates

<!-- Maintenance rule: these numbers are maintained in exactly two places, this
     table and docs/HIGHLIGHTS.md section 2, because a translation cannot link
     its way out of needing the values. HIGHLIGHTS is the source of record:
     update it first, then mirror here in the same commit. -->

These mirror [the ten-minute version, §2](docs/HIGHLIGHTS.md), which is the
source of record. Four environments; they may be cited separately and **must not
be added together** — the two backend environments have overlapping skip sets.
The last row was measured **2026-09-01** on batch 67's tree; the rows above it on
`main`, **2026-08-31**. Five sets were taken that day and the differences mean
something:

| Point | Real services | Offline | Five dirs | Frontend |
|---|---|---|---|---|
| `a3619f9` (before the closing scan) | 3981 | 3193 | 1376 | 826 |
| Batch 55 (the ADR-097 wiring) | 3989 | 3201 | 1376 | 826 |
| Batch 56 (the C-05 diagnostic) | 3993 | 3205 | 1376 | 828 |
| Batch 57 (the boundary tests) | 3993 | 3207 | 1376 | 828 |
| Batches 58–63 (the closing pass) | 4013 | 3225 | 1376 | 842 |
| Batch 64 (the boundary guard) | 4017 | 3229 | 1376 | 842 |
| Batch 65 (the last dead symbols) | 4020 | 3232 | 1376 | 842 |
| Batch 66 (the checkpoint migration) | 4032 | 3244 | 1376\* | 842 |
| Batch 67 (the capability report, this table) | **4088** | **3300** | **1398** | **857** |

**Half of the last row's +56 / +56 / +22 / +15 is not its own.** Twelve changes
landed on `main` after this table was last refreshed (ADR-101's stored key, the
Windows launcher, the architecture panel, four batches of frontend styling) without
recording numbers, so that difference is theirs plus this batch's. **This batch's own
share is +21 / +21 / +10 / +4**: ten in `tests/api/test_system_capabilities.py` (two
of them pinning "a keyless stack is not told its index is missing because of the
key"), eight in `tests/config/test_provider_key_probe.py` (four parameterized over
the settings module's own placeholder-prefix table), three more in
`tests/deployment/test_compose.py`, and `SystemPage.test.tsx` going from one test to
five. The five-directory column gains only ten, because just the first of those files
is in those directories.

**The sentences below name batches rather than rows.** They used to say "the last
row" and "the row before it", and both had drifted: when "the +3 after it" was
written, the last row was batch 65, and two rows were added after it without that
sentence moving. A record that refers to itself by ordinal turns one of its own
sentences false every time a row is added.

Batches 58–63's +20 / +18 / 0 / +14 account for themselves: six API-ceiling guards
(`test_api_runtime_ceilings.py`), four config-leaf reader guards
(`test_config_leaves_have_readers.py`), one asserting every ownership owner is an
importable module, seven for the corpus and its digest
(`test_corpus_agrees_with_the_system.py` plus one in `test_runner.py`); the
real-services column carries two more that only run against a real database.
On the frontend, eight for the quick switcher (`navigation.test.ts` — 52 test files
had **zero** coverage of `QUICK_DESTINATIONS` before it) and six for the evaluation
page. **The five-directory row did not move**, because none of these batches added
tests there — whether they pass is covered by the full-suite row above.

Batch 64's +4 is ADR-099's four allowlist guards; batch 65's +3 is the identifier
vocabulary's three; batch 66's +12 / +12 / 0 / 0 is carried by that batch's own
record in `docs/status.md`. The five-directory row was **re-measured both times**,
even though the new tests are not in it: those batches changed
`src/` (two tool specs now import `WORKSPACE_WRITE_SCOPE`, and a duplicate terminal
-state set was removed), and "no new tests there" is a different claim from "nothing
there behaves differently".

> **The real-services column was measured twice and the first one was red, which is
> worth writing down.** The first (40m20s) ran *concurrently* with a 2h30m RAG
> ablation re-run, and
> `tests/apps/test_sandbox_isolation.py::test_the_process_ceiling_holds` reported
> "the sandbox container did not finish within 35 seconds". On an idle machine that
> same test passes in **6.62 seconds** and the whole file is 14 passed. The table
> records the second run (16m13s, 4013 passed / 12 skipped / **0 failed**). This is
> not "re-run until green": these batches did not touch the sandbox at all
> (`git diff 52809db..HEAD --stat | grep -i sandbox` finds nothing), and that
> assertion measures whether a container can report its own process ceiling within
> 35 seconds — starved of CPU, it measures how busy the machine is.

That note records *the tree the measurement ran on*; it is not a promise that "the
current baseline" always matches. **The previous edition of this sentence said `main`
itself, and stopped being true the same day it was written** — not a slip, but another
instance of the thing this section keeps having to correct: a number and its provenance
go stale together, and a stale provenance is the harder one to notice.

| Environment | Result |
|---|---|
| Backend, real PostgreSQL + Qdrant (local, idle machine) | `4088 passed / 12 skipped` (21m29s) |
| Backend, no external services (local) | `3300 passed / 800 skipped` (1m37s) |
| Backend, the CI service-backed directories (`contracts`/`persistence`/`api`/`vector`/`e2e`) | `1398 passed / 2 skipped` (20m34s)\* |
| Frontend Vitest (local, 55 files) | `857 passed` |

These four were measured **2026-09-01** on batch 67's tree, machine idle. The
previous edition (2026-08-31, batch 66) was `4032 / 3244 / 1376 / 842`, and twelve
changes that recorded no numbers sit between the two — the paragraph under the
table above accounts for the difference.

> The frontend row spent an edition outside this table, below the B-13 footnote,
> where it rendered as a stray line rather than a row. It is back in the table.

> \* That row went red twice on the night of 2026-08-31 and the cause is **not
> established** — tracked as B-13. Both times it was the same five tests in
> `test_code_api.py`, and both times the run took 2:27, faster than any passing
> run. It has not reproduced in three subsequent runs of the identical command,
> and **the actual error text was never captured**. The table records the
> passing run; the asterisk is there because writing `1376 passed` unqualified
> for a suite that flakes is exactly the kind of sentence this section exists
> to stop.
>
> **2026-09-01: two more runs on batch 67's tree, both green** (`1396`, `1398`,
> about 20 minutes each). Still no reproduction and still no error text, so B-13
> stays open — two passes are not a fix, they only rule out "red every time".

**The first row used to carry 3 failures; it no longer does, and why they went
is worth writing down.** All three were in
`tests/e2e/test_worker_process_crash_recovery.py`, and the symptom was that the
v1 graph's `approval` node never ran (`approval ran 0 times`) while the Task
still succeeded. Re-measured 2026-08-29: the whole file is **11 passed**.

They were **fixed**, not explained away, and the fix is `174f1f2` of
2026-08-28: `config/config.test.toml` declares no `[workflow]` section, so
`export_requires_approval` fell back to the factory `false` and those Workers
started in a deployment with no approval gate — `approval` could never run, and
every assertion in that file counts it. The fix is one line: the child
environment now carries `AW_WORKFLOW__EXPORT_REQUIRES_APPROVAL=true`. That
file's whole subject is the v1 graph crossing its human gate, so it has to
configure the deployment it means to test rather than inherit one that happens
to have no gate.

**Why it could stay red for two weeks**: `tests/e2e` was in no CI job at the
time, so the explanation recorded against it was neither confirmed nor refuted.
That is fixed too — `014de9e`, the same day, added `tests/e2e` to the
service-backed job, which now runs five directories. The third row above is
measured over those five.

**Noted 2026-08-29, because it is this document's own subject.** This paragraph
previously said the cause was "the earlier record was taken in a shell that did
not set `AGENT_WORKBENCH_TEST_DSN`", and the one after it said `tests/e2e` was
still not in CI. Both were false, and both were written the **day after** the
real cause had been found and fixed: without the DSN those tests skip rather
than fail (they are inside the 800 skips of the second row), so they were red
*with* the services. Whoever wrote it did not read the commit that fixed them
and filled in a plausible-sounding mechanism from a stale impression — which is
the class of error this document keeps having to correct.

**The fourth row is a local number, not a CI one.** An older table
cited a CI number there, because the node `24.14.0` pinned in `engines` cannot be
installed on this machine. The 857 above is also a **local** run, on the same node as the previous edition: the
v24.8.0 kept in the repository's own `var/toolchain`, not the system `26.7.0`. The
previous edition's note about `NODE_OPTIONS=--no-experimental-webstorage` therefore
**does not describe this measurement** — it records the workaround needed when the
system 26.x runs the suite (26.x defines `localStorage` as a global getter evaluating
to `undefined`, and jsdom installs its own only when that global is *absent*). Both
routes pass; do not read that note as how this row was produced. It is still a local
number and must not be cited as a CI one; Playwright was not run this
time, and the old `4 passed` has been dropped rather than left in to pad the
table.

Static gates all pass: `ruff format --check .` (635 files), `ruff check .`,
Pyright strict `0 errors / 0 warnings / 0 informations`, ESLint
`--max-warnings 0`, `tsc -b`, production build. Config schema `1.19`; single
Alembic head `0032_events_stream_run_sequence` (32 migrations).

Scale: 82,002 lines of Python across 324 files, 100,556 lines of tests across 265
files, 51,976 lines of frontend TypeScript across 142 files; 89 files under
`docs/adr/`, numbered 0012–0102 — **with gaps**: 0050 and 0053 were claimed by the
block reservation of 2026-08-13 and have never been written (the last section of
`docs/adr/README.md` records that reservation). This line previously read
"0012–0083 without gaps"; both halves were wrong, and the edition after that
("82 files, 0012–0095") was left behind by ADR-096, the one after that
("83 files, 0012–0096") by ADR-097, the one after that ("84 files, 0012–0097")
by ADR-098, and the one after *that* ("85 files, 0012–0098") by ADR-099 — the
last two on the same evening. **More test code than source code is deliberate** — the rule is that a test must first be shown red, and **a
test without a control case does not count**.

**For a figure computed *now* rather than mirrored**, open the
[architecture panel](#0-see-the-whole-thing-first): it counts modules, lines,
endpoints, tools, ADRs and test functions from the working tree at build time.
It answers a different question from the table above — the table records what a
full run *did*, on a named tree, in a named environment; the panel records what
the tree *is* at this moment. Neither substitutes for the other, and the panel is
the one that cannot go stale.

---

## 11. Technology choices

| Layer | Choice | Responsibility boundary |
|---|---|---|
| Agent Runtime | **Self-built** | The tool loop, policy, budgets, cancellation — **not outsourced** |
| Workflow control plane | LangGraph | Compiles the control-flow declaration; `TaskState` fields are the graph channels |
| Retrieval | Self-built + a LlamaIndex adapter | LlamaIndex only satisfies a retrieval contract, and is **off by default** |
| Vector store | Qdrant | Dense / sparse storage; fusion happens in this process |
| Embedding | BGE-M3 | Dense + lexical; **refuses to construct** without weights |
| Reranking | BGE reranker | Runs after authorization, returns positionally aligned scores |
| Model | DeepSeek (OpenAI-compatible) | Streaming; the server-side `web_search` introduces no second key |
| Persistence | PostgreSQL 16 + Alembic | Sessions, tasks, events, checkpoints, outbox |
| Tool protocol | MCP SDK v2 | Streamable HTTP, frozen into local bindings at startup |
| Frontend | React + TypeScript + Vite | Eight pages; Node 24.x (`engines` pins 24.14.0) |
| Observability | OpenTelemetry | Port + OTLP Adapter; the core imports no SDK |

---

## 12. Documentation

| Document | Purpose |
|---|---|
| **[The local architecture panel](#0-see-the-whole-thing-first)** | `scripts/dev.sh panel` — the whole picture, computed, offline |
| [The ten-minute version](docs/HIGHLIGHTS.md) | A real event stream, gate numbers, technical judgements (Chinese) |
| [Known gaps](docs/known-gaps.md) | **What is not done**, five categories, each with a criterion (Chinese) |
| [Implementation status](docs/status.md) | Per-PR implementation and test evidence (Chinese) |
| [Architecture baseline](docs/architecture-baseline.md) | Product boundary, layering, reliability protocols (Chinese) |
| [Configuration contract](docs/configuration.md) | Config sources, secret rules, snapshot semantics (Chinese) |
| [Running locally](docs/running-locally.md) / [Compose deployment](docs/deployment.md) | How to run it (Chinese) |
| [Frontend design baseline](docs/frontend-design.md) | Frontend structure, protocol boundary, responsive strategy (Chinese) |
| [ADR index](docs/adr/) | 89 implementation-period decision records (0012–0102; 0050 and 0053 reserved but never written) |
| [Full documentation map](docs/README.md) | Layered index and reading paths by role (Chinese) |

---

## License and provenance

Released under the [Apache License 2.0](LICENSE). Keep [NOTICE.md](NOTICE.md)
when using or distributing — Apache-2.0 §4(d) requires it. Dependencies keep
their own licenses, unaffected by this repository's; the rules are in
[compliance.md](docs/compliance.md).

This repository is a clean-room implementation; the boundary is described in
[NOTICE.md](NOTICE.md) and the [compliance note](docs/compliance.md).
