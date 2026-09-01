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
services sharing one build context, which this topology has four of, *and* a
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

The default API is a control-plane/local smoke stack. The embedding extra and
model credentials are intentionally absent, so Chat can report unavailable
rather than pretending a real RAG/model deployment exists.

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

## Windows

`scripts/dev.sh` is bash, and the native path it drives wants `uv`, a Python
3.12 and a Node 24. Compose needs none of that, so on Windows the whole stack
has one way in:

```bat
scripts\stack.cmd            :: build, start, wait for healthy, open the console
scripts\stack.cmd down       :: stop and remove
scripts\stack.cmd logs       :: follow
scripts\stack.cmd status     :: what is running
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

Configuration belongs in `AW_*` variables. For a real provider or secret-file
test, create an untracked directory such as `.secrets/`, set
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
