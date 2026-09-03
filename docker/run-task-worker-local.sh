#!/bin/sh
# Start the two MCP servers this Worker's tools live in, prove they answer,
# then become the Worker.
#
# The order is the whole point, and it is not tidiness. A Worker freezes its
# MCP tool catalogue **once, at startup**, and never reloads it
# (`adapters/mcp/registry_source.py`). Discovery failure is fail-soft: it logs
# `mcp_connection_failed` and continues. So a Worker that starts before its
# servers is not a Worker that retries -- it is a healthy Worker, permanently
# missing the tools it exists for, and the only trace is one log line nobody is
# watching. `scripts/dev.sh` solves the same problem the same way on the native
# path, and its comment carries the incident that taught it.
#
# Health is not enough to gate on: both servers answer `/health` with `ok` from
# the moment uvicorn binds, before the MCP application can list a tool. So the
# gate is `scripts/smoke_mcp_server.py`, which connects a real MCP client and
# asserts the tool names -- the same probe `scripts/dev.sh` runs.
#
# Sidecars rather than their own Compose services: reaching a server across the
# container network would have to break each server's loopback `--host`
# whitelist, the MCP SDK's Host-header validation, and the settings rule that a
# non-loopback MCP endpoint must be HTTPS. `config/config.compose-local.toml`
# carries the endpoints and the reasoning.
set -eu

WORD_PORT=8765
WEB_PORT=8767

agent-word-mcp --port "$WORD_PORT" &
word_pid=$!
agent-web-mcp --port "$WEB_PORT" &
web_pid=$!

cleanup() {
  kill -TERM "$word_pid" "$web_pid" 2>/dev/null || true
  wait "$word_pid" 2>/dev/null || true
  wait "$web_pid" 2>/dev/null || true
}
trap 'cleanup; exit 0' INT TERM

# Long enough to cover a cold container on a laptop that is also building
# nothing else; short enough that a server which will never come up is reported
# rather than waited on. `up --wait` allows 600s for the whole stack.
if ! python /app/scripts/smoke_mcp_server.py \
    --label word \
    --endpoint "http://127.0.0.1:${WORD_PORT}/mcp" \
    --health-url "http://127.0.0.1:${WORD_PORT}/health" \
    --expect-tool render_document \
    --wait-seconds 60 >&2; then
    echo "task-worker: the word MCP server never advertised render_document" >&2
    cleanup
    exit 1
fi

if ! python /app/scripts/smoke_mcp_server.py \
    --label web \
    --endpoint "http://127.0.0.1:${WEB_PORT}/mcp" \
    --health-url "http://127.0.0.1:${WEB_PORT}/health" \
    --expect-tool fetch_page \
    --expect-tool download_document \
    --wait-seconds 60 >&2; then
    echo "task-worker: the web MCP server never advertised its tools" >&2
    cleanup
    exit 1
fi

# `--demo` or not, decided here rather than in compose.yaml, because it cannot
# be decided statically: a real Worker requires a provider key
# (`RealTaskHandlersUnavailableError`), and a fresh stack has none until
# somebody saves one on the console's settings page -- which lives in a process
# that has to be up for them to reach it.
#
# Falling back rather than exiting is deliberate, and it is the opposite of
# what `scripts/dev.sh demo-api` does with the same missing key. That launcher
# runs in a terminal somebody is watching, so exiting is a message. This one
# runs under `docker compose up -d --wait`, where an exiting container is not a
# message -- it is the whole stack failing to come up, thirty minutes into a
# first run, for the ordinary and expected condition of not having typed a key
# yet. So it says so and carries on.
if python -c "
import sys
from agent_workbench.bootstrap.provider_key import usable_key_present
sys.exit(0 if usable_key_present() else 1)
"; then
    echo "task-worker: provider key present -- real handlers, real graph" >&2
    exec agent-task-worker
fi

echo "task-worker: no provider key, so this Worker runs SYNTHETIC handlers." >&2
echo "             Tasks will reach 'succeeded' without a single model call or" >&2
echo "             tool call, and the console cannot tell that apart from a" >&2
echo "             real run. Save a key in 系统 > 模型密钥, then:" >&2
echo "               scripts\\stack.cmd restart   (or: docker compose" >&2
echo "               --profile demo restart api task-worker task-worker-b)" >&2
exec agent-task-worker --demo
