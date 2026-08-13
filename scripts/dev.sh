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
#   scripts/dev.sh web-server   # loopback read-only web MCP server
#   scripts/dev.sh web-check    # health + tools/list probe
#   scripts/dev.sh web-api      # API with explicit web MCP profile
#   scripts/dev.sh web-worker   # real Worker; requires a model provider key
#   scripts/dev.sh demo-check   # probe both MCP servers at once
#   scripts/dev.sh demo-api     # API with Word *and* web: the console profile
#   scripts/dev.sh demo-worker  # real Worker for that profile; needs both servers
#   scripts/dev.sh smoke        # drive the whole thing and print what happened
#
# This is the one place that knows the local environment. The three DSNs live
# here rather than in the committed TOML because settings forbids connection
# strings in configuration files -- one is a credential even when today's has no
# password. Ordinary commands use config/config.local.toml; the explicit
# word-api/word-worker pair uses config/config.word-local.toml, and the
# web-api/web-worker pair uses config/config.web-local.toml. Those two profiles
# are separate files rather than one: each freezes its own tool names into
# every newly submitted Task envelope, so a combined profile widens every Task
# by both.
#
# demo-api/demo-worker is that combined profile, declared openly as
# config/config.demo-local.toml rather than smuggled into one of the narrow
# ones. It is what the console runs: a person typing "写一份 Word 报告" into Work
# is not choosing a profile, and on the web profile that Task has no renderer in
# its envelope at all.
#
# Whether chat runs depends on one thing: AW_SECRETS__DEEPSEEK_API_KEY. With it,
# the API serves chat and the Task worker runs the real model-calling graph.
# Without it, the ordinary API omits Chat and the ordinary worker runs `--demo`,
# and both say so rather than pretending. The explicit ones are stricter and
# refuse to start: word-worker and web-worker because a demo graph cannot
# exercise an MCP tool, demo-worker for the same reason, and demo-api because a
# keyless console loses Chat, the event stream, and triage without any of the
# three being visible from the browser. Only the console profile refuses: plain
# `api` still starts keyless and serves search, and `api --without-chat` goes
# further and skips the embedding runtime as well.
#
# The key is never read from a file inside this repository and never written to
# one. Export it in your shell, or leave it in a file outside the checkout --
# AW_KEY_FILE, default ~/.config/agent-workbench/key -- which every command here
# reads when the variable is unset. A path outside the working tree is what
# keeps `zip -r` and Finder's "Compress" from carrying a live credential into an
# archive; neither of those honours .gitignore, and the CI secret scan reads
# commit history, where this key has never been.
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

# The one place a provider key is read from disk, and it reads only from outside
# the working tree. An exported variable still wins, so nothing about an existing
# shell changes; what this adds is that the file and the shell are the same key
# for every command below, rather than the key existing on whichever launcher
# happened to know about it. That asymmetry is what this replaces: a wrapper
# elsewhere was the only thing that loaded the key, so a documented `dev.sh
# demo-api` start silently had no provider -- which is precisely the failure the
# demo-api refusal now names.
#
# `-r` and not `-f`: an unreadable key file is the same as no key here, and the
# refusal downstream says so more usefully than a redirect error would. Setting
# AW_KEY_FILE to the empty string means "no file at all" -- hence `-` rather than
# `:-` in the expansion -- which is how the tests that assert a refusal keep
# asserting it on a machine that does have a key sitting in the default place.
AW_KEY_FILE="${AW_KEY_FILE-$HOME/.config/agent-workbench/key}"
if [ -z "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ] && [ -r "$AW_KEY_FILE" ]; then
  AW_SECRETS__DEEPSEEK_API_KEY="$(tr -d '[:space:]' < "$AW_KEY_FILE")"
  export AW_SECRETS__DEEPSEEK_API_KEY
fi

TENANT="${TENANT:-tenant_local}"
PRINCIPAL="${PRINCIPAL:-user_local}"
API_URL="${API_URL:-http://127.0.0.1:8000}"

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }

# Whether every host port a container publishes is bound to loopback.
#
# `docker start` reuses the port bindings the container was *created* with, so
# pinning the interface in `docker run` below does nothing for a container that
# already exists -- and the ones this script created before it was pinned are
# still on 0.0.0.0. Warned about rather than recreated: these containers hold
# the local database in their writable layer, so `docker rm` would take a
# developer's data with it. Saying which command to run is the caller's
# decision to act on.
loopback_only() {
  local bound rest
  # Each binding as `<host-ip>`, angle brackets included, so that the *empty*
  # HostIp Docker records for the bare `-p PORT:PORT` form stays visible as
  # `<>`. That empty value is the whole thing being looked for -- it means
  # every interface -- and a check that read the IPs as bare words would skip
  # it as blank and call the container safe.
  bound=$(docker inspect -f \
    '{{range $port, $binds := .HostConfig.PortBindings}}{{range $binds}}<{{.HostIp}}>{{end}}{{end}}' \
    "$1" 2>/dev/null) || return 0
  [ -n "$bound" ] || return 0
  rest=${bound//<127.0.0.1>/}
  rest=${rest//<::1>/}
  [ -z "$rest" ]
}

warn_if_exposed() {
  loopback_only "$1" && return 0
  cat >&2 <<EOF
warning: container '$1' publishes a port on a non-loopback interface.
         It was created before this script pinned the binding, and 'docker
         start' keeps the old one. Anyone on your network can reach it.
         To re-create it on 127.0.0.1 (this DELETES that container's data):
           docker rm -f $1 && scripts/dev.sh services
EOF
}

case "${1:-}" in
services)
  # 5433, not 5432: this machine runs its own PostgreSQL on the default port,
  # and a container published there is shadowed by it -- the symptom is a
  # confusing `role "agent" does not exist` from a server you did not start.
  #
  # `127.0.0.1:` on every published port, and not merely `PORT:PORT`. Docker's
  # short form binds 0.0.0.0, so the bare form put a password-known PostgreSQL
  # and an unauthenticated Qdrant on every interface this laptop has -- café
  # Wi-Fi included -- while the deployment notes say the local stack is
  # loopback-only. Compose already publishes nothing but the API this way; this
  # script is the path that disagreed with it.
  docker start aw-postgres 2>/dev/null ||
    docker run -d --name aw-postgres -p "127.0.0.1:${PG_PORT}:5432" \
      -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=ci-only \
      -e POSTGRES_DB=agent_workbench_test postgres:16
  docker start aw-qdrant 2>/dev/null ||
    docker run -d --name aw-qdrant -p "127.0.0.1:${QDRANT_PORT}:6333" \
      qdrant/qdrant:v1.12.4
  # Wait for the server, then make the local database if it is not there.
  for _ in $(seq 1 30); do
    docker exec aw-postgres pg_isready -U agent >/dev/null 2>&1 && break
    sleep 1
  done
  docker exec aw-postgres psql -U agent -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${PG_DB}'" | grep -q 1 ||
    docker exec aw-postgres createdb -U agent "${PG_DB}"
  warn_if_exposed aw-postgres
  warn_if_exposed aw-qdrant
  echo "postgres 127.0.0.1:${PG_PORT}/${PG_DB}  qdrant 127.0.0.1:${QDRANT_PORT}"
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
  exec "$PYTHON" scripts/smoke_mcp_server.py \
    --label word \
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
  "$PYTHON" scripts/smoke_mcp_server.py \
    --label word \
    --endpoint "http://127.0.0.1:8765/mcp" \
    --health-url "http://127.0.0.1:8765/health" \
    --expect-tool render_document >&2
  echo "Word profile + model provider configured: real graph" >&2
  exec "$PYTHON" -m agent_workbench.apps.task_worker.main
  ;;

web-server)
  # It reads and never writes: two GETs, both through the resolved-address
  # guard, and no path or ownership field in either contract. Downloaded bytes
  # become an artifact inside the Task Worker process, not here.
  exec "$PYTHON" -m agent_workbench.apps.web_mcp.main
  ;;

web-check)
  exec "$PYTHON" scripts/smoke_mcp_server.py \
    --label web \
    --endpoint "http://127.0.0.1:8767/mcp" \
    --health-url "http://127.0.0.1:8767/health" \
    --expect-tool fetch_page \
    --expect-tool download_document
  ;;

web-api)
  export AW_CONFIG_FILE=config/config.web-local.toml
  if [ -n "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ]; then
    echo "web profile selected; provider key is available to real Worker processes" >&2
  else
    echo "web profile, no provider key: API can submit but no real web Worker can run" >&2
  fi
  shift
  exec "$PYTHON" -m agent_workbench.apps.api.main "$@"
  ;;

web-worker)
  export AW_CONFIG_FILE=config/config.web-local.toml
  if [ -z "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ]; then
    echo "web-worker requires AW_SECRETS__DEEPSEEK_API_KEY; refusing a demo graph" >&2
    exit 2
  fi
  "$PYTHON" scripts/smoke_mcp_server.py \
    --label web \
    --endpoint "http://127.0.0.1:8767/mcp" \
    --health-url "http://127.0.0.1:8767/health" \
    --expect-tool fetch_page \
    --expect-tool download_document >&2
  echo "web profile + model provider configured: real graph" >&2
  exec "$PYTHON" -m agent_workbench.apps.task_worker.main
  ;;

demo-check)
  # Both, in one command, because the console profile is only whole with both.
  # Sequential rather than parallel: the point is to say *which* one is missing,
  # and `set -e` stops at the first failure with that server's own message.
  "$PYTHON" scripts/smoke_mcp_server.py \
    --label word \
    --endpoint "http://127.0.0.1:8765/mcp" \
    --health-url "http://127.0.0.1:8765/health" \
    --expect-tool render_document
  exec "$PYTHON" scripts/smoke_mcp_server.py \
    --label web \
    --endpoint "http://127.0.0.1:8767/mcp" \
    --health-url "http://127.0.0.1:8767/health" \
    --expect-tool fetch_page \
    --expect-tool download_document
  ;;

demo-api)
  export AW_CONFIG_FILE=config/config.demo-local.toml
  # Refused rather than degraded, the same as `demo-worker` above it, and for a
  # sharper reason. Without the key `_assemble_chat` catches
  # `ModelNotConfiguredError` and returns a chat-less API: neither `chat.router`
  # nor `events.router` is mounted, and `triage.enabled` in this profile is left
  # with no model, so every Task submitted from Work falls back to v1. None of
  # that is visible from the console -- `/ui` serves, all six pages render, and
  # Chat draws its empty state exactly as it does on a working start. You find
  # out by asking it something.
  #
  # That silence is the whole argument. An API which cannot answer is not a
  # smaller console, it is a console with its front half removed, and the one
  # place that can still say so is here, before the process replaces this shell.
  # Only this arm refuses: a keyless deployment that indexes and searches is a
  # real thing to want, and `dev.sh api` is how you say you want it.
  if [ -z "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ]; then
    echo "demo-api requires AW_SECRETS__DEEPSEEK_API_KEY; refusing a console without Chat" >&2
    echo "  no key means no chat and no events route, and every Task quietly runs v1" >&2
    echo "  for a keyless API say so: 'dev.sh api', or 'dev.sh api --without-chat'" >&2
    echo "  to skip the embedding runtime too" >&2
    exit 2
  fi
  # Chat's `web_search` exists only when `research` is configured (ADR-021):
  # with no provider the tool is never built, and the model answers
  # "我没有联网查询功能" -- which is true of that deployment and reads to a user
  # like the feature is broken.
  #
  # Set here rather than in config.demo-local.toml because that file is tracked
  # and `research.enabled` without a key is a startup error by design: turning
  # it on in the file would break every keyless checkout. config.local.toml
  # documents this exact escape hatch; this is the console profile applying it
  # for itself, on the one condition that makes it safe -- and the refusal above
  # is now what guarantees that condition holds.
  export AW_RESEARCH__ENABLED=true
  echo "console profile (Word + web + chat search); provider key available" >&2
  shift
  exec "$PYTHON" -m agent_workbench.apps.api.main "$@"
  ;;

demo-worker)
  export AW_CONFIG_FILE=config/config.demo-local.toml
  if [ -z "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ]; then
    echo "demo-worker requires AW_SECRETS__DEEPSEEK_API_KEY; refusing a demo graph" >&2
    exit 2
  fi
  # The Worker's own reason for the same switch: with `research` unconfigured,
  # `external_search` is left out of every Task authorization envelope frozen
  # at submission, so the graph's research node proposes a tool its own
  # envelope denies -- one wasted model turn per Task, ending in
  # `outside_submitted_envelope`.
  export AW_RESEARCH__ENABLED=true
  # Both servers, before the Worker rather than after: MCP discovery happens
  # once at startup and never hot-reloads, so a server started late leaves a
  # Worker that is up, healthy, and missing the tool the whole profile is for.
  "$PYTHON" scripts/smoke_mcp_server.py \
    --label word \
    --endpoint "http://127.0.0.1:8765/mcp" \
    --health-url "http://127.0.0.1:8765/health" \
    --expect-tool render_document >&2
  "$PYTHON" scripts/smoke_mcp_server.py \
    --label web \
    --endpoint "http://127.0.0.1:8767/mcp" \
    --health-url "http://127.0.0.1:8767/health" \
    --expect-tool fetch_page \
    --expect-tool download_document >&2
  echo "console profile + model provider configured: real graph" >&2
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
