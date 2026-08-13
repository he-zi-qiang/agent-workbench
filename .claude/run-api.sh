#!/usr/bin/env bash
# Launch the API for the Browser pane.
#
# What this adds over `scripts/dev.sh demo-api` is reading the provider key from
# the untracked `API key/key` file at *runtime*, so the key stays in the one
# place it already lives. It is never copied into this file, into launch.json, or
# into any command line.
#
# `demo-api` rather than `api`: the pane serves the console, and a Task submitted
# from Work carries the tool names *this* process freezes into its envelope. On
# `config.local.toml` that envelope has no MCP tool at all, so "写一份 Word 报告"
# could only ever come back as Markdown -- which is exactly what it did. The API
# needs no MCP server running to freeze the names; the Worker is what needs them
# up, which is why `demo-worker` probes both before it starts.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${AW_SECRETS__DEEPSEEK_API_KEY:-}" ] && [ -r "API key/key" ]; then
  AW_SECRETS__DEEPSEEK_API_KEY="$(tr -d '[:space:]' < "API key/key")"
  export AW_SECRETS__DEEPSEEK_API_KEY
fi

# Chat's web search lives on exactly one branch: `routed` retrieved, scored, and
# found the corpus does not cover the question (ADR-021). Neither switch alone
# reaches it -- `fixed` never builds the tool, and `research.enabled = false`
# makes the tool `None` even under `routed`. Both are off in the tracked config
# for reasons that hold there (`fixed` is the shape the evals measure;
# `research.enabled` without a key is a startup error and this checkout is
# shared), and neither reason applies to this machine, which has the key.
export AW_RESEARCH__ENABLED="${AW_RESEARCH__ENABLED:-true}"
export AW_CHAT__RETRIEVAL_SHAPE="${AW_CHAT__RETRIEVAL_SHAPE:-routed}"

exec scripts/dev.sh demo-api "$@"
