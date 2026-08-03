# Local Compose deployment

The checked-in [compose.yaml](../compose.yaml) is a reproducible **local
development/demo** topology, not a production deployment. It runs PostgreSQL
16 and Qdrant at the same digests used by CI, applies Alembic migrations once,
then starts the API. The lock file is installed into a non-root application
image; no `.env`, secret directory, database data or artifact data is copied
into its build context.

Start the default local stack:

```bash
docker compose up --build
curl http://127.0.0.1:8000/health/ready
```

Only the API is published to the host, and it is mapped to
`127.0.0.1:8000`—never `0.0.0.0`. PostgreSQL and Qdrant have no published
ports. The application itself remains bound to container loopback because the
current identity adapter reads request headers. A small standard-library proxy
is necessary for Docker NAT to reach that loopback process; its host mapping is
still loopback-only.

The default API is a control-plane/local smoke stack. The embedding extra and
model credentials are intentionally absent, so Chat can report unavailable
rather than pretending a real RAG/model deployment exists.

To include deterministic synthetic workers, opt in explicitly:

```bash
docker compose --profile demo up --build
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
