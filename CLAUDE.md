# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A clean-room Agent platform with two product shapes: **Chat** (ACL-checked knowledge-base QA) and **Task** (resumable, human-approvable workflows). The one architectural claim: *the self-built Agent Runtime owns the only tool loop.* LangGraph, LlamaIndex and MCP enter through Ports/Adapters and never take a turn of that loop.

Python 3.12 + `uv` for the backend; React + TypeScript + Vite + pnpm for `web/`. `docs/` is written in Chinese.

## Commands

### Backend gate (a subset of the CI `quality` job)

```bash
cp .env.example .env          # first time only, and the gate's first line fails without it
uv sync --frozen --group dev --no-editable
uv run agent-config-check --profile development && \
  uv run ruff format --check . && uv run ruff check . && \
  uv run pyright && uv run pytest
```

- **`.env` is not optional.** `database.dsn`, `guard_dsn` and `listen_dsn` are in
  `FORBIDDEN_TOML_PATHS`, so they can only come from the environment. Without them
  `agent-config-check` — the *first* command above — stops at
  `3 validation errors for LoadedSettings`, which reads like a broken checkout and is not one.
- **A subset, not a mirror.** The CI job also runs `uv lock --check --offline`, the `test`
  and `production` profiles, the `agent-cli demo` golden diff, `pip-licenses`, and the
  assertion that the `embedding` extra is *absent*. Six steps this line does not have.

- **`pyright` must be run bare.** `include` is pinned to `src` in `pyproject.toml`; passing a path overrides it and produces thousands of false errors from `.venv` and `tests`.
- **Export `NO_PROXY=localhost,127.0.0.1,::1` before `pytest`** on a machine with a system proxy, or `tests/vector` and other loopback suites fail for reasons that have nothing to do with the code. Telemetry export through a proxy also stalls the suite; keep OTLP off locally.
- Single test: `uv run pytest tests/runtime/test_agent_runtime.py::test_name`. `pythonpath = ["src", "."]` is set, so `tests.support.*` helpers import from a fresh process too.

### Service-backed suites

`tests/contracts tests/persistence tests/api tests/vector tests/e2e` skip themselves unless two variables point at real servers — **five directories**, and `tests/e2e` is the one people leave out (it joined the CI line on 2026-08-28, and it is where the killed-Worker recovery tests live). The PostgreSQL harness truncates between scenarios and **refuses any database whose name does not end in `_test`**.

```bash
scripts/dev.sh services   # PostgreSQL on 127.0.0.1:5433, Qdrant on 6333
AGENT_WORKBENCH_TEST_DSN=postgresql+asyncpg://agent:ci-only@127.0.0.1:5433/agent_workbench_test \
AGENT_WORKBENCH_TEST_QDRANT_URL=http://127.0.0.1:6333 \
  uv run pytest tests/contracts tests/persistence tests/api tests/vector tests/e2e
```

Port 5433, not 5432 — a locally installed PostgreSQL usually shadows the container otherwise. These variables sit outside the `AW_` namespace deliberately: settings reject unknown `AW_*` variables.

### Frontend (`web/`)

```bash
pnpm --dir web install --frozen-lockfile
pnpm --dir web check          # lint + typecheck + vitest + build, the CI gate
pnpm --dir web test -- src/app/AppShell.test.tsx   # one file
pnpm --dir web test:e2e       # Playwright, needs `playwright install chromium`
```

Node **24.x** is what `engines` pins (24.14.0), and CI uses it. A working copy can live at `var/toolchain/node`; `.claude/run-web.sh` puts it on PATH.

**26.x needs one flag, not a different Node.** It defines `localStorage` as a global getter that evaluates to `undefined` unless `--localstorage-file` is passed, and jsdom installs its own only when that global is *absent* — so 62 tests fail on `localStorage.clear()`. Export `NODE_OPTIONS=--no-experimental-webstorage` and the suite is green. 22.x red-herrings a few others.

The current count lives in [HIGHLIGHTS.md §2](docs/HIGHLIGHTS.md#2-门禁与规模), not here — this line said `458/458` for months after the suite had passed 800, because a number written beside an unrelated fact has nothing that fails when it goes stale.

### Running it locally

`scripts/dev.sh` is the single place that knows this machine's environment (the three DSNs, the proxy handling, the provider key). Read its header comment before adding a launch path.

**`up` owns the start order, and two parts of it are load-bearing rather than tidy**: `demo-api` probes the sandbox MCP server before it will start, and a Worker freezes its MCP catalogue once at startup. `docs/running-locally.md` used to carry that order in prose and got it wrong — its console list never named `sandbox-server`, so following it exactly made `demo-api` die on a probe the doc did not mention.

```bash
scripts/dev.sh up [--with-retrieval]  # THE ONE COMMAND. Without the flag the
                                  # `embedding` extra is NOT installed and this
                                  # deployment has no retrieval -- invisible from
                                  # the browser, so `up` probes and says so.
                                  # setup, services, migrate, MCP
                                  # servers, API, ingest, worker -- in the order the
                                  # console needs. --plan prints it without starting.
scripts/dev.sh down / status / logs <name>
scripts/dev.sh services           # then: scripts/dev.sh migrate
scripts/dev.sh api                # control plane; --without-chat skips the embedding runtime
scripts/dev.sh ingest             # ingestion worker; ALSO bootstraps the Qdrant collection/alias
scripts/dev.sh worker             # Task worker (demo graph without a provider key)
scripts/dev.sh demo-api / demo-worker   # console profile: Word + web MCP + chat search
                                        # (unless switched off on the System page, ADR-104)
scripts/dev.sh code-api           # Code sessions
scripts/dev.sh smoke              # drive the stack and print what happened
```

Without `ingest` running, uploads sit in `processing` forever and the UI cannot tell that apart from "vectorizing". MCP tool catalogues are frozen **once at process start** — a server started after the Worker leaves a healthy Worker missing the tool it exists for, which is why `demo-worker` probes both servers first.

The provider key is read from `AW_SECRETS__DEEPSEEK_API_KEY`, falling back to `AW_KEY_FILE` (default `~/.config/agent-workbench/key`). It lives **outside the checkout** on purpose — `zip -r` and Finder's Compress ignore `.gitignore`. Package releases with `git archive` only.

### Config profiles

`config/config.<name>.toml`, selected by `AW_CONFIG_FILE`. **Ten of them**: `local` (no MCP), `word-local`, `web-local`, `code-local`, `computer-local`, `sandbox-local`, `demo-local` (the union, what the console runs), plus `default`/`test`/`production`. The profiles are deliberately separate files: each freezes its own tool names into every Task authorization envelope at submission, so a wider profile widens every Task.

`agent-config-check --profile` accepts only three names — `development`, `test`, `production`. The other seven are checked with `--config config/config.<name>.toml`.

## Architecture

### The layering, and the test that enforces it

```
core (no framework imports, ever)     ports/ (Protocol contracts, the only seam)
  runtime/     the tool loop, Policy Gateway, budgets, deadlines, cancellation      ▲
  domain/      invariants written into types (auth envelope, budgets, events)       │
  workflows/   control flow as a declaration (graph nodes, edges, reducers)         │
  application/ use-case orchestration (chat publish, task lifecycle, recovery)      │
                                                                                    │
outer (frameworks live only here): adapters/ apps/ bootstrap/ workers/ _config/ ────┘
```

`tests/architecture/test_dependency_boundaries.py` fails CI on any core module importing a framework. Adding a new integration means editing `OUTER_BOUNDARY_PACKAGES`/`FORBIDDEN_CORE_IMPORTS` consciously, not discovering it leaked in.

The method-call guard is **narrow and worth knowing the shape of**: it is two hard-coded attribute names, `as_query_engine` and `as_chat_engine`, because those hang off the `VectorStoreIndex` this project does build and therefore need no new import. Everything else is import-shaped.

Third-party imports into core are an **allowlist** (`CORE_THIRD_PARTY_ALLOWLIST`, ADR-099): the standard library, `agent_workbench` itself, and exactly `pydantic` (anywhere) and `regex` (only `domain/workspace.py`, for a matching engine with a timeout). Anything else fails, listed or not. `FORBIDDEN_CORE_IMPORTS` stays beside it and names the integrations this project *rejected*, so importing one of them fails with "move it behind an adapter" instead of the general message; a test asserts the two lists never overlap.

A corollary the same test enforces: LlamaIndex's agent executor, query engines and response synthesizers are never imported *anywhere*, adapters included. The tool loop has one owner; the answer has one author.

### Chat request path

Turn created idempotently (`Idempotency-Key`; no interleaved active turns per session) → dense + sparse arms → **RRF fusion in-process**, ordered by `(-score, chunk_id)` → PostgreSQL ACL filter (authorization happens here) → reranker over already-authorized candidates → top_k → model → **publish fence re-checks source revision and ACL** → answer, assistant history and turn terminal state commit in one PostgreSQL transaction. A revocation between generation and publish withholds the answer (`AnswerWithheld`) rather than shipping it.

### Task run path

Submit (tenant-scoped idempotency key + input fingerprint; the authorization envelope is stored with the Task) → `SKIP LOCKED` claim with lease/heartbeat/**epoch fencing** → LangGraph executes the graph version frozen at submission → each node runs through AgentExecutor → Tool Gateway (envelope + Policy) → events + checkpoint. Approvals `interrupt` the graph; the decision goes to the authoritative ledger and is re-applied after cross-process recovery. Crash → lease expiry → another Worker reclaims under a new epoch and resumes from the checkpoint.

Nodes write under the immutable `ExecutionLease` they received **at claim time**, never by re-asking the registry for the current epoch — otherwise a Worker that lost its lease passes the ledger fence using its replacement's epoch.

Two graphs, chosen and frozen at submission: the fixed research graph (`understand → plan → route → research_{internal,external} → synthesize → critic → quality_gate → approval → export`) and `v2_general` (`understand → work → review → (approval) → export`). `POST /v1/tasks/triage` picks between them.

### Configuration is a contract, not a bag of values

Single schema (currently `1.19`), cross-domain validation at startup: a capability the config claims but the code does not have **fails at config load**. `docs/configuration.md` §3 lists the invariants written as single-valued `Literal`s in `bootstrap/settings.py` — PostgreSQL as the fact source, `FOR UPDATE SKIP LOCKED`, fusion owned by the application, the self-built runtime as the only tool loop, telemetry body recording off, and more. **Changing one of those requires an ADR first**, not an environment override.

### Where things live

- `apps/` — `agent-api`, `agent-cli`, `agent-task-worker`, `agent-ingestion-worker`, plus **four** project-owned MCP servers (`word`, `web`, `sandbox`, `computer`). Entry points are in `pyproject.toml` `[project.scripts]`.
- `adapters/` — one directory per outer concern, **22 of them**: `artifacts`, `concurrency`, `documents`, `embedding`, `evaluation`, `filesystem`, `ingestion`, `langgraph`, `llama_index`, `mcp`, `memory` (the in-memory doubles), `models`, `persistence`, `policy`, `reranking`, `research`, `retrieval`, `screen`, `telemetry`, `testing`, `tools`, `vector` — plus two loose modules, `delegation.py` and `events.py`. The list here used to name eleven, and the omissions were not the small ones: `research/` is 1310 lines, `documents/` 835, `filesystem/` 791, `screen/` 773.
- `tests/contracts/` — one contract, every implementation: in-memory and PostgreSQL stores are parameterized through the *same* suite, so a divergence is a failure rather than a production surprise.
- `web/src/features/` — `chat`, `code`, `work`, `knowledge`, `evaluation`, `computer`, `system`, `usage`. **`computer` does read an endpoint** (ADR-095): `routes/computer.py` serves `GET /session` through a read-only reverse proxy, `main.py` mounts it unconditionally, and the page polls it every 4 seconds for the allowlist, the frontmost application and recent actions. This line said the opposite until 2026-08-31, which mattered because it was the premise a reader would use to decide whether to *add* an endpoint for that page.
- `evals/` — `chat`, `rag`, `triage` gold sets; runners in `scripts/run_*_eval.py`. A full RAG ablation takes 30–70 minutes; low CPU during it is MPS working, not a hang.

## Working conventions

- **One ADR per boundary change.** `docs/adr/` (0012–0104, with 0050 and 0053 reserved but never written) records implementation-period decisions: anything altering a fact source, the control plane, the runtime owner, the fusion owner, or recovery semantics. New ADRs continue the numbering; superseded ones say what replaced them.
- **Capability claims only move up the ladder `Planned → Implemented → Tested → Demonstrated`, and never without linkable test or demo evidence.** `docs/status.md` is the per-PR evidence log, `docs/known-gaps.md` the honest list of what is not done. Do not describe a Planned item as working.
- **Comments explain the decision, not the code.** This codebase's comments are unusually long and carry measured numbers, rejected alternatives, and the incident that motivated a line. Match that register when touching commented code; a bare restatement of the syntax is a regression here.
- `ruff` per-file-ignores exist for files that deliberately contain Chinese prose (RUF001) and verbatim OOXML fixtures (E501). Prefer adding a scoped ignore with a reason over rewording user-facing Chinese.
- CI runs on pull requests, pushes to `main` and manual dispatch — **pushing a feature branch does not trigger it**. The `quality` job runs offline and asserts the multi-gigabyte `embedding` extra is *not* installed, so nothing behind it is covered by CI; real-model evidence comes from local runs and must be labelled as such.
- The Identity Adapter trusts request headers only. `agent-api` is loopback-bound local development, not something to expose (ADR-044).
