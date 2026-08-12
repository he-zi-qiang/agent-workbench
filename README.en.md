# Agent Workbench

Agent Workbench is a clean-room portfolio project whose target is a general
Agent platform with two product modes:

- Chat Mode: multi-turn conversation and authorized RAG;
- Task Mode: recoverable LangGraph workflows and controlled multi-Agent work.

The custom Agent runtime remains framework-neutral. LlamaIndex, LangGraph,
LangChain and later comparison adapters stay behind explicit ports.

## Current status

**As of 2026-08-12 the baseline is `main@3c8bc95` (PR #116), configuration schema
`1.14`.** What is *not* built is not described here: it is recorded gap by gap in
[Known gaps](docs/known-gaps.md), each classified as refused, unwired, absent or
misstated, with a repository location and a criterion for what "done" means. The
paragraphs below are the incremental narrative up to PR #87 and keep their
original date.

As of 2026-08-09, `main` carries the Task/HITL/side-effect-ledger baseline, the
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
(PR #87). Two further batches on 2026-08-11 closed places where the
documentation and the code disagreed: the first made the configuration stop
lying, gave `LISTEN/NOTIFY` a consumer, and stopped a poison row from blocking
replay; the second made knowledge bases declare write permission and ingestion
failure out loud, made the Work page admit when the timeline it received has
holes, let a machine without the `embedding` extra run Tasks at all, and
corrected the stale numbers on this page. Current evidence and historical
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
- a knowledge base that says what it is before you act on it: `can_write` is
  computed from the same owner-only rule the server's `require_writable`
  enforces, so a read-only base hides the upload panel entirely rather than
  offering a button that 404s after the whole file has gone up. Ingestion failure
  is now recorded per revision (`failed_revision` + `failure_code`, migration
  `0024`, with a check constraint making half a failure unrepresentable) instead
  of being indistinguishable from "still indexing" forever. What is stored is an
  `ErrorCode`, never the parser's exception text, which would echo the document's
  own bytes back to everyone who can read the base. A transient failure is
  recorded the same way and cleared by the next successful attempt, so a
  dependency blip shows briefly as "indexing failed";
- a Work timeline that admits when it is incomplete: the server already reported
  the positions it could not decode, and the page now anchors each one between
  the two events that did arrive, saying the events are still in the log rather
  than that they were lost;
- a Task Worker that starts without the `embedding` extra and serves `v2_general`
  only, which the composition module had argued for since v2 landed but which was
  unreachable dead code — the projection filled retrieval in unconditionally, so
  the refusal fired on every deployment that lacked the extra. Such a deployment
  must also set `workflow.graph_version` to `v2_general`, since that value is the
  API's submission default; the requirement lives in the deployment docs and a
  startup warning, and CI cannot catch it;
- a local-only Docker Compose topology for PostgreSQL, Qdrant, migrations, the
  API and explicitly opted-in synthetic workers.

Validation measured on 2026-08-12 against baseline `main@3c8bc95`:

| Environment | Result |
|---|---|
| Backend, real PostgreSQL 5433 + Qdrant 6333 (local) | 2758 passed / 11 skipped |
| Backend, no external services at all (local) | 2065 passed / 704 skipped |
| Frontend Vitest (CI) | 171 passed (22 files) |
| Frontend Playwright (desktop and mobile projects, CI) | 4 passed |

The first two rows are local and the last two are CI, because this machine
cannot install the `24.14.0` that `web/package.json` pins in `engines`: under
node 22, jsdom's `Blob` has no `.stream()`, so three `downloadArtifact` cases
throw before reaching the code under test. That is a toolchain fact rather than
a code fact, but it does mean **only CI's frontend numbers count**.

Static gates: `ruff format --check .` passed (493 files), `ruff check src tests`
passed, Pyright strict reported 0 errors, 0 warnings, 0 informations, ESLint
`--max-warnings 0` passed, `tsc -b` passed, and the production build passed.
Alembic reports a single head, `0025_agent_invocation_count`.

Of the 11 backend skips, 10 need the `embedding` extra and local BGE weights and
one is a contract that only holds on PostgreSQL; the extra 693 skips in the
service-less row are all gated on `AGENT_WORKBENCH_TEST_DSN` or
`AGENT_WORKBENCH_TEST_QDRANT_URL` being unset. **Four rows, four environments:
quote them separately, never add them.**

A separate CI job carries real-service evidence on **every PR**: `Migrations,
PostgreSQL and Qdrant-backed stores` runs `alembic upgrade head` and then
`tests/contracts tests/persistence tests/api tests/vector` against a real
PostgreSQL 16 and Qdrant. That same command run locally against real services
gives 1012 passed and 2 skipped, one of which needs the `embedding` extra and
local BGE weights that CI does not install — the count is local, but the command
and the environment gates are the ones CI uses. It does not cover `tests/e2e`,
Task Worker end-to-end, or anything requiring a model provider, so it does not
replace the real Task acceptance run behind ADR-032 recorded in
[the implementation status](docs/status.md).

An earlier version of this page said that job was **not green every time**,
because `test_the_hybrid_and_dense_paths_agree_on_the_tie_break` failed
intermittently on tied retrieval scores. That defect has been fixed and the
original diagnosis was wrong; see the reproducibility note below. The test is now
deterministic.

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
text is a known boundary rather than a bug**. WP15 stages four
(`workspace_edit`, `workspace_grep`, cost and deadline as the governing budget,
[ADR-030](docs/adr/0030-working-nodes-are-governed-by-cost.md)) and five (the
second graph `v2_general`,
[ADR-031](docs/adr/0031-a-second-graph.md)) have both landed since an earlier
version of this page said they had not started.

One cost boundary found by measurement, hit twice: a node that does work does not
fit a ceiling set for "read the input, then answer". An article runs 20–50 KB,
two reads come to roughly 28000 tokens, and the default 16000 of
`multi_agent.max_tokens_per_agent_invocation` stops the run mid-JSON, while the
default `runtime.max_steps=12` stops v2's `work` node before it renders — every
tool succeeded and the node still failed. The defaults are unchanged; only
`config/config.web-local.toml` (120000) and `config/config.word-local.toml`
(120000 plus `max_steps=40`) raise them, with the measurements in the comments.

**The reproducibility gap this page used to list is closed, and the original
diagnosis was wrong.** It said tied retrieval scores had no deterministic order.
[ADR-033](docs/adr/0033-fusion-ranks-are-ours.md) found that the unstable order
was the symptom and the unstable *scores* were the cause: server-side RRF scores
by within-arm rank, and a point tied in both arms gets whatever rank the engine
happened to assign, so the fused score is arbitrary. Ten re-index rounds produced
ten different orders, and the strictly-best point was not first in two of them.
Sorting happens after scoring, so no amount of post-sorting could reach it. The
fix moves that one RRF into this process and orders each arm by
`(-score, chunk_id)` before fusing; `chunk_id` is derived from the chunk, so it
survives a re-index. `tests/vector/test_tied_score_order.py` pins it, with a
control asserting a higher score still outranks a smaller id.

The remaining boundaries are explicit. Physical deletion of stale Qdrant points
and Chat history compaction are not done. EventLog upcasters and poison-row
isolation are implemented, but the production upcaster registry is still empty
and only the Work timeline surfaces skipped positions — Chat's
`stream.quarantined` frame still only advances the cursor and shows nothing. Not
started at all: the CrewAI comparison, a Task/multi-Agent benchmark runner,
dynamic multi-Agent supervision and agent spawning, a durable mailbox, general
Tool-level dynamic approval, Langfuse, production identity, and production
deployment. Remote object storage is the same story: `artifact_store.backend`
accepts `s3`, but the only adapter is `LocalArtifactStore` and all three
composition sites refuse to start and say so — fail closed, not a capability.

The console has boundaries worth naming: Chat sessions live only in the browser
(the sidebar's accessible name is literally "local Chat sessions"), with no
server-side list, rename or delete; knowledge bases can be created and uploaded
to, but not renamed, deleted, re-indexed or ACL-managed; a file chosen next to
the message box goes into the selected knowledge base permanently, because
per-message ephemeral attachments do not exist in this system; and `.docx` can be
read as extracted text but not edited.

LlamaIndex is the selected primary RAG integration and RAGAS the offline
evaluation baseline
([ADR-017](docs/adr/0017-llamaindex-primary-rag.md)). The retrieval adapter is
built and contract-tested, but `rag.llama_index.enabled` is `false`, and the
reason has changed: the equivalence measurement ADR-017 step 3 requires came back
inconclusive because each retriever disagreed with *itself* on 9–10 of 38 gold
questions. That noise floor is gone as of ADR-033, but **the evaluation has not
been re-run on the reproducible retriever**, so what is missing now is the
evidence rather than the path to it. Ingestion is not migrated — the LlamaIndex
vector-store adapter explicitly refuses writes — and the RAGAS runner does not
exist, so both stay Planned in the capability table: an adapter existing is not
the finished framework integration.

The evidence manifest (`agent-evidence write`) has been produced for real once,
recording the commit, a dirty flag, the config schema version, the policy
fingerprint and model identities, attaching a test report with its SHA-256, and
listing what it is still missing. **It is a local artefact, not part of the
repository**: `artifacts/evidence/` is gitignored, so a fresh clone has none, and
the one that exists is anchored to the commit that produced it and is now out of
date. Regenerating it means really running an evaluation round.

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
