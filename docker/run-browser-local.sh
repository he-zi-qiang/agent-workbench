#!/bin/sh
# The browser container: Chromium and the MCP server, and no way out.
#
# This container is on an `internal: true` network, so it has no default route
# (ADR-0113 §3.3). It cannot reach the internet at all; what it can reach is
# `browser-egress`, which holds the destination guard and is the only thing in
# this topology that is both visible from here and able to leave. That is why
# "the browser only leaves through the guard" is not a flag somebody has to
# remember: turn the guard off and this container reaches nothing.
#
# Chromium keeps its own sandbox here. `--no-sandbox` is deliberately absent --
# ADR-0113 §3.5 measured that the namespace sandbox works under `cap_drop: ALL`
# once `docker/chromium-seccomp.json` stops refusing `CLONE_NEWUSER`, and the
# A/B is in that section. If the profile is ever dropped from `compose.yaml`,
# Chromium aborts at start rather than quietly running unsandboxed, which is the
# failure mode worth having.
set -eu

# Two ports for the same reason the sandbox broker has two: a wildcard bind and
# a loopback bind on one port collide, and the server itself is loopback-only
# because its `--host` is a choice list.
SERVER_PORT=8780
PUBLIC_PORT=8773

EGRESS_HOST="${BROWSER_EGRESS_HOST:-browser-egress}"
EGRESS_PROXY_PORT="${BROWSER_EGRESS_PROXY_PORT:-8771}"
EGRESS_CONTROL_PORT="${BROWSER_EGRESS_CONTROL_PORT:-8772}"

# Chromium writes a lot under $HOME, which is on the read-only root.
HOME=/tmp/browser-home
export HOME
mkdir -p "$HOME"

LOCAL_PROXY_LISTEN_HOST=0.0.0.0 \
LOCAL_PROXY_PORT="$PUBLIC_PORT" \
LOCAL_PROXY_UPSTREAM_HOST=127.0.0.1 \
LOCAL_PROXY_UPSTREAM_PORT="$SERVER_PORT" \
    python /app/docker/loopback_proxy.py &
proxy_pid=$!

# `--allow-host` is deliberately not passed here: the allowlist belongs to the
# guard, and the guard is in the other container. `agent-browser-mcp` refuses
# the combination rather than silently ignoring it.
agent-browser-mcp \
    --port "$SERVER_PORT" \
    --proxy-endpoint "http://${EGRESS_HOST}:${EGRESS_PROXY_PORT}" \
    --decisions-url "http://${EGRESS_HOST}:${EGRESS_CONTROL_PORT}/decisions" \
    --workspace-root /workspace &
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
