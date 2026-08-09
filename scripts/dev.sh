#!/usr/bin/env bash
# Run the workbench on this machine, against services on localhost.
#
#   scripts/dev.sh services     # start PostgreSQL and Qdrant
#   scripts/dev.sh migrate      # bring the schema to head
#   scripts/dev.sh api          # HTTP control plane (add --without-chat to skip
#                               # the embedding runtime entirely)
#   scripts/dev.sh ingest       # ingestion worker (also bootstraps the index)
#   scripts/dev.sh worker       # Task worker, demo graph
#   scripts/dev.sh word-server  # loopback Word document MCP server
#   scripts/dev.sh word-check   # health + tools/list probe
#   scripts/dev.sh word-api     # API with explicit Word MCP profile
#   scripts/dev.sh word-worker  # real Worker; requires a model provider key
#   scripts/dev.sh smoke        # drive the whole thing and print what happened
#
# This is the one place that knows the local environment. The three DSNs live
# here rather than in the committed TOML because settings forbids connection
# strings in configuration files -- one is a credential even when today's has no
# password. Ordinary commands use config/config.local.toml; the explicit
# word-api/word-worker pair uses config/config.word-local.toml.
#
# Whether chat runs depends on one thing: AW_SECRETS__DEEPSEEK_API_KEY. With it,
# the API serves chat and the Task worker runs the real model-calling graph.
# Without it, the ordinary API omits Chat and the ordinary worker runs `--demo`,
# and both say so rather than pretending. The explicit word-worker is stricter:
# it refuses to start, because a demo graph cannot exercise a Word MCP tool.
#
# The key is never read from a file in this repository and never written to one.
# Export it in your shell, or source it from somewhere outside the checkout.
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

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

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
  # No branch on the key any more. A missing provider costs chat and nothing
  # else -- retrieval assembles without one, so /v1/search is served either way
  # and the process reports what it could not build.
  #
  # `--without-chat` still exists and means something stronger: do not load the
  # embedding runtime at all. That is for a process serving only uploads and
  # tasks, where paying a minute of model loading would buy nothing.
  if [ -n "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ]; then
    echo "model provider configured: chat and search" >&2
  else
    echo "no AW_SECRETS__DEEPSEEK_API_KEY: search without chat" >&2
  fi
  shift   # drop the subcommand; anything after it is the API's own
  exec "$PYTHON" -m agent_workbench.apps.api.main "$@"
  ;;

ingest)
  # Also what creates the Qdrant collection and binds the read alias, under
  # qdrant.allow_local_bootstrap. Run it once before expecting an index.
  exec "$PYTHON" -m agent_workbench.apps.ingestion_worker.main
  ;;

worker)
  # The demo graph answers its own approval gate, so it never interrupts. Only
  # the real handlers reach a human, which is why the walkthrough for that needs
  # a provider.
  if [ -n "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ]; then
    echo "model provider configured: real graph" >&2
    exec "$PYTHON" -m agent_workbench.apps.task_worker.main
  fi
  echo "no AW_SECRETS__DEEPSEEK_API_KEY: demo graph" >&2
  exec "$PYTHON" -m agent_workbench.apps.task_worker.main --demo
  ;;

word-server)
  # The server owns no user path and listens on loopback only. Its tool returns
  # document bytes through MCP; the existing adapter assigns tenant/owner and
  # persists them in ArtifactStore inside the Task Worker process.
  exec "$PYTHON" -m agent_workbench.apps.word_mcp.main
  ;;

word-check)
  exec "$PYTHON" scripts/smoke_word_mcp.py \
    --endpoint "http://127.0.0.1:8765/mcp" \
    --health-url "http://127.0.0.1:8765/health" \
    --expect-tool render_document
  ;;

word-api)
  export AW_CONFIG_FILE=config/config.word-local.toml
  if [ -n "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ]; then
    echo "Word profile selected; provider key is available to real Worker processes" >&2
  else
    echo "Word profile, no provider key: API can submit but no real Word Worker can run" >&2
  fi
  shift
  exec "$PYTHON" -m agent_workbench.apps.api.main "$@"
  ;;

word-worker)
  export AW_CONFIG_FILE=config/config.word-local.toml
  if [ -z "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ]; then
    echo "word-worker requires AW_SECRETS__DEEPSEEK_API_KEY; refusing a demo graph" >&2
    exit 2
  fi
  "$PYTHON" scripts/smoke_word_mcp.py \
    --endpoint "http://127.0.0.1:8765/mcp" \
    --health-url "http://127.0.0.1:8765/health" \
    --expect-tool render_document >&2
  echo "Word profile + model provider configured: real graph" >&2
  exec "$PYTHON" -m agent_workbench.apps.task_worker.main
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
