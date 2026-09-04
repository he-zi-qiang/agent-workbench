#!/bin/sh
# The application is intentionally loopback-only. The tiny stdlib proxy is
# the only listener on the container interface, and Compose maps that listener
# to 127.0.0.1 on the host (never 0.0.0.0).
set -eu

# Chat's `web_search` exists only where `[research]` is configured (ADR-021):
# with no provider the tool is never built, and the model answers "我没有联网
# 查询功能" -- true of that deployment, and indistinguishable from a broken
# feature to the person reading it. That is what this stack looked like the day
# somebody went looking for an expired API key that was not expired.
#
# Decided here rather than in `compose.yaml` because it cannot be decided
# statically: `research.enabled` without a key is a *startup error* by design,
# so a compose file that set it unconditionally would turn a fresh stack --
# which has no key until somebody saves one on the settings page -- into a stack
# that will not come up, and the page that saves the key is inside the process
# that refuses to start. The probe asks the package for the same answer the
# validator will reach a second later.
#
# `scripts/dev.sh demo-api` makes the same decision for the same reason -- and
# since ADR-104 by running this same probe, so the two launchers cannot answer
# differently -- because this is the containerised console rather than a
# different product. What it
# costs is stated: searches go to the provider on this key, bounded at
# `research.max_uses` per turn. To decline it, start the stack with
# `AW_RESEARCH__ENABLED=false` in the environment -- an explicit value is left
# alone here, and only an unset or empty one is decided.
#
# Compose has no syntax for "omit this key", so an unset host variable arrives
# here as the empty string. Empty means "nobody decided", and it is safe either
# way: measured 2026-09-01, pydantic-settings loads `AW_RESEARCH__ENABLED=""`
# as False, which is exactly what a start that found no key wants.
#
# Since ADR-103 "nobody decided" also means nothing is stored for
# `research.enabled` on the console's System page: a stored choice, either
# way, is applied (or held, when "on" meets no key) by the settings loader,
# and this probe stays out of it. The probe prints which case it found.
if [ -z "${AW_RESEARCH__ENABLED:-}" ]; then
    if python /app/docker/decide_web_search.py; then
        AW_RESEARCH__ENABLED=true
        export AW_RESEARCH__ENABLED
    fi
fi

# Two inward tunnels (ADR-0107, ADR-0108), started before anything that would
# dial them. Each puts a loopback listener in this container in front of a
# loopback-only server that lives somewhere else, so every guard on the path
# -- the settings validator, the MCP SDK's Host check, the servers' own
# `--host` choice lists -- keeps seeing the loopback address it was written
# for. `docker/loopback_proxy.py` carries the argument.
#
# 8766 is the sandbox broker, another container of this stack. 8768 is the
# computer-use server, which no container can run: it needs the desktop, so
# it runs on the host (`scripts\computer.cmd`), and `host.docker.internal` is
# Docker Desktop's name for the host. When nothing listens there the tunnel
# drops each connection, and `routes/computer.py` reports "not running" --
# the same answer it gives on a machine that never started one.
LOCAL_PROXY_LISTEN_HOST=127.0.0.1 \
LOCAL_PROXY_PORT=8766 \
LOCAL_PROXY_UPSTREAM_HOST="${SANDBOX_UPSTREAM_HOST:-sandbox}" \
LOCAL_PROXY_UPSTREAM_PORT=8766 \
    python /app/docker/loopback_proxy.py &
sandbox_tunnel_pid=$!
LOCAL_PROXY_LISTEN_HOST=127.0.0.1 \
LOCAL_PROXY_PORT=8768 \
LOCAL_PROXY_UPSTREAM_HOST="${COMPUTER_UPSTREAM_HOST:-host.docker.internal}" \
LOCAL_PROXY_UPSTREAM_PORT=8768 \
    python /app/docker/loopback_proxy.py &
computer_tunnel_pid=$!

# The sandbox, decided per start for the reason web search is: on without a
# broker that answers is a startup error by design (`SandboxSlot.open`,
# ADR-057), and the broker may be pulling its image, or the socket mount may
# have failed, or somebody may have taken it out of the topology. An explicit
# value for either variable is left alone; only "nobody decided" is decided.
# The probe waits for the *runtime* behind the broker, not for its socket.
if [ -z "${AW_CODE__SANDBOX_ENABLED:-}" ] && [ -z "${AW_SANDBOX__ENABLED:-}" ]; then
    if python /app/docker/decide_sandbox.py; then
        AW_CODE__SANDBOX_ENABLED=true
        AW_SANDBOX__ENABLED=true
        export AW_CODE__SANDBOX_ENABLED AW_SANDBOX__ENABLED
    fi
fi

# --web-dir makes this stack a demo somebody can open rather than a set of
# routes somebody has to know. The API refuses to start if the directory is
# missing, so a broken image fails here rather than in a browser.
agent-api --web-dir /app/web &
api_pid=$!
python /app/docker/loopback_proxy.py &
proxy_pid=$!

cleanup() {
  kill -TERM "$api_pid" "$proxy_pid" "$sandbox_tunnel_pid" "$computer_tunnel_pid" 2>/dev/null || true
  wait "$api_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
  wait "$sandbox_tunnel_pid" 2>/dev/null || true
  wait "$computer_tunnel_pid" 2>/dev/null || true
}

trap 'cleanup; exit 0' INT TERM
wait "$api_pid"
status=$?
kill -TERM "$proxy_pid" "$sandbox_tunnel_pid" "$computer_tunnel_pid" 2>/dev/null || true
wait "$proxy_pid" 2>/dev/null || true
wait "$sandbox_tunnel_pid" 2>/dev/null || true
wait "$computer_tunnel_pid" 2>/dev/null || true
exit "$status"
