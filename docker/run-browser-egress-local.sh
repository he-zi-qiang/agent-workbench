#!/bin/sh
# The destination guard, alone in the one container that can leave (ADR-0112 §3.3).
#
# It holds nothing: no key, no database, no workspace, no Docker socket, and no
# browser. What a compromise of this process buys is the ability to make
# outbound requests -- which is the one thing it exists to do, under judgement.
# What it does not buy is anything the browser container holds, because nothing
# here reaches back into it.
#
# `AW_BROWSER_ALLOW_HOSTS` is a space-separated list of internal `host:port`
# destinations an operator wants reachable -- a dev server in this same project,
# typically. It is empty by default, and nothing in an MCP request can add to
# it: the list is parsed before the first connection is accepted.
set -eu

PROXY_PORT="${BROWSER_EGRESS_PROXY_PORT:-8771}"
CONTROL_PORT="${BROWSER_EGRESS_CONTROL_PORT:-8772}"

set -- --proxy-port "$PROXY_PORT" --control-port "$CONTROL_PORT"
for entry in ${AW_BROWSER_ALLOW_HOSTS:-}; do
    set -- "$@" --allow-host "$entry"
done

exec agent-browser-egress "$@"
