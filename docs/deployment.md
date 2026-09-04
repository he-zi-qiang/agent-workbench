# Local Compose deployment

The checked-in [compose.yaml](../compose.yaml) is a reproducible **local
development/demo** topology, not a production deployment. It runs PostgreSQL
16 and Qdrant at the same digests used by CI, applies Alembic migrations once,
then starts the API. The lock file is installed into a non-root application
image; no `.env`, secret directory, database data or artifact data is copied
into its build context.

Start the default local stack:

```bash
docker build -t agent-workbench:local .
docker compose up -d --wait
curl http://127.0.0.1:8000/health/ready
```

**Two steps, and the split is not stylistic.** `docker compose build` — so
`compose up --build` too — goes through buildx bake, which sets a gRPC header,
`x-docker-expose-session-sharedkey`, derived from the build context directory's
own name. A non-ASCII name makes that header invalid and the build dies before
a single layer runs, naming neither the path nor the directory:

```
failed to dial gRPC: ... header key "x-docker-expose-session-sharedkey"
contains value with non-printable ASCII characters
```

Measured 2026-09-01 on Docker 29.4.0. It needs both halves — two or more
services sharing one build context, which this topology has eight of today, *and* a
non-ASCII directory name. One service under a non-ASCII name builds; four under
an ASCII name build; four under a non-ASCII name never do, and `COMPOSE_BAKE=false`
does not change it. Plain `docker build` does not take that path.

This matters here rather than in the abstract: this repository's own checkout
lives under `agent工作台`, so the one-step form fails for the person most likely
to run it. Every building service declares `image: agent-workbench:local`, so a
pre-built tag satisfies all four and `up` needs no `--build` at all.

Only the API is published to the host, and it is mapped to
`127.0.0.1:8000`—never `0.0.0.0`. PostgreSQL and Qdrant have no published
ports. The application itself remains bound to container loopback because the
current identity adapter reads request headers. A small standard-library proxy
is necessary for Docker NAT to reach that loopback process; its host mapping is
still loopback-only.

The default API starts without a model credential. Open **Settings → Model
key** in the console to store a DeepSeek key in the dedicated
`provider_key_data` volume, then restart the API with
`docker compose restart api`. The key is never written to the checkout, image,
Compose file or command line. The public `deepseek-chat` model ids are pinned in
the topology so that the restarted API can assemble Direct Chat.

Since ADR-0105 the image carries the embedding extra, and since ADR-0106 the
weights are loaded by one service — `encoder` — that the API, both Workers and
the ingestion worker call over HTTP. The weights themselves live in a named
volume filled once by `weights-init`; `docker/fetch_weights.py` says why they
cannot simply be downloaded on first use.

**A Task Worker on that stack is a v2-only Worker, and needs the matching
submission default.** With no embedding runtime to load, real assembly opens no
Qdrant client, registers only `v2_general`, and logs
`task_worker_grounding_unavailable` with the reason. That is deliberate: v1's
research nodes fall back to plain model calls when handed no research handlers,
which would put model prose in `evidence_refs` and let the report cite it as
retrieved evidence, so a Worker that cannot ground refuses to run v1 at all.

The consequence is on the *other* process. `workflow.graph_version` is what the
API submits with when a client names no shape, and it ships as `v1` — so
without the embedding extra, set it to `v2_general` for the whole deployment:

```toml
[workflow]
graph_version = "v2_general"
```

Leave it at `v1` and nothing errors. Submissions succeed, the Worker claims
them, and each one parks as `waiting_migration` because the version it names is
not registered here — a queue that stops draining for a reason no failure
mentions. The Worker logs `task_worker_default_graph_not_buildable` at startup
when it can see the disagreement, but it can only see the value it was projected
with, not the one the API was.

To include deterministic synthetic workers, opt in explicitly:

```bash
docker build -t agent-workbench:local .
docker compose --profile demo up -d --wait
```

`task-worker`, `task-worker-b` and `ingestion-worker` all receive `--demo`. The
ingestion worker is allowed to bootstrap the disposable local Qdrant collection
only in this profile. None of these commands is a production deployment recipe.

**Two Task Workers, on purpose.** Claim, lease, epoch and fencing only mean
anything under contention: with one Worker every one of those invariants holds
trivially, and a topology shipping one cannot show the part of this system that
took the most work. Neither container pins a worker id — each process mints its
own at startup, so two replicas of one image are already two distinct
claimants, and an id set in the file is an id they could share.

The API serves the browser console from the image at
[http://127.0.0.1:8000/ui/](http://127.0.0.1:8000/ui/), same-origin with the
routes it calls. `agent-api` refuses to start when the directory is missing, so
a broken image fails at startup rather than in somebody's browser.

## What this stack can and cannot do, and where it says so

**The stack says it itself now** (ADR-102). The console's 系统 → 运行状态 page —
and the settings dialog's 运行状态 section — list every capability this API
process assembled, every one it did not, why, and what would change it:

```bash
curl -s -H 'x-tenant-id: t' -H 'x-principal-id: p' \
  http://127.0.0.1:8000/v1/system/capabilities
```

The rows are split into `core` — what this product claims to be — and `optional`,
what it can also be asked to do. That split is the first thing to read: a
missing optional row is a choice, a missing core row is a console with a piece
of its front half removed. What a fresh Compose stack answers today:

| Row | Tier | On this stack | Why |
|---|---|---|---|
| `chat.direct` | core | after a key is saved | the image ships no credential; the settings page writes one to a volume |
| `chat.knowledge_base` | core | after a key is saved | since ADR-0105 the image carries the `embedding` extra and `weights-init` warms the cache before anything serves |
| `knowledge.search` | core | yes | same runtime; `/v1/search` is registered because `retrieval` is no longer `None` |
| `task.submit` | core | yes | the Task service is not optional in any process that serves routes |
| `task.worker` | core | **unknown** | no Worker reports itself to the control plane ([D-08](./known-gaps.md)); with no key saved both containers still fall back to `--demo` |
| `chat.web_search` | optional | after a key is saved | see below — it follows the key rather than a static switch |
| `task.external_search` | optional | follows the same switch | the envelope is frozen at submission from the API's own configuration |
| `task.mcp_tools` | optional | yes | `config.compose-local.toml` declares `word` and `web`, and each Worker starts both as loopback sidecars before it freezes its catalogue |
| `task.sandbox` | optional | yes, once the broker's runtime answers | the `sandbox` service alone holds the Docker socket (ADR-0107); each launcher probes it and switches the sandbox on for that start |
| `code.sessions` | optional | yes, with `sandbox_run` on the same condition | `code.enabled` is on in this profile; its sandbox arm follows the same probe |
| `task.delegation` | optional | yes | `multi_agent.delegation_enabled` is on in this profile |
| `task.triage` | optional | yes | `triage.enabled` is on in this profile, as it is in `config.demo-local.toml` |

**A `--demo` Worker and a real one look identical from the console**, which is
the sharpest of these: a Task submitted there reaches `succeeded` without a
single model call or MCP tool ever having existed. Since ADR-0105 that is the
*keyless* case rather than the only case — `docker/run-task-worker-local.sh`
probes for a key and execs the real Worker when it finds one — but the console
still cannot tell the two apart, so the fallback says so in the container log
and the launcher says so on screen. The capability page says so
in words rather than leaving it to be discovered from a `RunStarted` event's
empty `tool_names`.

### Chat's web search follows the key, and is not set in this file

`research.enabled` without a provider key is a *startup error* by design — it is
the "configuration describes a system that does not exist" defect this project
keeps removing. That makes it the one switch Compose must not set: `true` in
`compose.yaml` would leave a fresh stack unable to start, and the page that
stores the key lives inside the process that refuses to start.

So `docker/run-api-local.sh` decides it per start, by asking the package
(`docker/decide_web_search.py`) whether a usable key is present **and nobody
has decided already**. Save a key on the settings page, restart the API, and
both Chat's `web_search` and the Task envelope's `external_search` are there.
The container prints which way it went.

Since ADR-103 the System page can decide it too: every switch-shaped optional
part (web search, triage, Code sessions, delegation) has a three-position
switch there — on, off, or unspecified — and a choice is written to
`switches.json` beside the key, for the *next* start. A stored choice takes the
decision away from the launcher's probe either way; a stored "on" that meets no
key is held rather than turned into a startup error, and the row says so.
Parts that need a server or another image (MCP tools, the sandbox, retrieval)
say "needs installing" instead of offering a switch nothing here could honour.

The native path makes the same decision with the same probe: since ADR-104
`scripts/dev.sh demo-api` and `demo-worker` run `docker/decide_web_search.py`
too, instead of exporting `AW_RESEARCH__ENABLED=true` unconditionally. So a
switch flipped on the System page means one thing whichever way the console was
started, and the page reports `overridden` only when somebody really did export
a value.

Searches spend that key at the provider, bounded by `research.max_uses` per
turn. To decline them, pass an explicit value — it is left alone, and only an
unset or empty one is decided:

```bash
AW_RESEARCH__ENABLED=false docker compose --profile demo up -d --wait
```

### What it would take to get Word/web MCP and real Workers here

Not shipped, and the reasons are worth having before anybody tries:

* **The MCP servers bind loopback only.** `agent-word-mcp` and `agent-web-mcp`
  take `--host` from a three-value choice list — `127.0.0.1`, `localhost`, `::1`
  — so a server in its own container cannot be reached from the Worker's. The
  two shapes that work are a sidecar process inside the Worker container (the
  pattern `docker/run-api-local.sh` already uses for the proxy) or a proxy in
  front of each server (the pattern that same file uses for the API). Neither is
  a config change; both are a new launcher and a new profile.
* **A real Worker needs the graph the image can register.** Without the
  embedding extra a Worker registers `v2_general` only, and `workflow.graph_version`
  ships as `v1` — so dropping `--demo` without also setting `v2_general` on
  *both* processes parks every Task at `waiting_migration`, as the section above
  describes. `v2_general`'s `work` node does declare the `research` and
  `synthesis` dynamic tool sources, so MCP tools do reach it once they exist.
* **The sandbox joined by topology, not by mount** (ADR-0107). `sandbox_run`
  runs each call in a fresh `--network=none` container, so its server needs a
  daemon — the Docker socket, which ADR-0105 refused to put into services that
  also hold a provider key, a database and every workspace. The `sandbox`
  service is a container that runs `agent-sandbox-mcp` and nothing else: no
  key volume, no artifact volume, no configuration, no database, and it is the
  only service that mounts the socket (a test holds it to *exactly one*). The
  API and the Workers reach it through a TCP tunnel whose both ends are
  loopback (`docker/loopback_proxy.py`), so their own `--host` choice lists,
  the MCP SDK's Host check and the settings validator all keep seeing the
  loopback address they were written for. `config.compose-local.toml` still
  says `code.sandbox_enabled = false`: `SandboxSession.open` is fail-fast, and
  the broker may be pulling its interpreter image, so `docker/run-api-local.sh`
  and `docker/run-task-worker-local.sh` probe its runtime
  (`docker/decide_sandbox.py`) and export `AW_CODE__SANDBOX_ENABLED` /
  `AW_SANDBOX__ENABLED` for that start — the shape web search already has.

## Windows

> **The step-by-step version of this section, in Chinese, is
> [Windows 快速开始](./windows-quickstart.md)** — from a machine with nothing
> installed, including the memory floor, the Hugging Face mirror a
> mainland-China connection needs, and what to do when a step fails. This
> section stays here as the topology's own account of the launcher.

`scripts/dev.sh` is bash, and the native path it drives wants `uv`, a Python
3.12 and a Node 24. Compose needs none of that, so on Windows the whole stack
has one way in:

```bat
scripts\stack.cmd            :: build, start, wait for healthy, open the console
scripts\stack.cmd down       :: stop and remove
scripts\stack.cmd logs       :: follow
scripts\stack.cmd status     :: what is running
scripts\stack.cmd restart    :: restart the API and both Workers, nothing else
```

Double-clicking it in Explorer works. It asks the machine for Docker Desktop
and nothing else, and it separates the two failures that look alike from the
outside — Docker absent, and Docker present with the engine stopped — because
only the first is obvious from what Docker prints.

It runs the two-step build above rather than `up --build`, for the reason given
there: a Windows checkout under a Chinese directory name is the ordinary case
here, and that is exactly the shape the bake path refuses.

It starts `--profile demo`, not the default topology. The default one has no
Task Worker in it, so the console opens on Chat and an empty task list and
shows nothing of claim, lease, epoch or fencing.

**It measures the machine before it builds** (ADR-0105, floors from ADR-0106).
One service here loads the retrieval model set — the `encoder` — and the API,
both Workers and the ingestion worker ask it over HTTP instead of loading their
own copies. The one measurement this repository has is for exactly such a
process, on the native path: about 12 GB of available memory, of which about
6.7 GB is the three model files. The floors the launcher compares against are
12 GB (that figure) and 16 GB (that figure plus a stated *allowance* for the
unmeasured rest: three lean processes, PostgreSQL, Qdrant, the collector). They
are decimal GB because the comparison slices digits off `MemTotal` instead of
going through `set /a`, which is 32-bit signed and overflows on any byte count
above ~2.1 GB. Under the lower floor it stops: the alternative is tens of
minutes of build and weight download followed by `up --wait` timing out in
swap, which reads as "this project does not run". A 32 GB Windows at Docker
Desktop's default (half of RAM) clears the upper floor untouched.
`scripts\stack.cmd anyway` overrides it.

**Computer use is the one part that stays outside the containers** (ADR-0108).
No container can reach the desktop, so `agent-computer-mcp` runs on the host
itself, started by `scripts\computer.cmd` — the only step on the Windows route
that needs a Python, and it asks for it through `uv`. The API reads that
server's read-only `/session` route through a loopback tunnel to
`host.docker.internal:8768`; when nothing listens there, the console's Computer
page says so, exactly as it does on a machine that never started one.

On the first run Chat has no provider yet. Open the identity button at the
bottom of the navigation rail, choose **Model key**, save the key, then run:

```bat
scripts\stack.cmd restart
```

That restarts the sandbox broker, the API and both Workers — the processes
that read configuration (or, for the broker, pick an image) once — and leaves
PostgreSQL, Qdrant, the collector and the encoder running; restarting the
encoder would reload three models for nothing.
The same command is what a flipped switch on the System page needs (ADR-103).
Reload the console after the API is healthy. `scripts\stack.cmd down` leaves
the named key volume intact, and with it `switches.json`; removing volumes
explicitly removes both.

`tests/deployment/test_compose.py` holds the launcher to all of that. As with
the panel's Windows tests, those are **rule assertions checked on POSIX**, not
a run on Windows: each asserts the rule that makes the Windows behaviour hold,
which is weaker than having run it there, and the tests say so.

## Telemetry, and where a trace sample comes from

`observability.otel_enabled` cannot be turned off, and every process has
exported OTLP since the telemetry adapter landed. Until the `otel-collector`
service existed there was nothing at the far end: the configured endpoint named
a host this topology never defined, and — because telemetry deliberately fails
open — the stack looked healthy while recording nothing. The port was wrong
too. The exporter is OTLP over **HTTP**, which posts to `<endpoint>/v1/traces`,
and the default pointed at `4317`, the gRPC port.

The collector is in the default stack rather than the `demo` profile, because
the API exports whether or not anybody opted into synthetic workers. Nothing
`depends_on` it: a collector's problem must never become a run's problem.

It writes what it receives to a volume, which is what makes a trace sample
something you can attach rather than describe:

```bash
docker compose cp otel-collector:/var/lib/otel/traces.jsonl ./traces.jsonl
```

Traces and metrics land in separate files (`traces.jsonl`, `metrics.jsonl`) as
OTLP JSON, one batch per line.

## The evidence manifest

A release gate's manifest records what a claim rests on:

```bash
uv run agent-evidence write --gate m6b \
  --attach test_report=./reports/pytest.txt \
  --attach otel_trace_sample=./traces.jsonl \
  --known-limitation "11 skips need BGE weights"
```

Revisions, fingerprints and the commit are derived; every attachment is stored
with its SHA-256, so the manifest can be re-checked rather than believed:

```bash
uv run agent-evidence verify artifacts/evidence/m6b/manifest.json
```

It refuses an attachment that does not exist or is empty, and it refuses a
dirty working tree unless `--allow-dirty` is passed — in which case the
manifest records itself as provisional. Whatever is not attached is listed
under `missing`, derived from what was, so a gate cannot quietly claim a
completeness it does not have.

## Configuration and secrets

The Compose file uses an internal, passwordless PostgreSQL network only for
this loopback-only local stack. It has no real provider, database, Qdrant or
artifact secret. Do not copy this trust setup into any remotely reachable
environment.

Configuration belongs in `AW_*` variables. The local console can write its
provider key to the Compose-managed `provider_key_data` volume as described
above. For a deployment-managed provider or secret-file test instead, create
an untracked directory such as `.secrets/`, set
`AW_SECRETS_DIR=/run/secrets`, and mount it through an uncommitted Compose
override. Flat secret filenames follow the settings names, for example
`AW_SECRETS__DEEPSEEK_API_KEY`. Do not put secret values in a Dockerfile,
image layer, committed Compose override or command line.

Before bringing the stack up, the static topology can be checked without
building images or starting containers:

```bash
docker compose config --quiet
```

The repository runs the same command in `tests/deployment/test_compose.py`
whenever Docker is installed.
