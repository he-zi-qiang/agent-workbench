#!/bin/sh
# The command runner (ADR-0115): `agent-runner-mcp` on loopback, published on
# this container's own interface through the same stdlib tunnel the sandbox
# broker and the browser use, so the server's `--host` choice list, the MCP
# SDK's Host check and the API's settings validator all keep seeing the
# loopback address they were written for (ADR-0107 §3.4).
#
# Two ports for one server, on purpose: 8784 is the server's own loopback
# listener, 8774 is what the API's tunnel dials. `run-sandbox-local.sh` has
# the same pair (8776 / 8766) for the same reason.
set -eu

SERVER_PORT=8784
PUBLIC_PORT=8774

# The image's `app` user has `/app` for a home, and `/app` is read-only here
# (`x-app-hardening`). A command that writes to `$HOME` -- a `pip` cache, a
# `.npmrc`, a tool's first-run marker -- would fail on that rather than on
# anything the person approving it could see, so the home is on the tmpfs.
# The same arrangement `run-browser-local.sh` makes for Chromium.
HOME=/tmp/runner-home
export HOME
mkdir -p "$HOME"

LOCAL_PROXY_LISTEN_HOST=0.0.0.0 \
LOCAL_PROXY_PORT="$PUBLIC_PORT" \
LOCAL_PROXY_UPSTREAM_HOST=127.0.0.1 \
LOCAL_PROXY_UPSTREAM_PORT="$SERVER_PORT" \
    python /app/docker/loopback_proxy.py &
proxy_pid=$!

# `--projects-root /projects` is where compose.yaml binds the host folder,
# and it is the one thing this server checks a `cwd` against. The server
# answers 503 on `/health` until that directory exists, which is what keeps
# the API's probe from turning `project_run` on for a start whose mount did
# not arrive.
agent-runner-mcp --port "$SERVER_PORT" --projects-root /projects &
server_pid=$!

cleanup() {
  kill -TERM "$server_pid" "$proxy_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
}

trap 'cleanup; exit 0' INT TERM
wait "$server_pid"
status=$?
kill -TERM "$proxy_pid" 2>/dev/null || true
wait "$proxy_pid" 2>/dev/null || true
exit "$status"
