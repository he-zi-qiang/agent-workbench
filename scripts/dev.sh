#!/usr/bin/env bash
# Run the workbench on this machine, against services on localhost.
#
#   scripts/dev.sh services     # start PostgreSQL and Qdrant
#   scripts/dev.sh migrate      # bring the schema to head
#   scripts/dev.sh api          # HTTP control plane, without chat
#   scripts/dev.sh ingest       # ingestion worker (also bootstraps the index)
#   scripts/dev.sh worker       # Task worker, demo graph
#   scripts/dev.sh smoke        # drive the whole thing and print what happened
#
# This is the one place that knows the local environment. The three DSNs live
# here rather than in the committed TOML because settings forbids connection
# strings in configuration files -- one is a credential even when today's has no
# password. Everything else comes from config/config.local.toml.
#
# There is no model provider here, so the API runs `--without-chat` and the Task
# worker runs `--demo`. Both say so rather than pretending: `build_model`
# refuses to start a process whose model it could not call, and that refusal is
# the behaviour worth keeping.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
PG_PORT="${PG_PORT:-5433}"
QDRANT_PORT="${QDRANT_PORT:-6333}"
# A database of its own, never the test one. Sharing them means the suite
# truncates your local data, and -- the way this was actually found -- your
# Worker claims a Task the suite left behind and dies on an artifact that
# was in a temporary directory somebody already deleted.
PG_DB="${PG_DB:-agent_workbench_local}"
DSN="postgresql+asyncpg://agent:ci-only@127.0.0.1:${PG_PORT}/${PG_DB}"

export PYTHONPATH=src
export AW_CONFIG_FILE=config/config.local.toml
export AW_DATABASE__DSN="$DSN"
export AW_DATABASE__GUARD_DSN="$DSN"
export AW_DATABASE__LISTEN_DSN="$DSN"
export AW_ARTIFACT_STORE__LOCAL_ROOT="${AW_ARTIFACT_STORE__LOCAL_ROOT:-./var/artifacts}"

TENANT="${TENANT:-tenant_local}"
PRINCIPAL="${PRINCIPAL:-user_local}"
API_URL="${API_URL:-http://127.0.0.1:8000}"

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
services)
  # 5433, not 5432: this machine runs its own PostgreSQL on the default port,
  # and a container published there is shadowed by it -- the symptom is a
  # confusing `role "agent" does not exist` from a server you did not start.
  docker start aw-postgres 2>/dev/null ||
    docker run -d --name aw-postgres -p "${PG_PORT}:5432" \
      -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=ci-only \
      -e POSTGRES_DB=agent_workbench_test postgres:16
  docker start aw-qdrant 2>/dev/null ||
    docker run -d --name aw-qdrant -p "${QDRANT_PORT}:6333" qdrant/qdrant:v1.12.4
  # Wait for the server, then make the local database if it is not there.
  for _ in $(seq 1 30); do
    docker exec aw-postgres pg_isready -U agent >/dev/null 2>&1 && break
    sleep 1
  done
  docker exec aw-postgres psql -U agent -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${PG_DB}'" | grep -q 1 ||
    docker exec aw-postgres createdb -U agent "${PG_DB}"
  echo "postgres :${PG_PORT}/${PG_DB}  qdrant :${QDRANT_PORT}"
  ;;

migrate)
  exec "$PYTHON" -m alembic upgrade head
  ;;

api)
  exec "$PYTHON" -m agent_workbench.apps.api.main --without-chat
  ;;

ingest)
  # Also what creates the Qdrant collection and binds the read alias, under
  # qdrant.allow_local_bootstrap. Run it once before expecting an index.
  exec "$PYTHON" -m agent_workbench.apps.ingestion_worker.main
  ;;

worker)
  exec "$PYTHON" -m agent_workbench.apps.task_worker.main --demo
  ;;

smoke)
  exec "$PYTHON" scripts/smoke_local.py \
    --api-url "$API_URL" --tenant-id "$TENANT" --principal-id "$PRINCIPAL"
  ;;

*)
  usage
  exit 2
  ;;
esac
