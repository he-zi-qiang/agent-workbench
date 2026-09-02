#!/usr/bin/env bash
# Run the workbench on this machine, against services on localhost.
#
#   scripts/dev.sh up               # THE ONE COMMAND: everything, in order
#   scripts/dev.sh up --with-retrieval   # ...including the real embedder (GBs)
#   scripts/dev.sh down             # stop what `up` started (containers stay)
#   scripts/dev.sh status           # what is running, and where its log is
#   scripts/dev.sh logs <name>      # follow one of those logs
#
# Everything below is a piece of `up`. Run them by hand to watch one part, or
# when you want a shape `up` does not offer.
#
#   scripts/dev.sh services         # start PostgreSQL and Qdrant
#   scripts/dev.sh migrate          # bring the schema to head
#   scripts/dev.sh api              # HTTP control plane (add --without-chat to skip
#                                   # the embedding runtime entirely)
#   scripts/dev.sh ingest           # ingestion worker (also bootstraps the index)
#   scripts/dev.sh worker           # Task worker, demo graph
#   scripts/dev.sh word-server      # loopback Word document MCP server
#   scripts/dev.sh word-check       # health + tools/list probe
#   scripts/dev.sh word-api         # API with explicit Word MCP profile
#   scripts/dev.sh word-worker      # real Worker; requires a model provider key
#   scripts/dev.sh web-server       # loopback read-only web MCP server
#   scripts/dev.sh web-check        # health + tools/list probe
#   scripts/dev.sh web-api          # API with explicit web MCP profile
#   scripts/dev.sh web-worker       # real Worker; requires a model provider key
#   scripts/dev.sh sandbox-image    # build the sandbox image that can draw a PDF
#   scripts/dev.sh computer-server  # loopback screen-control MCP server (macOS only)
#   scripts/dev.sh computer-check   # health + tools/list probe
#   scripts/dev.sh code-api         # API with Code sessions on; requires a key
#   scripts/dev.sh demo-check       # probe both MCP servers at once
#   scripts/dev.sh demo-api         # API with Word *and* web *and* Code: the console
#   scripts/dev.sh demo-worker      # real Worker for that profile; needs both servers
#   scripts/dev.sh smoke            # drive the whole thing and print what happened
#   scripts/dev.sh panel            # architecture panel on 127.0.0.1:8770 (offline)
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
# The locally built sandbox image, and the reason it is local rather than a
# name on a registry: `docker/sandbox-pdf.Dockerfile` is two packages on a
# digest-pinned base, and publishing it would create a supply-chain artifact
# this project would then owe somebody maintenance on. `sandbox-server` uses it
# only when `docker image inspect` finds it, so an unbuilt one costs a line of
# output rather than a broken call.
SANDBOX_PDF_IMAGE="${SANDBOX_PDF_IMAGE:-agent-workbench-sandbox-pdf:local}"
# A database of its own, never the test one. Sharing them means the suite
# truncates your local data, and -- the way this was actually found -- your
# Worker claims a Task the suite left behind and dies on an artifact that
# was in a temporary directory somebody already deleted.
PG_DB="${PG_DB:-agent_workbench_local}"
DSN="postgresql+asyncpg://agent:ci-only@127.0.0.1:${PG_PORT}/${PG_DB}"

# Proxy, and why both halves are set together.
#
# The web-search guard decides whether to resolve a hostname or to judge it by
# name, and it decides from `urllib.request.getproxies()` -- the same call httpx
# makes. On macOS that function falls back to System Configuration **only when
# no proxy variable is set at all**. So exporting NO_PROXY on its own, which is
# what every command here needs (Qdrant and the API itself are on loopback and
# must not go through a proxy), silently switches the lookup off: the guard then
# resolves, a fake-IP proxy hands back 198.18.0.0/15, and every web search is
# refused with `refused=N` and no clue why. Measured: five of five pages refused
# on a weather question, and the answer read "搜索结果暂时无法获取".
#
# So: if the shell has no proxy set and the system does, carry the system one
# forward explicitly. An exported variable still wins, so anyone with their own
# setup is untouched. `scutil` rather than `networksetup` because it answers for
# whichever service is active, and its absence is not an error -- a machine with
# no proxy simply gets nothing here.
if [ -z "${HTTPS_PROXY:-}${https_proxy:-}" ] && command -v scutil >/dev/null 2>&1; then
  _system_proxy="$(
    scutil --proxy 2>/dev/null | awk '
      /HTTPSEnable/ { enabled = $3 }
      /HTTPSProxy/  { host = $3 }
      /HTTPSPort/   { port = $3 }
      END { if (enabled == 1 && host != "") print "http://" host ":" port }
    '
  )"
  if [ -n "$_system_proxy" ]; then
    export HTTPS_PROXY="$_system_proxy"
    export HTTP_PROXY="$_system_proxy"
    echo "carrying the system proxy forward: $_system_proxy" >&2
  fi
  unset _system_proxy
fi
# Loopback never goes through it. Set after the block above on purpose: setting
# it first is what suppresses the lookup that block depends on.
export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,::1}"
export no_proxy="$NO_PROXY"

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

usage() { sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'; }

# ---------------------------------------------------------------------------
# `up`, and why one command exists on top of the twenty below it.
#
# The console needs six processes started in an order that is not guessable and
# not forgiving: three MCP servers, then the API, then the ingestion worker,
# then the Task worker. Two of those orderings are load-bearing rather than
# tidy -- `demo-api` probes the sandbox server before it will start, and an MCP
# catalogue is frozen once at Worker startup, so a server that comes up late
# leaves a Worker that is healthy and missing the tool the profile exists for.
#
# Written down, that was six terminals and a paragraph of prose, and
# `docs/running-locally.md` got it wrong: its console list named `word-server`
# and `web-server` and never `sandbox-server`, so following it exactly made
# `demo-api` fail on a probe the doc did not mention. Windows has had one
# command since `scripts\stack.cmd`; this is the same idea for the native path,
# and the ordering now lives in code that runs rather than in a list a reader
# has to keep.
#
# It is a launcher, not a supervisor: nothing restarts a process that dies, and
# `var/run/*.pid` is a best-effort record (a PID can be recycled). `status`
# says what it sees, `logs` hands you the file, and both are honest about only
# knowing what `up` itself started.
RUN_DIR="${AW_RUN_DIR:-var/run}"
LOG_DIR="${AW_LOG_DIR:-var/log}"
# Mirrors `[api] port` in config.default.toml. It is not a flag on the process
# -- the port comes from settings -- so this is only used to tell "already
# serving" apart from "about to fail to bind", which is the difference between
# a sentence and a stack trace.
API_PORT="${AW_API_PORT:-8000}"

_pidfile() { printf '%s/%s.pid' "$RUN_DIR" "$1"; }
_logfile() { printf '%s/%s.log' "$LOG_DIR" "$1"; }

# Alive means: we wrote a pid, something answers to it, **and what answers
# looks like one of ours**. The last clause is not caution for its own sake.
# A pid file outlives the process it names -- a machine that was rebooted, or a
# `kill -9` that skipped the cleanup -- and pids get recycled, so without it
# `down` would send TERM to whatever unrelated program inherited the number.
# The first version of this function omitted the check and the comment beside
# it claimed the protection anyway; the comment was wrong, which is worse than
# the omission.
#
# Ownership is read off the command line, and it accepts two shapes because a
# start passes through both: `bash …/dev.sh <arm>` for the moment before the
# arm `exec`s, and `…/python -m agent_workbench.…` for every moment after.
# A process of this project started by hand also matches, and that is the
# honest limit of what a pid file plus `ps` can tell apart.
_is_ours() {
  local command
  command=$(ps -p "$1" -o command= 2>/dev/null) || return 1
  case "$command" in
    *agent_workbench*|*dev.sh*) return 0 ;;
    *) return 1 ;;
  esac
}

_running() {
  local file pid
  file=$(_pidfile "$1")
  [ -f "$file" ] || return 1
  pid=$(cat "$file" 2>/dev/null) || return 1
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  _is_ours "$pid"
}

_port_busy() {
  # bash's own /dev/tcp rather than lsof or nc: both are optional on a stock
  # macOS or a slim Linux, and this check must never be the thing that fails.
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null || return 1
  exec 3<&- 3>&-
  return 0
}

_start() {
  # _start <name> <dev.sh arm> [args...]
  local name=$1
  shift
  if _running "$name"; then
    echo "  $name already running (pid $(cat "$(_pidfile "$name")"))" >&2
    return 0
  fi
  mkdir -p "$RUN_DIR" "$LOG_DIR"
  # nohup, so closing the terminal that ran `up` does not take the stack with
  # it. Appending rather than truncating: the previous run's failure is the
  # thing you want when this one starts and dies.
  nohup "$0" "$@" >>"$(_logfile "$name")" 2>&1 &
  echo $! >"$(_pidfile "$name")"
  echo "  $name started (pid $!, log $(_logfile "$name"))" >&2
}

_wait_http() {
  # _wait_http <url> <deadline seconds> <label>
  #
  # Through `docker/wait_for_http.py` rather than `curl`, for two reasons. The
  # first is reuse: that file is how the container topology waits for Qdrant,
  # and a second implementation of "poll until 2xx or give up" would be a
  # second set of semantics to keep in step -- the argument ADR-104 made about
  # the web-search probe, applied again.
  #
  # The second is that `curl` is not guaranteed. A minimal WSL or container
  # image may not have it, and the failure was indistinguishable from a broken
  # API: with `curl` absent the loop simply never succeeded, spun for the full
  # deadline, and then reported that the API had not answered -- naming the
  # wrong thing for five minutes. The interpreter this needs is the one
  # `_setup` has already guaranteed, and it imports nothing outside the
  # standard library.
  local url=$1 deadline=$2 label=$3
  if WAIT_FOR_HTTP_URL="$url" WAIT_FOR_HTTP_DEADLINE_SECONDS="$deadline" \
    "$PYTHON" docker/wait_for_http.py >/dev/null 2>&1; then
    echo "  $label answered $url" >&2
    return 0
  fi
  echo "  $label did not answer $url within ${deadline}s" >&2
  return 1
}

_alive_after() {
  # A process with no endpoint to poll: give it a moment and see if it is still
  # there. Weaker than a readiness check and labelled as such -- what it
  # actually catches is the common case, a process that exits on its first line
  # because a dependency is missing.
  local name=$1 seconds=$2
  sleep "$seconds"
  if _running "$name"; then
    echo "  $name still up after ${seconds}s (no readiness endpoint; see its log)" >&2
    return 0
  fi
  echo "  $name exited within ${seconds}s -- read $(_logfile "$name")" >&2
  return 1
}

# The plan is computed in one place and printed by `--plan`, so "what will this
# start" is answerable without starting it -- and so a test can assert the
# ordering without a database, a key or a single real process.
# Progress that reads like a modern installer rather than a wall of output.
# Each step prints its name, then its result and how long it took, so a start
# that takes four minutes -- most of it one model download -- shows which
# minute belongs to what instead of appearing hung.
STEP_INDEX=0
STEP_TOTAL=0
_step_begin() {
  STEP_INDEX=$((STEP_INDEX + 1))
  STEP_STARTED=$SECONDS
  printf '  [%d/%d] %-16s %s\n' "$STEP_INDEX" "$STEP_TOTAL" "$1" "${2:-}" >&2
}
_step_end() {
  printf '        %-16s %s (%ss)\n' "" "${1:-ok}" "$((SECONDS - STEP_STARTED))" >&2
}

# What `uv` does for a Python project, done once for this checkout: if the
# environment a command needs is not there, build it rather than printing an
# instruction. `up` calls this itself, so a fresh clone is one command.
#
# `.env` is created too, and it is not decoration: three DSNs are in
# FORBIDDEN_TOML_PATHS and can only come from the environment, so a checkout
# without `.env` fails the *first* line of the gate with three validation
# errors that read like a broken clone. dev.sh exports its own DSNs and does
# not need the file; `pytest` and `agent-config-check` do.
# Whether this interpreter can build the real embedder, asked the way the
# application asks it: `adapters/embedding/bge.py` imports `sentence_transformers`
# and turns the ImportError into `EmbeddingBackendUnavailableError`. Importing
# the same name is the only probe that cannot disagree with it.
_has_embedding_extra() {
  [ -x "$PYTHON" ] || return 1
  "$PYTHON" -c "import sentence_transformers" >/dev/null 2>&1
}

_setup() {
  local did=0
  if [ ! -x "$PYTHON" ]; then
    if ! command -v uv >/dev/null 2>&1; then
      echo "setup: no uv on PATH and no $PYTHON." >&2
      echo "       Install it: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
      return 1
    fi
    echo "        creating .venv with uv (first run downloads the dependency tree)" >&2
    if [ "${WITH_RETRIEVAL:-0}" = 1 ]; then
      echo "        including the embedding extra: several GB of torch and weights" >&2
      uv sync --frozen --group dev --extra embedding >&2 || return 1
    else
      uv sync --frozen --group dev >&2 || return 1
    fi
    did=1
  fi
  # Asked for retrieval on a venv that already exists and does not have it.
  # Adding to an existing environment rather than rebuilding: the weights live
  # in ~/.cache/huggingface and torch in uv's cache, so this is usually a
  # minute rather than a download.
  if [ "${WITH_RETRIEVAL:-0}" = 1 ] && ! _has_embedding_extra; then
    echo "        adding the embedding extra to the existing .venv" >&2
    uv sync --frozen --group dev --extra embedding >&2 || return 1
    did=1
  fi
  if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    echo "        wrote .env from .env.example (the three DSNs in it are correct;" >&2
    echo "        the provider key line is a placeholder and stays one)" >&2
    did=1
  fi
  [ "$did" = 0 ] && echo "        already present" >&2
  return 0
}

_plan() {
  if [ -n "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ]; then
    PLAN_PROFILE="console"
    PLAN_WHY="provider key present: the console profile (Word + web + sandbox + Chat)"
    PLAN_STEPS=(word-server web-server sandbox-server demo-api ingest demo-worker)
    # Not len(PLAN_STEPS): the three servers render as one step plus one probe
    # step, and waiting for the API is a step of its own. Kept here beside the
    # steps it counts, and asserted against the `_step_begin` calls the arm
    # actually reaches -- the first version hand-counted it and said 6 for a
    # path that runs 7, so the last line read `[7/6]`.
    PLAN_STEP_TOTAL=10
  else
    PLAN_PROFILE="keyless"
    PLAN_WHY="no provider key: search and Tasks without Chat, Worker on the demo graph"
    PLAN_STEPS=(api ingest worker)
    PLAN_STEP_TOTAL=8
  fi
}

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
up)
  shift
  plan_only=0
  WITH_RETRIEVAL=0
  for argument in "$@"; do
    case "$argument" in
      --plan|--dry-run) plan_only=1 ;;
      --with-retrieval) WITH_RETRIEVAL=1 ;;
      *) echo "up: unknown option $argument (--plan, --with-retrieval)" >&2; exit 2 ;;
    esac
  done
  _plan
  if [ "$plan_only" = 1 ]; then
    echo "profile: $PLAN_PROFILE"
    echo "reason:  $PLAN_WHY"
    echo "steps:   services migrate ${PLAN_STEPS[*]}"
    echo "shown as: $PLAN_STEP_TOTAL steps"
    if _has_embedding_extra; then
      echo "retrieval: real BGE-M3 (the embedding extra is installed)"
    else
      echo "retrieval: ABSENT -- no embedding extra; add --with-retrieval to install it"
    fi
    exit 0
  fi

  # Refused rather than left to fail at bind time. The realistic collision is
  # this project's own Compose stack, which publishes the same port on
  # loopback -- and its failure, an API that exits on "address already in use"
  # a minute into loading the embedding runtime, is read as a broken checkout
  # far more often than as two stacks wanting one port.
  if _port_busy "$API_PORT" && ! _running api && ! _running demo-api; then
    echo "up: something is already serving 127.0.0.1:$API_PORT." >&2
    echo "    The usual cause is this project's Compose stack. Check with:" >&2
    echo "      docker compose --profile demo ps" >&2
    echo "    Stop it (docker compose --profile demo down) or use that one." >&2
    exit 2
  fi

  # One step per thing that can take time or fail, counted up front so the
  # display can say 3/9 rather than leaving a reader to guess how much is left.
  STEP_TOTAL=$PLAN_STEP_TOTAL
  echo "" >&2
  echo "  $PLAN_WHY" >&2
  echo "" >&2

  _step_begin setup "python env and .env, if this checkout has none"
  _setup || exit 1
  _step_end

  # Said here, before four minutes of startup, because the absence it reports
  # is the one this stack cannot show you afterwards. A process with no real
  # embedder serves every route, answers /health/ready 200, and comes up in
  # five seconds instead of two minutes -- measured 2026-08-30. From outside it
  # is a fast, healthy console that happens to retrieve nothing.
  #
  # It is a report, not a repair: the extra is gigabytes, and installing it
  # because somebody typed `up` would be deciding for them. `--with-retrieval`
  # is how they decide.
  _step_begin retrieval "can this start build the real embedder?"
  if _has_embedding_extra; then
    _step_end "yes: BGE-M3 dense + sparse and the reranker"
  else
    _step_end "NO -- knowledge-base retrieval will be absent"
    echo "        The 'embedding' extra is not in $PYTHON. This start will serve" >&2
    echo "        Chat and Tasks, and /health/ready will say 200, but an upload" >&2
    echo "        will never become searchable and the ingestion worker exits on" >&2
    echo "        its first line. Nothing in the browser says so." >&2
    echo "        To get it:  scripts/dev.sh down && scripts/dev.sh up --with-retrieval" >&2
    echo "        Cost: several GB, and one retrieval process wants ~12 GB of RAM." >&2
  fi

  # Containers and schema in the foreground: everything below is meaningless
  # without them, and both are fast and idempotent.
  _step_begin services "PostgreSQL 5433 · Qdrant 6333"
  "$0" services >&2
  _step_end

  _step_begin migrate "schema to head"
  "$0" migrate >/dev/null 2>&1 || { _step_end "failed"; echo "  migrate failed; rerun it alone to see why: scripts/dev.sh migrate" >&2; exit 1; }
  _step_end

  if [ "$PLAN_PROFILE" = "console" ]; then
    # All three MCP servers before anything that reads a tool catalogue.
    # `sandbox-server` is in this list because `demo-api` probes it and will
    # not start without it -- the omission that made the written instructions
    # unfollowable.
    _step_begin mcp-servers "word 8765 · web 8767 · sandbox 8766"
    _start word-server word-server
    _start web-server web-server
    _start sandbox-server sandbox-server
    _step_end "started"

    _step_begin mcp-probe "all three answer before anything freezes a catalogue"
    for pair in "word-check:word-server" "web-check:web-server" "sandbox-check:sandbox-server"; do
      if ! "$0" "${pair%%:*}" >/dev/null 2>&1; then
        _step_end "failed"
        echo "  ${pair%%:*} never passed -- read $(_logfile "${pair##*:}")" >&2
        exit 1
      fi
    done
    _step_end "all three healthy"

    _step_begin api "demo-api: Word + web + sandbox + Chat"
    _start demo-api demo-api
    _step_end "started"
  else
    _step_begin api "api: search and Tasks, no Chat"
    _start api api
    _step_end "started"
  fi

  # The API loads the embedding runtime before it serves, which is where the
  # minutes go. 300s rather than 60: measured cold on this machine at well
  # over a minute, and a deadline shorter than the thing it waits for turns a
  # slow start into a reported failure.
  _step_begin api-ready "loading BGE-M3; a cold start is minutes, not seconds"
  if ! _wait_http "http://127.0.0.1:$API_PORT/health/ready" 300 "API"; then
    _step_end "failed"
    echo "  the API never became ready -- read $(_logfile "$([ "$PLAN_PROFILE" = console ] && echo demo-api || echo api)")" >&2
    exit 1
  fi
  _step_end

  # Ingestion after the API, though nothing forces that order: it is the one
  # process whose absence is invisible in the browser -- uploads sit in
  # `processing` and the page cannot tell that apart from vectorizing -- so it
  # is started here rather than left to be remembered.
  _step_begin ingest "the one absence a browser cannot see"
  _start ingest ingest
  _alive_after ingest 5 || true
  _step_end

  _step_begin worker "Task worker"
  if [ "$PLAN_PROFILE" = "console" ]; then
    _start demo-worker demo-worker
  else
    _start worker worker
  fi
  _step_end "started"
  echo "" >&2

  echo "console  http://127.0.0.1:$API_PORT/ui/"
  echo "status   scripts/dev.sh status"
  echo "logs     scripts/dev.sh logs <name>"
  echo "stop     scripts/dev.sh down"
  if [ "$PLAN_PROFILE" = "keyless" ]; then
    echo ""
    echo "No provider key, so this deployment has no Chat: the route is not"
    echo "registered rather than registered and failing. The System page lists"
    echo "that alongside everything else it could not assemble. To add one,"
    echo "put it in \$AW_KEY_FILE (default ~/.config/agent-workbench/key),"
    echo "then: scripts/dev.sh down && scripts/dev.sh up"
  fi
  ;;

down)
  # Reverse start order, so a Worker is gone before the servers whose tools it
  # holds. TERM and then wait: these processes have shutdown grace periods
  # (the API drains, the Worker finishes or releases its lease), and KILL
  # skips exactly the part that keeps a Task from being stranded.
  #
  # `_running` is what decides whether to signal at all, and it checks the pid
  # still belongs to something of ours -- a stale file naming a recycled pid
  # gets deleted here rather than turned into a TERM for a stranger.
  stopped=0
  for name in demo-worker worker ingest demo-api api sandbox-server web-server word-server; do
    _running "$name" || { rm -f "$(_pidfile "$name")"; continue; }
    pid=$(cat "$(_pidfile "$name")")
    kill -TERM "$pid" 2>/dev/null || true
    waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 30 ]; do
      sleep 1
      waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "  $name ignored TERM for 30s; sending KILL" >&2
      kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$(_pidfile "$name")"
    echo "  $name stopped" >&2
    stopped=$((stopped + 1))
  done
  [ "$stopped" = 0 ] && echo "  nothing that \`up\` started is running" >&2
  # PostgreSQL and Qdrant keep running on purpose: they hold the local
  # database, they cost nothing idle, and `docker rm` on them would take a
  # developer's data with it. Stop them with `docker stop aw-postgres
  # aw-qdrant` when you actually mean to.
  echo "  containers left running (docker stop aw-postgres aw-qdrant to stop them)" >&2
  ;;

status)
  printf '%-16s %-8s %-10s %s\n' NAME PID STATE LOG
  for name in word-server web-server sandbox-server api demo-api ingest worker demo-worker; do
    file=$(_pidfile "$name")
    [ -f "$file" ] || continue
    pid=$(cat "$file" 2>/dev/null || echo "-")
    if _running "$name"; then state=running; else state=gone; fi
    printf '%-16s %-8s %-10s %s\n' "$name" "$pid" "$state" "$(_logfile "$name")"
  done
  if _port_busy "$API_PORT"; then
    echo ""
    echo "127.0.0.1:$API_PORT is serving. If nothing above says running, that is"
    echo "somebody else's stack -- most likely Compose."
  fi
  ;;

logs)
  if [ -z "${2:-}" ]; then
    echo "usage: scripts/dev.sh logs <name>" >&2
    # Only the names `up` manages. `var/log/` also collects whatever anybody
    # ever redirected into it by hand, and offering those as choices makes
    # this list a worse answer than no list.
    for name in word-server web-server sandbox-server api demo-api ingest worker demo-worker; do
      [ -f "$(_logfile "$name")" ] && printf '  %s\n' "$name" >&2
    done
    exit 2
  fi
  exec tail -f "$(_logfile "$2")"
  ;;

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

sandbox-image)
  # Build the image that lets `sandbox_run` produce a PDF instead of only text.
  #
  # Separate from `sandbox-server` because it is slow, needs the network, and
  # is a thing somebody chooses -- the same reason `services` is its own verb.
  # Roughly 70 MB on top of the 41 MB base; about a minute cold.
  exec docker build -t "$SANDBOX_PDF_IMAGE" -f docker/sandbox-pdf.Dockerfile docker
  ;;

sandbox-server)
  # One container per call, created and destroyed inside the call (ADR-029).
  # It owns no path, no tenant and no workspace: files in, files out. What it
  # needs is a container runtime; without one it starts and every call answers
  # an error, which is why the check below runs a real script rather than
  # reading /health.
  #
  # The image is chosen here rather than defaulted in the code, and `--image`
  # has been a parameter of `apps.sandbox_mcp.main` since ADR-029. What decides
  # it is whether the richer one has actually been built: a server started
  # against an image that is not on this machine answers every call with a
  # pull failure, and `--network=none` means it cannot fetch one mid-call.
  #
  # So this probes, and says which of the two it got. Silently falling back
  # would reproduce the bug this whole batch is about -- a model that cannot
  # produce a PDF, with nothing anywhere saying why.
  if docker image inspect "$SANDBOX_PDF_IMAGE" >/dev/null 2>&1; then
    echo "sandbox image: $SANDBOX_PDF_IMAGE (reportlab + CJK font available)" >&2
    exec "$PYTHON" -m agent_workbench.apps.sandbox_mcp.main --image "$SANDBOX_PDF_IMAGE"
  fi
  echo "sandbox image: the stock default -- scripts are limited to the standard library." >&2
  echo "  no PDF, no charts, no spreadsheets; \`--network=none\` means a script cannot install one." >&2
  echo "  to change that: scripts/dev.sh sandbox-image" >&2
  exec "$PYTHON" -m agent_workbench.apps.sandbox_mcp.main
  ;;

sandbox-check)
  # `--expect-tool run_python` is the remote name; `sandbox_run` is what the
  # envelope and the model call it. The two differ on purpose (ADR-029 §3.2).
  exec "$PYTHON" scripts/smoke_mcp_server.py \
    --label sandbox \
    --endpoint "http://127.0.0.1:8766/mcp" \
    --health-url "http://127.0.0.1:8766/health" \
    --expect-tool run_python
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

computer-server)
  # The one server here whose reach is the machine rather than this process's
  # own workspace: it moves the cursor and presses keys on whatever is in
  # front of you. That is why it is absent from demo-local and why the extra
  # it needs is not installed by default (ADR-070).
  #
  # It exits at startup rather than serving when the `computer-use` extra is
  # missing or Screen Recording is not granted, naming whichever one it is. A
  # server that starts and refuses every call is one you diagnose by reading
  # logs; macOS is worse than that, and hands a process without the grant a
  # picture of the wallpaper with every window gone.
  #
  # There is deliberately no `computer-api`/`computer-worker` pair to go with
  # this, and ADR-075 is where the reason is argued rather than here. The
  # short of it: `config.computer-local.toml` declares
  # `retryable_effects = false` -- a click is not a GET, and a replayed one
  # lands on whatever is under the cursor *now* -- and the Task path declines
  # such a server at both ends, keeping its tools out of every authorization
  # envelope and registering no binding for them. Measured 2026-08-23: a
  # Worker on that profile comes up holding zero MCP tools.
  #
  # That refusal is now a decision with a test rather than a consequence
  # nobody owned (tests/config/test_local_computer_profile.py). A command that
  # started this profile's API and Worker would therefore be a promise the
  # platform does not keep: it would cost the console its Word, web and Code
  # tools and buy a Worker with no screen tools in exchange.
  #
  # So the screen tools are reached by speaking MCP to this server --
  # `computer-check` below is a working example -- and not through a Task.
  #
  # Started from a signed .app rather than from this shell, and that is a
  # requirement rather than packaging taste (ADR-092). macOS lets a process
  # change which application is frontmost only when it has a bundle identity,
  # a code signature, the Accessibility grant and a live main-thread run loop.
  # Launched from here it would have none of the first three, and
  # `activate_application` would refuse every call -- politely, naming the
  # reason, but every call.
  #
  # The build is idempotent and keeps the bundle id and signing identity
  # fixed, so rebuilding does not cost another trip to System Settings.
  bash "$(dirname "${BASH_SOURCE[0]}")/build_computer_app.sh"
  APP="${AW_COMPUTER_APP_DIR:-$HOME/Applications}/AgentComputerMCP.app"
  echo "starting $APP (log: ~/Library/Logs/AgentComputerMCP.log)" >&2
  exec open -W -a "$APP"
  ;;

computer-check)
  # All eight named rather than trusting a count. Not because they end up in
  # an envelope -- on this profile they deliberately do not -- but because the
  # profile's `tools` list is the contract this server is supposed to satisfy,
  # and a server that came up with seven of them is a different deployment
  # wearing the same alias. Counting would call it healthy.
  #
  # Six of these until ADR-091. The two added are the ones a task needs to get
  # past its first application: the others all act on whatever is frontmost,
  # and until `activate_application` existed nothing could change what that
  # was.
  exec "$PYTHON" scripts/smoke_mcp_server.py \
    --label computer \
    --endpoint "http://127.0.0.1:8768/mcp" \
    --health-url "http://127.0.0.1:8768/health" \
    --expect-tool request_access \
    --expect-tool list_granted_applications \
    --expect-tool activate_application \
    --expect-tool screenshot \
    --expect-tool left_click \
    --expect-tool type \
    --expect-tool key \
    --expect-tool scroll
  ;;

code-api)
  # The profile existed before this command did, which meant the only way to
  # start it was to know the file's name and export AW_CONFIG_FILE by hand.
  #
  # Refuses without a key, like the three explicit workers and unlike plain
  # `api`. The asymmetry is the point: a keyless `api` loses chat and still
  # serves search, so it is a smaller process rather than a broken one. A Code
  # session has no fixed-shape fallback -- a turn is a model loop or it is
  # nothing -- so a keyless one opens sessions that can only ever fail, and it
  # fails inside a turn, where the message reaches the browser as a turn error
  # rather than as a process that said why it would not start.
  export AW_CONFIG_FILE=config/config.code-local.toml
  if [ -z "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ]; then
    echo "code-api requires AW_SECRETS__DEEPSEEK_API_KEY; a coding turn has no fallback" >&2
    exit 2
  fi
  # Said here rather than left to be discovered: the session opens and reads
  # its workspace without it, and every write is refused by policy -- which
  # from the transcript alone looks like a model that will not use its tools.
  echo "code profile: the console must send x-principal-scopes with workspace:write" >&2
  shift
  exec "$PYTHON" -m agent_workbench.apps.api.main "$@"
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
  #
  # Code sessions come from the profile now (`[code] enabled = true` in
  # config.demo-local.toml) rather than from an `AW_CODE__ENABLED` a caller
  # remembered to export. The same key gates them: a coding turn is a model
  # loop or it is nothing, so the refusal above covers Code too.
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
  #
  # Decided, not exported unconditionally (ADR-104). This used to be a bare
  # `export AW_RESEARCH__ENABLED=true`, and that overrode two people: an
  # operator who had exported `false` in this very shell, and anyone who had
  # switched web search off on the System page -- a stored switch ranks below
  # the environment (ADR-103 §3.2), so the page reported this start as
  # `overridden` and blamed an environment nobody had set. The container
  # launcher had already been taught to step aside; this is the same probe
  # answering the same question, so the two launchers cannot drift apart: an
  # explicit value is left alone, a stored choice -- either way -- takes the
  # decision away and the settings loader applies or holds it, and only when
  # nobody decided does "is there a key" stand, which the refusal above has
  # already answered. The probe explains which case it found, on stderr; its
  # stdout is sent there too because this arm's stdout belongs to the process
  # it is about to exec.
  if [ -z "${AW_RESEARCH__ENABLED:-}" ]; then
    if "$PYTHON" docker/decide_web_search.py >&2; then
      export AW_RESEARCH__ENABLED=true
    fi
  fi
  # Probed here, before the process replaces this shell, for the reason
  # `demo-worker` probes its two servers: an MCP tool catalogue is frozen once
  # at startup. The API's own refusal (ADR-057) would arrive too -- it raises
  # rather than serving a coding session that was promised a sandbox it cannot
  # reach -- but it arrives after the embedding runtime has spent forty
  # seconds loading, and it cannot name the command that fixes it as plainly
  # as this can.
  "$PYTHON" scripts/smoke_mcp_server.py \
    --label sandbox \
    --endpoint "http://127.0.0.1:8766/mcp" \
    --health-url "http://127.0.0.1:8766/health" \
    --expect-tool run_python >&2
  # Says what was decided rather than promising "chat search": with a stored
  # "off" this start has none, and a banner claiming otherwise is the same
  # lie the System page was built to stop telling.
  echo "console profile (Word + web + sandbox); key available;" \
    "research.enabled=${AW_RESEARCH__ENABLED:-<the stored switch decides>}" >&2
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
  #
  # Decided the way `demo-api` decides it, by the same probe (ADR-104). The
  # API and the Worker read one switches file, and "web search is on" has to
  # be one sentence across the two processes: an API that froze
  # `external_search` into an envelope for a Worker that never registered it
  # is the failure above, reached from the other side.
  if [ -z "${AW_RESEARCH__ENABLED:-}" ]; then
    if "$PYTHON" docker/decide_web_search.py >&2; then
      export AW_RESEARCH__ENABLED=true
    fi
  fi
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

panel)
  # The one command here that needs nothing running: no database, no Qdrant, no
  # provider key. It reads the working tree, writes one self-contained page and
  # serves it on loopback -- so "what is this repository" is answerable on a
  # fresh checkout, before any of the above has been started.
  #
  # And deliberately not "$PYTHON", which is .venv/bin/python. The venv is
  # exactly the thing you do not have yet on the checkout where this command is
  # most useful, so routing through it would have made the first step depend on
  # the step it exists to precede. The panel imports nothing outside the
  # standard library, so any python3 runs it; the venv is preferred only when it
  # is already there, to keep one interpreter in play for people who have one.
  shift
  if [ -x "$PYTHON" ]; then
    exec "$PYTHON" scripts/architecture_panel.py --serve "$@"
  fi
  if command -v python3 >/dev/null 2>&1; then
    exec python3 scripts/architecture_panel.py --serve "$@"
  fi
  echo "panel: no $PYTHON and no python3 on PATH" >&2
  exit 1
  ;;

*)
  usage
  exit 2
  ;;
esac
