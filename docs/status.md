# 实施状态

## 文档基线

状态：**已纳入 Git 版本管理**。

- [架构与技术选型基线 v1.3](./architecture-baseline.md)；
- [代码实施计划 v1.0](./implementation-plan.md)；
- [配置管理契约 schema 1.1](./configuration.md)。

这些文档描述目标架构和增量计划，不代表其中列出的产品能力已经实现。

## PR-001 Bootstrap

状态：**已实现并通过本地测试，已纳入 Git 版本管理**。

已交付：

- Python 3.12 src-layout 包；
- `pyproject.toml` 和 `uv.lock`；
- 已迁移并适配正式目录的配置基线；
- 脱敏的 `agent-config-check` 入口；
- clean-room 与合规文档；
- 使用 `main` 分支的独立 Git 仓库。

2026-07-23 验证证据：

```text
uv 0.11.31
Python 3.12.13
uv lock --check --offline: passed
pytest: 43 passed
agent-config-check test profile: status=ok
non-editable install with packaged default config: passed
```

仍不属于 PR-001 的内容：

- CI 和配置 ownership 架构测试；
- API、进程 Container 和 readiness；
- Runtime、Domain 与 Ports；
- Persistence、RAG、Workflow 与 Multi-Agent；
- Docker Compose 和外部服务连通性。

## PR-002 Config CI

状态：**已实现并通过本地同构检查**。

已交付：

- 覆盖 230 个 Settings 叶子字段的 `config/ownership.yaml`；
- ownership 唯一性、生命周期和 Task snapshot 正向 allowlist 架构测试；
- 核心层框架依赖、反向依赖、原始配置读取和 `os.environ` 边界测试；
- development、test、production 三个离线配置 profile；
- Ruff、严格 Pyright（产品源码）、pytest、许可证与 Git 历史密钥扫描 CI；
- GitHub Actions 与下载型工具都固定到 release SHA 或 SHA-256；
- 删除没有消费者的 `admin_token`、`webhook_token` 配置入口。

2026-07-23 本地验证证据：

```text
uv lock --check --offline: passed
ruff format --check: passed
ruff check: passed
pyright: 0 errors, 0 warnings
pytest: 60 passed
development/test/production config profile contract tests: passed
dependency license allowlist: passed
Gitleaks 8.30.1 working-tree/history scan: passed
```

该 PR 只证明配置和 CI 合同，不代表 API、Worker、模型或外部 Adapter 已经
启动。

## PR-003 Domain

状态：**已实现并通过本地测试**。

已交付 `src/agent_workbench/domain/`，只依赖标准库与 Pydantic：

- `schema.py`：`DomainModel`/`VersionedModel` 基类、schema 版本闭合校验、
  `extra="forbid"`、frozen 与 `hide_input_in_errors`；
- `identifiers.py`：平台自铸 ID 前缀与生成器，同时接受 provider 原样
  ID（`toolu_...`）；
- `errors.py`：13 个稳定 `ErrorCode`、`ErrorInfo` 与领域异常层级；
- `artifacts.py`：`ArtifactRef`（content-addressed，不含 URL/路径）；
- `messages.py`：provider-neutral 消息与 text/tool_use/tool_result 块；
- `tools.py`：`ToolSpec`/`ToolCall`/`ToolResult` 与 `align_results()`；
- `context.py`：`ContextPacket`/`ContextChunk`/`Citation`/`SourceLocator`；
- `policies.py`：`PrincipalContext`、`AuthorizationEnvelope`、
  `ExecutionContext`、`PolicyDecision`；
- `runs.py`：`TraceContext`、`RunBudget`/`BudgetUsage`/`TokenUsage`、
  `AgentRunRequest`、`AgentOutcome`、Runtime 状态机与 `StopReason` 词汇表；
- `events.py`：基线 6.7 的 19 种事件、统一 envelope 与 durability 映射。

已经被编码为构造期校验（而不是注释）的不变量：

- 每个 `ToolCall` 恰有一个 `ToolResult`，提交顺序按模型调用顺序稳定；
- write/external/destructive Tool 必须 exclusive 且声明 permission scope；
- 失败 `ToolResult` 必须携带 `ErrorInfo`，成功的必须不携带；
- `ModelDelta`/`ToolProgress` 恒为 transient 且不得携带 stream sequence，
  durable 事件必须携带；`event_type` 必须与 payload 一致；
- Citation 必须落在同一 `ContextPacket` 的 chunk 与相同 document version 上；
- 预算耗尽的 run 报告为 failed + 结构化 `stop_reason`，不伪装成 completed；
- 第三方异常转 `ErrorInfo` 时只保留异常类型名，丢弃消息正文。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 219 passed（其中 domain 新增 159 项）
development/test/production config profile: status=ok
golden 序列化基线：tests/domain/golden/domain_v1.json（13 个聚合）
```

本 PR 不新增依赖，`pyproject.toml` 与 `uv.lock` 未改动，`uv lock --check`
由 CI 执行。

仍不属于 PR-003 的内容：Ports 与 Fake Adapter（PR-004）、CLI 纵向切片
（PR-005）、Runtime 循环本身（PR-006 起）。领域对象存在且有契约测试，
按项目纪律只能标记为 Implemented/Tested，不能标记为 Demonstrated。

## PR-004 Ports + Fakes

状态：**已实现并通过本地测试**。

`src/agent_workbench/ports/`（框架无关 Protocol）：

- `model.py`：`ModelRequest`、`ModelEvent`（text_delta / tool_call / usage /
  completed）、`ModelPort.stream()`；
- `tools.py`：`ToolHandler`、`ToolInvocation`、`ToolBinding`、`ToolRegistry`；
- `agent_executor.py`：`AgentExecutor.run(request, emit, cancellation)`；
- `policy.py`：`PolicyEngine.decide(call, context)`；
- `event_log.py`：`EventScope`、`EventCursor`、`EventLogPort`、`EventSink`；
- `conversation_store.py`：`ConversationSession`、`StoredMessage`、
  `ConversationStore`；
- `artifact_store.py`：`ArtifactStore`；
- `cancellation.py`：`CancellationToken` 协议、`CancellationSource`、
  `NullCancellationToken`。

`src/agent_workbench/adapters/`（无外部依赖的实现）：

- `models/fake.py`：脚本化 `FakeModel`（可重放最后一轮，供死循环测试）；
- `tools/`：`StaticToolRegistry` 与两个无副作用 Tool（`read_document`、
  `text_statistics`）；
- `memory/`：`InMemoryEventLog`、`InMemoryConversationStore`、
  `InMemoryArtifactStore`；
- `policy/envelope.py`：`EnvelopePolicyEngine`（deny-by-default 的过渡实现）；
- `events.py`：`ScopedEventSink`；
- `testing.py`：`fake_stack()` 组合出完整栈，字段类型全部写成 Port，
  由 Pyright 静态验证结构一致性。

本 PR 固定下来的行为：

- **sequence 由 log 分配**，durable 事件在流内单调无洞，transient 事件返回后
  即丢弃且不占用 sequence——SSE cursor 因此是可信的续传位置；
- **跨租户读与不存在完全同形**（同一 `NotFoundError`、同一文案），
  artifact 与 conversation 都有针对性测试；
- `ModelRequest` 只携带 model profile，不含 model id / temperature / key；
- Tool call id 从 provider 原样穿过 proposal → 执行 → tool 消息 → 下一次
  ModelRequest（`test_fake_stack.py` 端到端断言）；
- `EventCursor.decode()` 对畸形输入统一 fail closed，且不解释拒绝原因。

领域侧的唯一改动：新增 `not_found` 错误码与 `NotFoundError`，用于统一
"不存在 / 无权知道它存在"两种情况。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 290 passed（contracts 新增 71）
development/test config profile: status=ok
```

测试用 `asyncio.run()` 驱动协程，**不引入 pytest-asyncio**，
`pyproject.toml` 与 `uv.lock` 保持不变。

按各自工作包推迟的 Port（不在本 PR 冻结）：`IngestionPort`、
`RetrieverPort`、`EmbeddingPort`、`SparseEncoderPort`、`RerankerPort`、
`DocumentStore`、`OutboxPort`（WP03–WP05）；`TaskRegistry`（WP07）；
`ApprovalStore`（WP10）；`TelemetryPort`（WP12）；`SandboxPort`、
`MemoryPort`（更后）。先冻结一个没有实现来校验的协议，正是契约在第一个
使用者出现前就漂移的方式。
