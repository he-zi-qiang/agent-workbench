#!/bin/sh
# The application is intentionally loopback-only. The tiny stdlib proxy is
# the only listener on the container interface, and Compose maps that listener
# to 127.0.0.1 on the host (never 0.0.0.0).
set -eu

agent-api &
api_pid=$!
python /app/docker/loopback_proxy.py &
proxy_pid=$!

cleanup() {
  kill -TERM "$api_pid" "$proxy_pid" 2>/dev/null || true
  wait "$api_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
}

trap 'cleanup; exit 0' INT TERM
wait "$api_pid"
status=$?
kill -TERM "$proxy_pid" 2>/dev/null || true
wait "$proxy_pid" 2>/dev/null || true
exit "$status"
