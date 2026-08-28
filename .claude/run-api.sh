#!/usr/bin/env bash
# Launch the API for the Browser pane.
#
# This adds nothing to `scripts/dev.sh demo-api` and is kept only because
# launch.json needs a command to run. That emptiness is the point: it used to
# add three things, and each of them made the pane's console a different
# application from the one docs/running-locally.md tells you to start.
#
#   * it read the provider key from the untracked `API key/key`. It was the only
#     script in the checkout that did, so the documented start had no provider
#     at all -- and a keyless `demo-api` used to come up without Chat, without
#     the event stream, and with triage silently disabled, none of which is
#     visible from a browser. `scripts/dev.sh` now reads the key itself, from
#     AW_KEY_FILE (default ~/.config/agent-workbench/key), outside the checkout
#     where no `zip -r` of this directory can pick it up;
#   * it exported AW_CHAT__RETRIEVAL_SHAPE=routed. That is a capability of the
#     console profile, so it lives in config/config.demo-local.toml now. It is
#     why Chat searched the web when started from here and did not when started
#     from the documented command;
#   * it exported AW_RESEARCH__ENABLED=true, which the `demo-api` arm sets for
#     itself on the one condition that makes it safe -- a provider key being
#     present, which that arm now requires rather than hopes for.
#
# `demo-api` rather than `api`: the pane serves the console, and a Task submitted
# from Work carries the tool names *this* process freezes into its envelope. On
# `config.local.toml` that envelope has no MCP tool at all, so "写一份 Word 报告"
# could only ever come back as Markdown -- which is exactly what it did. The API
# needs no MCP server running to freeze the names; the Worker is what needs them
# up, which is why `demo-worker` probes both before it starts.
#
# 曾经并排放过一条 `agent-api-delegating`，唯一差别是带上
# `AW_MULTI_AGENT__DELEGATION_ENABLED=true`。**已经删掉**：那个变量补的是
# `config.demo-local.toml` 自己该表态的事，而它现在表了态（`delegation_enabled =
# true`）。留着它等于给同一个控制台留两条启动路径，其中一条悄悄比另一条能力更强——
# 而「这个部署有没有多 agent」应该看 profile，不该看你用哪一行命令起的它。
set -euo pipefail

cd "$(dirname "$0")/.."

exec scripts/dev.sh demo-api "$@"
