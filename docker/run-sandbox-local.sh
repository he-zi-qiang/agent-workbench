#!/bin/sh
# The sandbox broker: the one container that may speak to the Docker daemon.
#
# `sandbox_run` starts a `--network=none` container per call, so its server
# needs a daemon to ask. ADR-0105 refused to mount the socket into the
# services that exist, because a socket in the API's container is root on the
# VM for anything that gets into the API. ADR-0107 puts it here instead: a
# container that runs `agent-sandbox-mcp` and nothing else, holds no key, no
# database, no workspace and no model, and is reached by the others only
# through the loopback tunnel `docker/loopback_proxy.py` describes. What a
# compromise of *this* process buys is what the native path already hands the
# sandbox server -- the daemon -- and what it no longer buys is everything
# else those processes hold.
#
# Root inside, and that is the socket's doing rather than a relaxation: the
# daemon's socket is owned by root and mode 660, so a non-root user needs its
# group, and the group is `root` on Docker Desktop. `cap_drop: ALL` still
# applies -- uid 0 with no capabilities opens the socket by *owning* it, which
# is a DAC check, and can do nothing else uid 0 usually can.
set -eu

# Two ports, because a wildcard bind and a loopback bind on the same port
# collide. The server itself is loopback-only (its `--host` is a choice list),
# so the tunnel's outward end takes the port the other containers dial.
SERVER_PORT=8776
PUBLIC_PORT=8766

# The CLI would otherwise try to write its context store under $HOME, which
# is on the read-only root.
DOCKER_CONFIG=/tmp/docker-config
export DOCKER_CONFIG
mkdir -p "$DOCKER_CONFIG"

# The interpreter image, pulled once. `--network=none` means a call can never
# fetch it, and the executor never pulls -- a missing image is a failed call
# with the daemon's own message in the log. So it is fetched here, before the
# server answers anything, and a failure is said rather than deferred.
STOCK_IMAGE="python:3.12-slim"
if ! docker image inspect "$STOCK_IMAGE" >/dev/null 2>&1; then
    echo "sandbox: pulling $STOCK_IMAGE (once)" >&2
    if ! docker pull "$STOCK_IMAGE" >&2; then
        echo "sandbox: could not pull $STOCK_IMAGE; every call will fail until" >&2
        echo "         it is present. On the host: docker pull $STOCK_IMAGE" >&2
    fi
fi

# The richer image, used only when somebody built it (scripts\stack.cmd
# sandbox-image, or scripts/dev.sh sandbox-image). Said out loud either way,
# because a silent fallback is the shape of the bug that image exists to fix:
# a model that cannot produce a PDF, with nothing anywhere saying why.
PDF_IMAGE="${SANDBOX_PDF_IMAGE:-agent-workbench-sandbox-pdf:local}"
if docker image inspect "$PDF_IMAGE" >/dev/null 2>&1; then
    echo "sandbox image: $PDF_IMAGE (reportlab + CJK font available)" >&2
    IMAGE="$PDF_IMAGE"
else
    echo "sandbox image: the stock default -- scripts are limited to the standard library." >&2
    echo "  no PDF, no charts, no spreadsheets; --network=none means a script cannot install one." >&2
    echo "  to change that: scripts\\stack.cmd sandbox-image, then scripts\\stack.cmd restart" >&2
    IMAGE="$STOCK_IMAGE"
fi

LOCAL_PROXY_LISTEN_HOST=0.0.0.0 \
LOCAL_PROXY_PORT="$PUBLIC_PORT" \
LOCAL_PROXY_UPSTREAM_HOST=127.0.0.1 \
LOCAL_PROXY_UPSTREAM_PORT="$SERVER_PORT" \
    python /app/docker/loopback_proxy.py &
proxy_pid=$!

agent-sandbox-mcp --port "$SERVER_PORT" --image "$IMAGE" &
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
