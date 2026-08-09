#!/usr/bin/env bash
# Launch the Vite dev server for the Browser pane.
#
# The only thing this adds over `pnpm dev` is putting a working `node` on PATH:
# the Homebrew node on this machine is linked against a libsimdjson that is no
# longer installed, so a patched copy lives in the scratchpad instead.
set -euo pipefail

cd "$(dirname "$0")/../web"

NODE_DIR="/private/tmp/claude-501/-Users-heziqiang-Documents-Codex-2026-07-15-new-chat-agent-workbench/4fb13fb8-dc71-4521-aafb-54c6d42172a3/scratchpad/nodefix"
if [ -x "$NODE_DIR/node" ]; then
  PATH="$NODE_DIR:$PATH"
  export PATH
fi

exec node_modules/.bin/vite --host 127.0.0.1
