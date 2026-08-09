# 本地 Word MCP：从协议探测到真实 Task

这条路径实现的是项目自己的 Word 能力：writer 调用
`mcp_word_render_document`，`.docx` 经 Tool Gateway 和 MCP 结果映射进入 ArtifactStore。
它不调用 Codex Documents skill，也不自动操作桌面版 Microsoft Word。架构决定见
[ADR-026](./adr/0026-word-docx-is-an-mcp-artifact.md)。

## 1. 启动、停止与探测

先安装项目依赖。在一个独立终端前台启动 loopback server：

```bash
scripts/dev.sh word-server
```

服务只监听 `127.0.0.1:8765`。按 `Ctrl-C` 停止；不写 PID 文件，也不在后台留下一个之后
忘记关闭的文档进程。

另一个终端运行一次完整探测：

```bash
scripts/dev.sh word-check
```

这条命令先请求 `http://127.0.0.1:8765/health`，再通过官方 MCP Client 对
`http://127.0.0.1:8765/mcp` 完成初始化和 `tools/list`，并断言目录中存在
`render_document`。成功输出的形状是：

```text
health  200 http://127.0.0.1:8765/health
mcp     http://127.0.0.1:8765/mcp
tools
  render_document: ...
```

只看 HTTP 健康不足以证明 MCP 目录有效，只跑 `tools/list` 又不容易区分进程未启动与协议
错误，所以检查命令保留两项结果。

## 2. 本地 profile 的能力边界

`config/config.word-local.toml` 已显式开启；普通 `config/config.local.toml` 保持 MCP
关闭：

```text
alias              word
remote tool        render_document
local tool         mcp_word_render_document
principal scope    mcp:word
endpoint           http://127.0.0.1:8765/mcp
retryable effects  true
opening tool call  optional
```

API 根据配置冻结新 Task 的工具上限；Task Worker 在启动时发现实际目录并取交集。因此启动
顺序是承重的：**先起 Word MCP，再起 Worker**。Server 后启动或中途增加工具都不会热更新
已经运行的 Worker。

## 3. 无模型 key：只能验 MCP，不是假装跑了 Task

下面两条在没有 `AW_SECRETS__DEEPSEEK_API_KEY` 时成立：

```bash
scripts/dev.sh word-server
scripts/dev.sh word-check
```

但 `scripts/dev.sh word-worker` 会拒绝启动并明确打印缺少 key；它不会悄悄降级成 demo
graph。常规 `scripts/dev.sh worker` 的 demo graph 同样不运行真实 writer，也不会调用 Word
MCP。健康与目录通过不能写成“Agent 已生成 Word 文档”。

## 4. 真实 Task 验收

准备 PostgreSQL 并迁移：

```bash
scripts/dev.sh services
scripts/dev.sh migrate
```

导出真实 Provider key；本地 profile 已钉住 `deepseek-chat`，不要把 key 写进仓库：

```bash
export AW_SECRETS__DEEPSEEK_API_KEY=sk-...
```

按顺序在三个终端运行：

```bash
scripts/dev.sh word-server
scripts/dev.sh word-api --without-chat
scripts/dev.sh word-worker
```

`--without-chat` 只让这个 API 作为轻量 Task 控制面运行，避免为 Word 演示加载整套 BGE；
模型调用发生在真实 Task Worker。`word-worker` 会先执行和 `word-check` 相同的 MCP
preflight，Word Server 不可达或缺少工具就拒绝启动；通用 ADR-025 Adapter 的 fail-soft
语义不变，但显式 Word 演示命令不允许“进程活着却没有 Word”。`word-api` 与
`word-worker` 会选择同一 `config.word-local.toml`；不要混用常规 `api/worker` 命令，
否则 API 提交时的信封与 Worker 目录会来自不同 profile。

提交时把 scope 放在 `task` 子命令之后、`submit` 之前；`--scope` 可重复：

```bash
PYTHONPATH=src .venv/bin/python -m agent_workbench.apps.cli.main task \
  --tenant-id tenant_local \
  --principal-id user_local \
  --scope mcp:word \
  --scope artifact:export \
  submit \
  --objective "请调用 render_document，生成一份包含标题、摘要和三节正文的中文 Word 项目周报" \
  --json
```

记下返回的 `task_id`，观察时间线：

```bash
PYTHONPATH=src .venv/bin/python -m agent_workbench.apps.cli.main task \
  --tenant-id tenant_local \
  --principal-id user_local \
  --scope mcp:word \
  --scope artifact:export \
  timeline <task_id> --json
```

如果 Task 停在 `waiting_approval`，按[本地运行手册](./running-locally.md)列出并批准最终导出。
验收不能只看最终状态，至少核对：

1. writer 的 `RunStarted.tool_names` 含 `mcp_word_render_document`；
2. 同一 run 出现 `ToolProposed → PermissionResolved → ToolStarted → ToolCompleted`；
3. `ToolCompleted.artifact.media_type` 是 Word OOXML 类型，artifact id 可由提交者下载；
4. 去掉 `mcp:word` 后，同一工具在 Gateway 被拒绝；换另一个 principal 后 artifact 下载被
   拒绝。

得到 `artifact_id` 后下载到显式的新文件，不覆盖已有文档：

```bash
PYTHONPATH=src .venv/bin/python -m agent_workbench.apps.cli.main artifact \
  --tenant-id tenant_local \
  --principal-id user_local \
  get <artifact_id> --output ./generated-report.docx
```

## 5. 常见问题

**健康通过、Worker 却没有 Word 工具。** Worker 早于 Server 启动，或启动发现时目录不合规。
先运行 `word-check`，再重启 Worker；目录只冻结一次。

**Task 使用 demo graph。** 当前 shell 没有 Provider key。重新导出 key 后重启 Worker；不能在
一个已启动的 demo Worker 上动态切换。

**时间线出现 `PermissionResolved` deny。** 提交请求缺 `mcp:word` scope。scope 属于
principal，不是模型参数，也不能由 objective 自己声明。

**为什么不直接保存到 Downloads。** MCP Server 不接收任意输出路径。文档先按 tenant/owner
写进 ArtifactStore，授权调用者再通过 Artifact API 选择下载位置；这是安全边界，不是绕路。
