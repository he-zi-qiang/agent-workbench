#!/usr/bin/env bash
# Launch the Vite dev server for the Browser pane.
#
# The only thing this adds over `pnpm dev` is putting a working `node` on PATH:
# the Homebrew node on this machine is linked against a libsimdjson that is no
# longer installed, so a patched copy lives in the scratchpad instead.
set -euo pipefail

cd "$(dirname "$0")/../web"

# Candidates in order of durability. The scratchpad copy came first
# historically, but it lives in *another* session's directory and is deleted
# whenever that session is cleaned up -- at which point this script failed with
# a bare "node: command not found" that says nothing about why. The repository
# copy under var/ (gitignored) outlives sessions, so it is tried first and the
# scratchpad remains only as a fallback for checkouts that have not made one.
for node_dir in \
  "$PWD/../var/toolchain" \
  "/private/tmp/claude-501/-Users-heziqiang-Documents-Codex-2026-07-15-new-chat-agent-workbench/4fb13fb8-dc71-4521-aafb-54c6d42172a3/scratchpad/nodefix"
do
  if [ -x "$node_dir/node" ]; then
    PATH="$node_dir:$PATH"
    export PATH
    break
  fi
done

if ! command -v node >/dev/null 2>&1; then
  echo "no working node on PATH. The Homebrew one on this machine is linked" >&2
  echo "against a libsimdjson that is no longer installed; put a working copy" >&2
  echo "at var/toolchain/node (see docs/running-locally.md)." >&2
  exit 1
fi

exec node_modules/.bin/vite --host 127.0.0.1
