# 实施状态

## 文档基线

状态：**已纳入 Git 版本管理**。

- [架构与技术选型基线 v1.3](./architecture-baseline.md)；
- [代码实施计划 v1.0](./implementation-plan.md)；
- [配置管理契约 schema 1.2](./configuration.md)；
- [2026-07-25 仓库核验报告](./repository-audit-2026-07-25.md)。

这些文档描述目标架构和增量计划，不代表其中列出的产品能力已经实现。

## 2026-07-25 仓库核验总览

核验基线：`main@f071323`，PR-001～PR-015 与 ADR-012 已合并。当前配置登记
231 个 Settings 叶子字段、47 个组。

已经实现并有测试证据：

- Domain、Ports、Fake Adapter、可复现 CLI；
- 自研 Runtime 的串行 Tool Loop、Policy/Tool Gateway、预算与取消、并行只读、
  exclusive 屏障和 Hook Bus；
- DeepSeek 流式 HTTP Adapter 的离线 contract tests；
- PostgreSQL ConversationStore、Document/Version/ACL、事务 Outbox；
- Local ArtifactStore 与 Upload / Artifact / Health API。

明确尚未实现：

- Chat RAG、LlamaIndex、Qdrant、BGE、RAGAS；
- LangGraph Task、Task Registry、lease/fencing/checkpoint、Multi-Agent；
- 生产身份认证、S3、Worker、SSE、UI、Docker Compose；
- DeepSeek 的进程级装配和真实服务 E2E。

本次门禁共收集 544 项测试。无数据库环境为 499 passed、45 skipped；PostgreSQL
16 集成套件为 160 passed，Alembic `0001 → 0002` 通过。两组测试有重叠，不能
相加。Ruff、Pyright、三 profile、CLI golden、许可证、Gitleaks 与 Actionlint
均通过；GitHub Actions 证据见
[run 30184299195](https://github.com/he-zi-qiang/agent-workbench/actions/runs/30184299195)。

当前阻断项：

1. ~~默认 `api.host = "0.0.0.0"`，开发 Header Identity Resolver 可能被本机
   之外的调用者访问并伪造身份~~（2026-07-26 已修复，见下方 P0-1 一节）；
2. ~~`requires_approval=True` 没有阻止 write tool 执行~~（2026-07-26 已修复，
   见下方 P0-2 一节）；
3. ~~Upload/Document/Artifact 缺少同 tenant 内的 owner/ACL 对象授权~~
   （2026-07-26 全部修复，见下方 P1-1、P1-3、P1-2 三节）；
4. ~~tool/token/cost budget 不是硬上限~~（2026-07-26 已修复，见下方 P1-5 一节）；
5. ~~Policy 改写可绕过参数字节上限，Policy/Hook deadline 不完整~~
   （2026-07-26 已修复，见下方 P1-6 / P1-7 一节）；
6. ~~DeepSeek 对损坏 SSE frame fail open，Artifact 下载不是真正流式~~
   （2026-07-26 均已修复，见下方 P1-9 / P1-4 两节）；
7. ~~Outbox claim 没有 lease/fence，worker 崩溃后不可恢复~~
   （2026-07-26 已修复，见下方 P1-10 一节）。

**七条阻断项已全部关闭**，核验报告的 14 条缺陷同样全部修复并附回归测试。每一条的
触发条件、判断依据、有牙验证结果与**明确留下的缺口**记在下面各自的小节里；报告的
§7 是同一份记录的另一半。

完整触发条件、文件位置和建议修复顺序见
[仓库核验报告](./repository-audit-2026-07-25.md)。核验那一轮只校正文档，没有改
生产逻辑；此后的修复各自一个 PR，逐条记在下面，并同步回核验报告的第 7 节。
纪律是：**没有覆盖触发条件的回归测试，任何一条都不算关闭。**

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

## PR-005 CLI Skeleton

状态：**已实现并通过本地测试**。

第一个跑通的纵向切片：输入 → 脚本化模型 → 统一事件 → 输出。

已交付：

- `src/agent_workbench/apps/cli/`：`main.py`（argparse 与退出码）、
  `demo.py`（确定性场景装配）、`rendering.py`（文本与 JSONL 两种渲染器）；
- `agent-cli` console script，CI 用已安装的 wheel 跑 smoke 并与 golden 比对；
- `src/agent_workbench/adapters/agents/single_turn.py`：
  `SingleTurnAgentExecutor`，满足 `AgentExecutor` 协议的 walking skeleton；
- `adapters/events.py` 增加 `ObservingEventSink`（live tee，让订阅者看到
  transient 事件）；`InMemoryEventLog` 与 `fake_stack()` 增加可注入的
  `event_ids`，与既有 `clock` 注入同一目的；
- 领域侧新增 `canonical_arguments()` / `argument_digest()`：事件记录参数摘要
  而不是参数本身，WP10 的副作用 ledger 复用同一份规范形式。

本 PR 固定下来的行为：

- **CLI 只消费统一事件与返回的 `AgentOutcome`**，不触碰 executor、模型
  adapter 或 store 的内部；
- **两个视图取自不同来源**：流式回答来自 live transient delta，时间线来自
  运行结束后对 durable log 的重放。重放拿不到的东西，重连的客户端也拿不到；
- **skeleton 不拥有 tool loop，并且拒绝得很大声**：模型提出工具调用时，先落
  一条 durable `ToolProposed`，再让整个 run 失败。默默丢弃会让模型永远等一个
  不会到来的 ToolResult，正是不变量 1 要防的情况；
- 预算在开工前检查（过期 deadline 时模型调用次数为 0）；
- adapter 异常变成结构化 outcome，且只保留异常类型名——
  测试用 `sk-ant-` canary 断言 provider 异常正文不进入 outcome 与事件；
- `max_tokens` 截断的回答报告为 failed，不伪装成 completed；
- 事件里没有 prompt 正文，测试用 canary prompt 断言 JSONL 中不出现。

demo 模式冻结时钟、用计数器生成事件 ID，因此**同一条命令的输出逐字节可复现**，
由 4 个 golden 文件（文本/JSONL × 完成/拒绝）守护。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 328 passed（cli 19 + 单轮 executor 19）
development/test config profile: status=ok
agent-cli demo / --propose-tool read_document: 退出码 0 / 1
```

`pyproject.toml` 只增加 `agent-cli` 入口，未新增依赖，`uv.lock` 未改动。

范围说明：demo 不加载 Settings。它不连数据库、向量库和任何 provider，要求注入
DSN 才能看一句脚本化回答属于仪式而非安全；从校验过的配置做真实依赖注入是
`bootstrap/container.py` 的职责，与第一个真实 adapter 一起落地。真正的
model-tool 循环仍属于 WP02（PR-006 起），本 PR 没有、也不允许提前实现第二个
循环。

## PR-006 Runtime Serial Loop

状态：**已实现并通过本地测试**。

自研 `ClaudeLikeAgentRuntime` 落地，替换 PR-005 的 walking-skeleton executor
（`SingleTurnAgentExecutor` 及其测试已删除，不与新 Runtime 并存）。

已交付 `src/agent_workbench/runtime/`，只依赖 domain 与 ports：

- `state.py`：把基线 7.1 的状态机写成可执行转移表 + `RunStateMachine`。
  非法转移抛 `InvalidStateTransition`，因此“跳过授权直接执行”这类实现错误
  在第一次发生时就会失败，而不是产出一条看起来合理的运行记录；
- `tool_executor.py`：单次已授权调用的执行原语，负责超时与异常归一化；
- `agent_runtime.py`：串行 `model → tool → result → model` 循环本体。

本 PR 固定下来的不变量：

- **每个已暴露的 `tool_call_id` 恰有一个 `ToolResult`**——未知工具、被拒、
  handler 抛异常、超时、批次中途取消，全部产出结果而不是留空。少一个结果，
  模型就会永远等一个不会到来的回答；
- **提交顺序永远是模型的调用顺序**：串行执行下两者本就一致，仍然强制过一次
  `align_results()`，这样 PR-009 的并行调度器无法通过“谁先跑完”改变模型看到
  的内容；
- **超时属于 executor 而不是循环**：串行循环没有别的办法从一个不返回的
  handler 里恢复，预算约束的是花费不是单次 await 的时长；
- **Tool 只有经过 PolicyEngine 才会执行**：deny 分支下 handler 调用数为 0；
  `allow_with_modified_input` 目前直接拒绝——重写后的参数必须重新做 schema
  校验与再授权，而做这件事的 Tool Gateway 还没到，所以宁可拒绝也不在未校验
  的输入上执行；
- **循环有上限**：`max_steps` / `max_tool_calls` / deadline 在每轮开工前检查，
  一个永远提工具调用的模型会被 `max_steps` 终止（有测试）；
- handler 越权回答别的 `tool_call_id` 时，只让它自己那次调用失败，不会同时
  破坏两个 id 的配对。

CLI demo 随之升级为完整一轮：`agent-cli demo` 现在演示
模型 → `read_document` → ToolResult → 模型 → 回答；`--deny` 演示 deny 分支
（handler 不被调用，模型仍收到该 id 的答复）；`--tool none` 回到单轮文本。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 360 passed（runtime 新增 49）
development/test config profile: status=ok
agent-cli demo / --deny / --tool none: 退出码 0
```

`pyproject.toml` 与 `uv.lock` 未改动。测试套件里有且只有一处等待真实时间：
tool 超时测试固定耗时 1 秒（`ToolSpec.timeout_seconds` 的域下限就是 1 秒），
是有界且确定的，不是靠 sleep 碰运气的竞态测试。

范围说明：本 PR 不含 **schema 校验**与 **Hook 重校验**（两者一起定义“最终参数”
的含义，属于 PR-007 Tool Gateway）、**并行只读调度**（PR-009）、大结果
Artifact 化（WP12）、真实 Anthropic Adapter（WP02-06）。因此基线不变量 2 目前
只实现了授权那一半；demo 中的两个 Tool 自己校验参数类型，`invalid_tool_input`
路径由它们覆盖。

## PR-007 Policy + Tool Gateway

状态：**已实现并通过本地测试**。

把“这次调用能不能跑”收进唯一的 `ToolGateway`，并补上基线不变量 2 与 3
缺的那一半：schema 校验与参数被改写后的重新校验/再授权。

已交付：

- `runtime/schema_validation.py`：一个**明确划定范围**的 JSON Schema 子集
  （type / properties / required / additionalProperties / items / enum /
  minimum / maximum / minLength / maxLength / pattern / minItems / maxItems）。
  超出子集的 schema 在**装配阶段**被拒绝，而不是在调用时被静默跳过——一个会
  忽略 `oneOf` 的校验器，会把每次调用都报成合法却什么都没校验；
- `runtime/tool_gateway.py`：`propose / prepare / authorize / invoke / refuse`
  五个阶段方法 + `advertise()`。Runtime 的批次循环退化成对它的编排，状态机
  仍留在 Runtime；
- `agent_runtime.py` 相应瘦身：不再直接持有 registry / policy / executor，
  只依赖 `ToolGateway`（构造签名随之简化为 `model` + `gateway`）。

本 PR 固定下来的行为：

- **handler 只在“最终参数”同时通过 schema 校验与授权决定之后才运行**。
  “最终”是关键词：策略返回 `allow_with_modified_input` 时，重写后的参数会被
  重新校验并重新提交决定；能在检查之后改参数，就等于同时绕过两道检查；
- **重写循环有界**（默认 3 轮）。一直改参数的引擎会被拒绝，而不是被无限重试；
- **重写成非法参数时调用被拒**，handler 调用数为 0（有测试）；
- **顺序可观测**：schema 校验先于授权，所以参数非法的调用连 `PermissionResolved`
  都不会产生——`tests/runtime/test_agent_runtime.py` 直接断言这条时间线；
- **参数体积上限**（默认 65536 字节，对应 `policy.max_tool_argument_bytes`）在
  解析之前拦截；
- **校验错误只报路径和期望，不报值**：这些消息会进入事件、运维日志和模型自己的
  上下文，用 canary 测试守住；
- Gateway 同时拥有一次调用的审计轨迹（proposal / permission / start /
  completion / failure），事件因此不可能和它实际做的事不一致。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 398 passed（新增 schema 24 + gateway 14）
development/test config profile: status=ok
CLI golden 文件逐字节未变——这次重构对 demo 行为完全等价
```

`pyproject.toml` 与 `uv.lock` 未改动：JSON Schema 子集是自研的，没有引入
`jsonschema` 依赖。工具 schema 都写在本仓库里，为了 8 个关键字引入一个完整
实现并不划算；代价是子集必须显式，扩展它是这个文件里的一次自觉修改。

范围说明：不含 **Hook Bus**（WP02-05，`ports/hooks.py` 与 `runtime/hook_bus.py`
尚未存在）——目前唯一能改写参数的来源是 PolicyEngine，重新校验/再授权的骨架
已经就位，接 Hook 时复用同一条路径。同样不含并行只读调度（WP02-04 / PR-009）、
大结果 Artifact 化（WP12）与真实 Model Adapter（WP02-06/07）。

## PR-008 Runtime Budgets

状态：**已实现并通过本地测试**。

PR-006 已经做掉了循环上限（`max_steps` / `max_tool_calls` / token / cost）与
轮次边界上的取消。本 PR 补的是**时限**：在实施计划 5.2 之前，一个停住不动的
模型流会让整个 run 无限期挂起——预算约束的是花费，不是单次 await 的时长。

已交付：

- `runtime/budgets.py`：纯粹的 deadline 运算。
  `effective_model_deadline()` 返回各层下界的最小值**以及它来自哪一层**；
  `effective_tool_timeout()` 用 run 剩余时间约束单个工具；
- Runtime：单次模型调用套 `asyncio.timeout(有效 deadline)`；流式过程中在每个
  事件边界检查取消；退出时显式 `aclose()` 生成器；
- ToolExecutor / Gateway：`run_budget_seconds` 一路传到执行处。

本 PR 固定下来的行为：

- **有效 deadline 是一个下界，不是几个互不相关的定时器**：
  `min(运行时 envelope, run 剩余时间)`。模型 profile 自己的超时由 Adapter 在更
  内层施加——只有 Adapter 知道 profile 映射到哪个具体模型——两层嵌套，短的先触发；
- **哪一层到期决定了怎么报**：run deadline 到期是预算结果（`budget_exceeded` +
  `stop_reason="deadline"`，run 结束）；超出 envelope 是 provider 问题
  （`provider_error`，`retryable=True`）。两者相等时按 run deadline 报，因为它的
  到期后果更严重。同一个"模型卡住"的场景有两个测试，只有配置不同、结论不同；
- **工具不能活得比批准它的 run 更久**：`min(工具声明超时, run 剩余时间)`，
  且每次调用重新计算——第一个工具跑得慢，第二个能用的就更少；
- **取消在下一个事件边界生效**，并通过关闭生成器传导到 Adapter（测试用生成器
  `finally` 里的标志位断言 `closed is True`）。模型完全静默时由 deadline 兜底，
  所以"有界时间内停止"两条路径都有覆盖；
- 卡住的模型调用**不会**产生 `ModelCompleted`——它确实没有完成。

取消测试不使用 sleep：cancel 是在事件 sink 观察到第一个 `ModelDelta` 时发出的，
因此交错是确定的。三个超时测试各等 50 毫秒真实时间，有界且必然触发。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 414 passed（新增 budgets 12 + runtime 5）
development/test config profile: status=ok
CLI golden 文件逐字节未变
```

范围说明：三层 retry 计数（model / graph node / task）中只有 model 这一层的
deadline 在本 PR 内；node 与 task 层属于 WP06/WP08。真实 Adapter 的
`max_retries` 要等 WP02-06 的 Anthropic Adapter。Hook Bus（WP02-05）与并行只读
调度（WP02-04）仍未实现。

## PR-009 Parallel Reads

状态：**已实现并通过本地测试**。

补上基线不变量 4 与 5：`parallel` 工具可以并发，`write/external/destructive`
必须过 exclusive 屏障；执行顺序与提交顺序彻底分离。

已交付：

- `runtime/tool_scheduler.py`：`plan_tool_batches()` 是一个**纯函数**——
  接收一批已授权调用，返回执行分组。并发 bug 难观察更难复现，所以"谁能和谁
  一起跑"这个决定放在不需要事件循环就能验证的代码里；
- Runtime 执行阶段按组推进：组内 `asyncio.gather`，组间顺序执行。

分组规则只有两条：

1. **保持模型的顺序**：从左到右扫描，连续的 parallel 调用累积成一组，因此
   排在写操作之前的读，绝不会在它之后执行；
2. **exclusive 调用自成一组**：写/外部/破坏性工具在领域层就被强制为
   exclusive（`ToolSpec` 构造期校验），所以"副作用要过屏障"从文档里的一句话
   变成了内存里的形状——它两侧都不会有东西在飞。

本 PR 固定下来的行为：

- **提交顺序永远是模型的调用顺序**，与完成顺序无关。测试用一对握手 handler
  证明：`waits` 必须等 `opens` 打开闸门才能返回，所以完成顺序是
  `[fast, slow]`，而提交给模型的顺序是 `[slow, fast]`；
- **并发是被证明的，不是被假设的**：上面那对 handler 在串行调度下会互相
  等死（由工具超时兜底），因此这个测试同时证明了"确实并发"和"顺序稳定"；
- **exclusive 工具旁边永远没有别人**：探针记录同时在场的 handler 数，
  写工具那一格恒为 1；
- `max_parallel_read_tools`（对应配置同名字段，默认 4）限制同时在场数量；
- 取消发生在组与组之间：**已在飞的组保留真实结果**，尚未开始的组拿到
  cancelled 结果——取消停止未开始的工作，不改写已经发生的事。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 428 passed（新增 scheduler 9 + runtime 5）
development/test config profile: status=ok
CLI golden 文件逐字节未变
```

范围说明：CLI demo 仍然只调用一个工具。M1 验收里"两个只读 Tool"的固定演示
留到评测/演示工作包——golden 文件是逐字节比对的，把它钉在并发执行的事件交错上，
等于承诺一个调度器并不保证的顺序。并发性由上面的握手测试证明，那是更强的证据。

不含 Hook Bus（WP02-05）与真实 Model Adapter（后来确定为 DeepSeek，
WP02-06/07）。WP02 至此
只剩这两项。

## PR-010 Hook Bus

状态：**已实现并通过本地测试**。

补上 WP02-05：部署方提供的 Hook 可以在工具调用被判定之前检查、改写或拦截它。
WP02 至此只剩真实 Model Adapter。

> 编号说明：原计划 §9 的首批 PR 列表把 Hook Bus 留在 WP02 内部未编号，
> PR-010 是 PostgreSQL/Artifact Base。实施时把 Hook Bus 排进 PR-010，其后
> Provider 占用 PR-011～PR-012，持久化与上传车道落在 PR-013～PR-015，
> 计划文档已同步。

已交付：

- `ports/hooks.py`：`ToolCallHook` 协议与 `HookOutcome`（不变 / 改写参数 /
  拦截）。Hook **返回决定，不修改传入的调用**；能改的只有 arguments——
  工具名和 call id 属于模型的请求和那条必须回答它的结果；
- `runtime/hook_bus.py`：按注册顺序跑一遍，每个 Hook 看到的是上一个产出的结果。

本 PR 固定下来的行为：

- **Hook 改写过的参数重新走 schema 校验，然后才进授权**——能在检查之后改参数，
  就等于绕过检查。这条路径和 PolicyEngine 的 `allow_with_modified_input`
  复用同一套 Gateway 逻辑（PR-007 就是按这个形状搭的）；
- **失败即拦截，不是忽略**：Hook 抛异常或超时，被检查的那次调用直接被拦下。
  反过来——把坏掉的 Hook 当成放行——会让部署方写的每条安全规则在出 bug 的
  瞬间静默消失；
- **Hook 有时限**（默认 5 秒），和它守护的工具同样理由：一个无界 await 就能
  让一条慢规则拖住整个 run；
- **只跑一遍**：后面的 Hook 改写不会重新触发前面的 Hook。多轮需要一条收敛
  规则，而说不清的规则等于没人能 review 的规则；
- **Hook 看不到已经失败的调用**：未知工具、参数非法的调用在 Hook 之前就被拒，
  Hook 负责塑形合法输入，不是给坏输入当第二个解析器；
- Hook 异常正文不外泄，只保留异常类型名（`sk-ant-` canary 测试）；
- Hook 名字必须唯一——审计行必须能说清是谁拦的。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 445 passed（新增 hook_bus 12 + gateway 5）
development/test config profile: status=ok
CLI golden 文件逐字节未变（demo 不装 Hook，空 bus 是 no-op）
```

范围说明：Hook 目前在代码里装配，没有对应的 Settings 字段——`runtime.*` 里
没有 hook 相关配置，因此 `config/ownership.yaml` 无需改动。把 Hook 变成可配置
（以及 `after_tool` / 会话级 Hook）要等 Bootstrap Container 能注入它们时再说。
本 PR 只实现 `before_tool` 一个阶段。

## PR-011 DeepSeek Provider Contract

状态：**已实现并通过本地测试**。

模型 Provider 由 Anthropic 改为 **DeepSeek**（OpenAI 兼容协议）。

这次改动最值得记的一点是：**Runtime、Gateway、Scheduler、Domain 一行都没改。**
换 Provider 只动了配置契约和（尚未实现的）Adapter，这正是 `ModelPort` 存在的
理由。相应地，它也不是改一行能完事的——`model.provider` 是被冻结成 `Literal`
的，`configuration.md` 早就写明"新增 Provider 时升级配置 schema，而不是接受
一个无法启动的 provider 字符串"，所以这是一次 schema 迁移。

已交付：

- `model.provider: Literal["deepseek", "fake"]`（`anthropic` 已移除——没有
  Adapter 的 provider 字符串不该是可配置的）；
- 新增 `model.base_url`，默认 `https://api.deepseek.com`；沿用与 Qdrant/OTel
  同一套 endpoint 校验（禁 userinfo / query / fragment）；
- `secrets.deepseek_api_key` 取代 `anthropic_api_key`，同步更新
  `SECRET_LIKE_ENV_KEYS`、脱敏键表、mounted-secret 文件名与 CI 环境；
- `config_schema_version` `1.1` → `1.2`；
- `config/ownership.yaml`：新增 `model.base_url`（owner
  `adapters.model.deepseek`，lifecycle `startup`）与 `secrets.deepseek_api_key`。

顺带修掉了一个**潜伏的快照违规**：Task snapshot 的正向 allowlist 原本写的是
`model.*`，而 `configuration.md` §7 明确规定 endpoint 不得进入恢复快照。加上
`model.base_url` 会让它直接违规，所以 allowlist 收窄为
`model.provider` / `model.main.*` / `model.compact.*`，`run_semantics_snapshot()`
显式剔除 `base_url`。**迁移端点不改变一个在跑的 Task 的语义，恢复旧 Task 也不会
连回它当初的端点**——两条都有测试。

新增的安全规则：

- **`model.base_url` 必须是 HTTPS，除非指向 loopback**。每一次请求都带着
  provider API key，所以这条比 Qdrant 那条更严（Qdrant 只在 `remote` 时强制
  HTTPS，模型端点则是无条件的，只给本地兼容服务开口子）。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 452 passed（config 新增 7）
development/test/production config profile: status=ok（config_schema_version=1.2）
CLI golden 文件逐字节未变
```

`pyproject.toml` 与 `uv.lock` 未改动——本 PR 不引入任何依赖。

范围说明：**Adapter 本体不在本 PR 内**。DeepSeek 的流式接口需要一个 HTTP
客户端（`httpx`），也就是需要新依赖与重新生成的 `uv.lock`，而当前工作环境里
没有 `uv`。`tests/architecture/test_dependency_boundaries.py` 已经预先把
`httpx`、`openai`、`deepseek` 加进核心层禁止导入清单，Adapter 落地时不会悄悄
渗进 Runtime。

另注：CI 的依赖许可证 allowlist 目前是
`MIT / BSD-2 / BSD-3 / Apache-2.0 OR BSD-2-Clause / PSF-2.0`，**不含纯
Apache-2.0**。选 HTTP 客户端时需要一并确认许可证，或显式扩展 allowlist。

## PR-012 DeepSeek Model Adapter

状态：**已实现并通过本地测试**。

第一个与进程外世界通信的 Adapter，也是第一个引入外部依赖的 PR。

已交付：

- `adapters/models/deepseek.py`：`DeepSeekModel` 实现 `ModelPort`，走 DeepSeek
  的 OpenAI 兼容 chat completions 流式接口；
- `httpx` 依赖（BSD-3）与重新生成的 `uv.lock`；
- CI 许可证 allowlist 扩展（见下）。

本 PR 固定下来的行为：

- **工具调用攒齐才发**：provider 把一次调用的 JSON 参数拆成多个分片流下来，
  Adapter 按 index 缓冲，直到流声明结束才组装成 `ToolCall`。半截 JSON 绝不能
  进 schema 校验和策略——`ports/model.py` 的契约就是这么写的，现在有实现来兑现；
- **每条流都以 `ModelStreamCompleted` 结尾**，包括失败的那些。让调用方去区分
  "provider 停了"和"adapter 抛了"，就是让它在某处一定弄错；
- **线上的东西一个字都不往回引**：HTTP 错误只带状态码，不读也不引用响应体——
  聊天补全的错误体可能把发出去的 prompt 原样回显，而错误文本会流进事件、日志和
  模型自己的上下文（canary 测试守住）；
- 传输层异常只保留异常类型名（URL 及其 query 可能出现在消息里）；
- **不认识的 finish_reason 报错而不是猜**（例如 `content_filter`）；
- 参数无法解析、缺 id 或缺名字 → 报 provider 错误。猜一个参数等于把模型从没要求
  过的东西送到 handler 面前；
- `stream_options.include_usage` 必开：拿不到 token 账目的 run 没法执行 token
  预算。DeepSeek 的 `prompt_cache_hit_tokens` 也一并映射（其他兼容服务上缺省为 0）。

测试用 `httpx.MockTransport` 喂**真实线格式字节**，CI 依然完全离线。其中一条端到端
测试直接断言 **Runtime 分不出它和脚本化模型的区别**：同样的两轮工具循环，durable
时间线与 `tests/runtime` 里 FakeModel 那条逐项相同。这是 `ModelPort` 这层抽象的
验收标准，不是"我检查过了"。

### 许可证 allowlist 扩展

加 `httpx` 时 CI 的许可证门禁拦下了它的传递依赖 `certifi`（MPL-2.0）。核查后发现
这不是 httpx 的问题——**原 allowlist 与项目自己选定的技术栈冲突**：

| 依赖 | 许可证 | 原 allowlist |
|---|---|---|
| certifi | MPL-2.0 | ❌ |
| asyncpg | Apache-2.0 | ❌ |
| qdrant-client | Apache-2.0 | ❌ |
| opentelemetry-sdk | Apache-2.0 | ❌ |

PR-002 那份清单看起来是照着当时已有的 21 个包写的，是描述而非策略。现在把策略写
明确（见 `docs/compliance.md`）：允许宽松许可证与文件级弱 copyleft，拒绝强
copyleft（GPL/AGPL/LGPL）与未声明许可证。allowlist 同时列 SPDX 与 classifier
两种拼写，因为 `pip-licenses` 只照搬包元数据里写的那种。

2026-07-25 验证证据：

```text
uv lock --check --offline: passed
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 473 passed（deepseek 新增 21）
dependency license allowlist: passed
CLI golden 文件逐字节未变
```

`uv lock --check` 与许可证门禁是前十个 PR 里唯一只能靠 CI 验证的两项，本次起可以
在本地跑（`uv` 按 CI 固定的 0.11.31 装在一次性 venv 里，未污染项目 `.venv`）。

范围说明：Adapter 尚未接进 Bootstrap——`base_url`、API key 与 profile 目前由构造
参数注入，从校验过的 Settings 组装是 `bootstrap/container.py`（WP00-03）的职责。
LangChain model/tool 互操作 Adapter（WP02-07）仍未实现。**model ID 与工具调用支持
情况请对照 DeepSeek 当前文档确认后再投产**；本 PR 的 contract test 钉住的是线格式
处理，不是某个具体模型的能力。

## PR-013 PostgreSQL/Artifact Base

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。

第一次有了真正的持久化：WP03-01～03（session factory + Alembic、PostgreSQL
ConversationStore、本地 ArtifactStore）。

已交付：

- `adapters/persistence/`：`models.py`（SQLAlchemy **Core** 表，不是 ORM——
  仓储的职责就是把行显式映射成领域对象，identity map 和惰性加载只会再引入一
  套隐式的"何时读"）、`engine.py`、`conversation_store.py`；
- `migrations/` + `alembic.ini`：`0001_conversations`；
- `adapters/artifacts/local.py`：`LocalArtifactStore`。

本 PR 固定下来的行为：

- **位置在锁住会话行的前提下分配**：同一会话的两次 append 在锁上串行，
  `sequence` 因此是**无洞的**而不只是唯一的。数据库另有
  `UNIQUE(session_id, sequence)`——锁一旦被绕过，写入会失败而不是悄悄复用位置。
  有测试并发发 5 条断言拿到 `[1,2,3,4,5]`；
- **消息按领域对象存取**（JSONB，含 schema 版本），读回时过同一个模型。
  用本进程不认识的契约写下的行会在边界上 fail closed，而不是半懂不懂地进入
  模型上下文；
- **`statement_timeout` 设在连接级**而不是每条查询：逐条设置的超时迟早会有人忘；
- **本地 Artifact 先写 blob 再写元数据**——中途崩溃留下的是一个没人引用的
  blob（垃圾），而不是一个指向不存在字节的引用（谎言）；
- **元数据放在 blob 旁边而不是建表**：目前除了按 id 点查没有任何查询，
  只服务点查的表是"伪装成索引的 schema"。等上传落地、artifact 行必须与
  document version 同事务写入时，那张表才有存在的理由；
- 跨租户读与不存在**完全同形**——沿用 in-memory 既有的常量文案（不含 id）。

契约测试重构成**一套跑两种实现**：`tests/contracts/` 的 conversation 与
artifact 套件现在参数化在 `memory`/`postgres` 与 `memory`/`local` 上。一个 Port
有两个实现和两套测试，就等于有两个契约。

`tests/persistence/test_migrations.py` 断言迁移与模型元数据描述的是同一个
schema（`compare_metadata`），并跑一遍 downgrade → upgrade。没人跑过的
downgrade 会在需要它的那次事故里才被发现。**这个测试验证过是有牙的**：给模型
加一列会让它失败。

2026-07-25 验证证据：

```text
本机 PostgreSQL 15.14（专用库 agent_workbench_test）
alembic upgrade head: 通过，schema 与模型零漂移
ruff format --check / ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest（有库）: 498 passed
pytest（无库，等同主 CI job）: 484 passed, 14 skipped
```

CI 新增 `postgres` job：`postgres:16`（按 digest 固定）service，先跑迁移再跑
两个数据库相关套件。主 `quality` job 保持完全离线，数据库参数化用例在那里跳过。

两条安全护栏：

- 测试用的 DSN 变量是 `AGENT_WORKBENCH_TEST_DSN`，**故意不在 `AW_` 命名空间内**
  ——settings 会拒绝任何未登记的 `AW_*`，那道守卫比前缀对称更值钱；
- 该 DSN 指向的库名必须以 `_test` 结尾，否则直接拒绝。这些套件会 TRUNCATE，
  导错 DSN 应该得到一个被跳过的套件，而不是一个被清空的数据库。

范围说明：上传数据面（create-upload / 流式或 presigned / complete）、Outbox、
document/version/ACL 仓储与 S3 Adapter 属于 WP03-04～09，不在本 PR。
`artifacts` 表后来随 PR-014 落入实际迁移 `0002_documents_outbox`。三个 DSN
里本 PR 只建了普通查询用的那一个；guard 与 listener 引擎要等协调工作包，
那时它们的连接规则才开始有意义。

## PR-014 Upload/Outbox

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。

WP03-04～06、08、09：上传用例、document/version/ACL 仓储、ingestion outbox
与竞争领取。

已交付：

- `ports/documents.py` / `ports/outbox.py`：`DocumentStore`、`OutboxPort`
  及其 DTO（PR-004 里刻意推迟冻结的两个 Port，现在有实现来校验了）；
- `adapters/persistence/documents.py`、`outbox.py`；
- `application/uploads.py`：`UploadService`（首个应用服务层）；
- 迁移 `0002_documents_outbox`：`artifacts`、`upload_intents`、`documents`、
  `document_versions`、`document_acl`、`outbox_events`。

本 PR 固定下来的行为：

- **version 与它的 outbox 事件同事务提交**。拆成两次提交会同时制造两种排序
  修不好的故障：一个永远不会被索引的文档，和一条指向已回滚内容的索引项。
  测试用 `monkeypatch` 把 outbox 插入换成会触发 CHECK 约束失败的版本——它在
  document 与 version 行已经写入之后才失败——断言两者都没留下；
- **revision 在锁住 document 行的前提下推进**，因此是**单调的**而不只是互不相同；
- **新建文档用条件插入再加锁**：两个上传竞争创建同一个新文档时，都读不到行，
  普通 INSERT 会让后者撞主键。`ON CONFLICT DO NOTHING` 之后重新加锁，拿到的
  要么是自己插的行，要么是抢先者的；
- **完成是双重幂等的**：同一个 upload 再完成一次返回它已经产出的 version；
  内容与当前版本完全相同时不推进 revision——重发的请求不该让索引重做一遍
  产出完全相同行的工作；
- **完成时两边都不信**：读取已存对象自身的 size 与 digest，与传输发生**之前**
  客户端声明的值比对。传错的字节、或声明了自己没有的摘要，都在这里失败，
  而不是变成一个索引会忠实复现的 document version；
- **outbox 没有指向 documents 的外键**：删除事件必须比它描述的那一行活得更久，
  否则索引永远无法被告知遗忘它；
- outbox payload 携带 `authorized_principals`，索引的过滤条件要靠它；
- `SKIP LOCKED` 领取：两个 worker 并发各领 3 条，合计 6 条、零重叠（有测试）。

2026-07-25 验证证据：

```text
本机 PostgreSQL 15.14
alembic upgrade head（0001 → 0002）: 通过，零漂移
pytest（有库）: 525 passed
pytest（无库，等同主 CI job）: 496 passed, 29 skipped
ruff / pyright (strict, src): 全部通过
uv lock --check --offline / 许可证 allowlist: 通过
```

**并发测试抓到了一个真实缺陷**：最初的实现在两个上传同时创建同一个新文档时会
撞主键失败。这不是测试写错了，是实现漏了竞争窗口——已按上面的条件插入模式修复。

反射式 Port 契约测试（PR-004 建立）也拦了一次：四个新聚合必须补进样例表才能
通过 round-trip / 版本 / golden 三重检查。

范围说明：**没有 HTTP**。FastAPI 上传路由与 2 MiB 控制面 request-limit
中间件（WP03-07）留给独立 PR——它引入的是另一个方向的表面（依赖、应用装配、
中间件、413 语义），和本 PR 的"事实与 outbox 原子性"是两件事。

outbox 的 claim **不是 lease**：worker 死了，它领的事件就一直被领着，这里没有
任何东西回收它。lease 时长、heartbeat、fencing 属于协调工作包；在这里做一半会
得到一个看起来可恢复、实际不可恢复的东西，比一个明显的缺口更糟。S3/presigned
传输与 `document_deleted` / `acl_changed` 两类事件的产生路径同样留待后续。

## PR-015 Upload API

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。WP03 的本地存储与
Upload/Outbox 基线完成；S3/presigned 与完整对象授权仍未完成。

WP03-07：FastAPI 上传路由与控制面 request-limit 中间件。

已交付：

- `apps/api/`：`main.py`（应用工厂 + `agent-api` 入口）、`dependencies.py`、
  `middleware.py`、`identity.py`、`state.py`、`routes/`（uploads / artifacts /
  health）；
- `bootstrap/projections.py`：`ApiRuntimeConfig` 等**窄配置对象**；
- `ArtifactStore.put_stream()`：流式写入，两个实现都补齐。

本 PR 固定下来的行为：

- **两个平面共用一个服务器**。控制请求是描述工作的 JSON，被
  `api.max_control_request_body_bytes` 限制住并在超限时返回 **413**；数据面
  （`PUT /v1/uploads/{id}/content`）豁免——把文档传输限制在控制面大小上，
  等于不支持上传；
- **限制不信任 `Content-Length`**：中间件读到比上限多一个字节为止。缓冲这么多
  正是上限本身允许的量，也是唯一能对"声明了长度却不遵守"的请求做出正确判断的
  办法。有一条测试专门用分块 body 且不声明长度来打这个点；
- **本地存储边读边写**：分块落盘、边写边算哈希与字节数，全程不在内存里持有整个
  对象；在检疫文件名下写完才改名发布，所以失败或超限留下的是一个检疫文件，
  而不是一个别人能读到的半成品；
- **跨租户读与不存在完全同形**：状态码、响应体都一样（有测试直接比对两者）；
- **传输前先读 intent**：未知上传或别的租户的上传在写入第一个字节之前就被拒；
- liveness 不碰数据库（碰了就会在与它无关的故障里报告进程已死，让编排器无故重启
  它）；readiness 碰，但有 2 秒上限。

**配置只有一个入口这条规则被架构守卫强制执行了。** `apps/api` 最初直接依赖
`bootstrap.settings`，`test_raw_configuration_sources_are_confined_to_bootstrap`
立刻拦下，报错信息就是设计说明："inject a narrow configuration object instead"。
于是补上 `bootstrap/projections.py`：API 拿到的是 `ApiRuntimeConfig`，看不到
检索漏斗、协调时序或评测指标，DSN 仍以 `SecretStr` 形式传递、只在构造引擎时
解包——一个配置对象的 repr 因此打不出 DSN。

**身份是接口层的结果，不是请求体字段。** 目前只有一个读 header 的开发用解析器，
`deployment_scope == "remote"` 时会在装配阶段拒绝启动。写这一节时默认 host 还是
`0.0.0.0`，名义上的 local scope 仍可能监听所有网卡，所以这道检查当时不足以阻止
意外暴露；监听地址的强制校验见下方 P0-1 一节（2026-07-26）。

2026-07-25 验证证据：

```text
本机 PostgreSQL 15.14
pytest（有库）: 541 passed
pytest（无库，等同主 CI job）: 496 passed, 45 skipped
ruff / pyright (strict, src): 全部通过
uv lock --check --offline / 许可证 allowlist: 通过
CLI golden 文件逐字节未变
```

CI 的 `postgres` job 现在同时跑 `tests/api`。

**修掉一个会挂住生产的 bug。** 413 中间件缓冲请求后，用一个"永远返回空 body"的
`receive` 替换了原来的。Starlette 的流式响应会在同一个通道上等
`http.disconnect`——它永远等不到，于是 artifact 下载整个挂死。测试跑不完暴露了
它；现在缓冲消息放完之后会**回落到原始通道**。

另一处也是测试抓的：数据面豁免最初写成了路由前缀 `/v1/uploads`，把**声明端点
一起豁免了**，413 门禁形同虚设。改成一个精确谓词
`is_data_plane_path()`——只有以 `/content` 结尾的那一条路由算数据面。

范围说明：Chat / Task / SSE / Approval 路由属于 WP13；S3 presigned 传输、
`document_deleted` 与 `acl_changed` 事件路径仍未实现。真正的身份提供方要等
D0 决策检查点（WP04 前）。

## ADR-012 身份边界（D0 决策点）

状态：**已决**。见 [ADR-012](./adr/0012-identity-boundary.md)。

实施计划要求在检索工作包之前定下：v1 是单用户本地演示，还是接入一个已认证的
Principal Adapter。之所以必须先定，是因为 WP04 的检索要按 ACL 过滤候选，而
ACL 过滤的意义完全取决于 principal 从哪里来。

**决定**：v1 在领域层是多租户的，在部署层是单机的。

到 WP03 为止的事实是：**隔离规则已经做完了，缺的是认证**。这两件事经常被混为
一谈，而它们的失败模式完全不同——隔离错了会泄漏数据，认证错了会让任何人成为
任何人。租户隔离在仓储、应用服务与 HTTP 三层都有测试，跨租户读与"不存在"在
状态码、响应体和错误文案上完全同形；缺的只是"请求头里写谁就是谁"这一层。

否决了两个选项：把 v1 降为单用户演示（会让 WP04 的 ACL 过滤无法验证，而那正是
这个项目在 RAG 方向最值得展示的部分），以及现在就接 OIDC（JWKS 缓存与轮换、
时钟偏移、令牌撤销各自都是能微妙出错的地方，且现在没有任何消费者压力，还需要
一份基线 13.1 尚未覆盖的令牌层威胁模型）。

原 ADR 依赖“开发身份只能随 loopback 监听运行”这一安全前提。2026-07-25 核验
发现当前实现并未强制它：默认 `api.host = "0.0.0.0"`，local scope 不检查实际
监听地址。因此 remote 拒绝装配只能算部分护栏，不能证明缺口无法被意外暴露。
ADR 已补记这一实现偏差，并在 2026-07-26 修复（见下方 P0-1 一节）。

新增 `tests/architecture/test_identity_boundary.py`，把 ADR 里的规则变成可执行
的：`PrincipalContext` 只能在一份显式清单里的模块中构造（API 的解析器、CLI 的
demo、以及定义它的领域模块）。新增一处就要改清单，也就必须有人解释为什么。
**验证过是有牙的**：在 `runtime/` 下放一个构造点会让它按模块名报错。同一个文件
还断言 remote 拒绝装配的那两行仍然在位。

能力表相应更新：tenant-scoped 数据访问标为 Implemented/Tested，**生产级身份
认证明确保持 Planned**。同 tenant 不同 principal 的对象级授权仍有已知缺口，
不能笼统宣称完整租户隔离已完成。README 与简历同样不得升级这一项；`scopes`
目前由调用方在请求头里自述，因此不是权限来源，只是让真实解析器接入时不必改动
下游的形状占位。

## P0-2 审批 fail closed

状态：**已实现并通过本地测试**。核验报告 §4 的 P0-2、§6 修复顺序第 2 项。

`ToolGateway.authorize()` 现在在两个 allow 分支之前检查
`decision.requires_approval`。为真时发出 `PermissionRequested`，然后以
`approval_required` 拒绝这次调用；handler 不再被触及。

这里的判断是：`effect="allow", requires_approval=True` 不是许可，是**尚未裁决**。
在此之前 `requires_approval` 是一个纯粹的只写字段——`domain/policies.py` 定义它、
`EnvelopePolicyEngine` 按风险等级设置它，而整个 `runtime/` 包没有任何一处读它。
于是「需要审批」这个信息在从 Policy 流向执行的路上被无声丢弃了，而丢弃的方向
恰好是放行。

之所以是拒绝而不是挂起等待：审批设施（`ApprovalStore`、恢复入口）属于 WP10，
现在不存在。「需要人来决定，但这里没有人可以决定」只能落到拒绝上。等 WP10 到位
后这一分支改为挂起并等待裁决，改动仍然限于 Gateway——`PermissionRequested` 事件
与 `approval_required` 错误码从 PR-003 起就在 Domain 里定义好了，此前一直没有
写入方，这次只是终于把它们接上。

模型侧拿到的是 `status="error"` 且正文含 `approval_required` 的 tool result，
因此能区分「不被允许」和「尚未裁决」；run 本身继续正常收尾，不会因为一次待审批
的调用而失败。

回归测试 6 条：Gateway 级 2 条（含 `allow_modified` 改写分支同样被拦），Runtime
级 4 条（完整 run 副作用为零、持久事件序列、回灌给模型的错误正文，以及一条对照
组）。**验证过是有牙的**：临时撤掉那个分支后前 5 条全部失败。第 6 条对照组在
撤掉后仍然通过——正是它保证前 5 条不是靠「write 工具一律拒绝」这种过度实现凑出
来的，拒绝跟的是审批要求本身，不是风险等级。

范围说明：只改这一件事。P0-1（loopback 强制）、P1-6（改写绕过字节上限）
与 P1-7（Policy/Hook deadline）都在同一个文件附近，但各自是独立的行为变化，
留给各自的 PR。

## P0-1 监听地址强制 loopback

状态：**已实现并通过本地测试**。核验报告 §4 的 P0-1、§6 修复顺序第 1 项。

`api.host` 默认从 `0.0.0.0` 改为 `127.0.0.1`；`ApiSettings.host` 只接受 loopback
地址；`build_dependencies()` 在选定 Header Resolver 之前再校验一次。

**规则是无条件的，没有以 `deployment_scope` 为条件。** scope 是部署给自己贴的
标签，而决定谁能触达 Header Resolver 的是绑定地址——把标签当作绑定地址的代理，
正是这条缺陷成立的原因。remote scope 拒绝装配的检查保持不变，两者互不替代。

为什么两层都校验：`ApiRuntimeConfig` 可以不经 Settings 构造（测试就这么做），
而装配层正是选定 Header Resolver 的那一层，拒绝把它和一个可达地址配在一起的
判断，属于那里。

不是 `localhost` 的主机名一律拒绝，而不是去解析它：解析在校验时和 bind 时可以
给出两个答案，DNS 在两者之间还能改变，「不确定」的安全答案是否。

新增 `tests/api/test_bind_address.py`，16 条，覆盖提交默认值、Settings、装配和
真实 socket 四层。**验证过是有牙的**：撤掉 Settings 校验失败 6 条，撤掉装配层
校验失败 1 条，只把默认值改回 `0.0.0.0` 失败 5 条，三者全撤失败 11 条。

其中 socket 那两条刻意绕开 `Settings`，直接读 `config.default.toml` 的原始值来
`bind()`——只见过校验器放行的值的 socket 测试，抓不到校验器本身写错，而这一条
要在校验器都被删掉时仍然成立。三者全撤那次，它是靠一次**成功的跨接口连接**抓到
缺陷的，不是靠读字符串。

配套的对照组同样必需：同一条连接在 `0.0.0.0` 绑定下必须连得上。没有它，
「连接被拒绝」既可能说明护栏有效，也可能说明探针指向了一个本来就没人监听的端口。
这正是这条缺陷能活下来的机制——`test_the_api_refuses_a_remote_deployment_scope`
断言的是 scope 标签，读起来却像在守护整个身份边界。该测试保留（scope 那一半它
确实守住了），docstring 已写明它不覆盖监听地址。

范围说明：这挡住的是**意外暴露**，不是认证。反向代理、SSH 端口转发或容器端口
映射仍可以把 loopback 进程送上网络——那是部署方的选择，代码拦不住，也不该假装
拦得住。生产身份认证仍是 Planned，README 与能力表不因此升级。Settings 叶子字段
数不变（231），没有新增配置项。

## P1-1 上传与文档的对象级授权

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。核验报告 §4 的 P1-1、
§6 修复顺序第 3 项的前半。

`DocumentStore` 的每个方法都显式接收 `principal_id`；`UploadService` 与三条上传
路由把接口层解析出的 principal 一路传下去。此前整条链路只往下传 tenant，于是同
一个 tenant 里的任何人只要知道 upload id，就能替 owner 传输、替 owner 完成，或者
把自己的 upload 指向别人的 document、覆盖内容并替换 ACL。

**读和写是两条不同的规则**，这是这一条最重要的判断：

| 操作 | 谁可以 |
|---|---|
| 观察 / 传输 / 完成一个 upload | 声明它的那个 principal |
| 向已存在的文档提交新版本 | 文档 owner，**仅此** |
| 读文档、版本列表、授权名单 | owner **或** ACL 授权的 principal |

把 ACL 同时当作写授权，会让「授权某人查看」悄悄变成「授权某人覆盖」——那是没有
任何人打算给出的权限。所以 `_is_granted()` 只服务读路径，写路径只比 owner。

**授权检查与写在同一把锁下。** `_locked_document()` 现在返回整行（revision、
owner、knowledge base），检查放在 `FOR UPDATE` 之后：先检查再取锁，判断的是一份
可能已经不是被写对象的行。条件插入那条竞态分支同理——检查放在
`ON CONFLICT DO NOTHING` **之后**，对最终握住的那一行做，否则「输掉创建竞态」
反而成了获得写权限的路径。

KB 不一致用新的 `KnowledgeBaseMismatchError` 拒绝（409）。它不是授权失败——调用方
确实是 owner——而是一个与已提交事实矛盾的断言：接受它会让 document 行停在 KB-A，
而 outbox 事件告诉索引 KB-B。沿用 `UploadVerificationError` 的既有做法复用
`invalid_tool_input` 错误码，**没有新增领域错误词汇**。

拒绝一律是 404，与「不存在」「别的 tenant 的」完全同形。

回归测试 17 条。新增 `tests/api/test_upload_authorization.py`（10 条）**固定
tenant、只换 principal**，并且**故意把 upload id 和 document id 交给攻击者**——
那正是要防的处境，id 会出现在日志、URL 和工单里，「难猜」不是授权规则。另有
7 条落在持久化层，因为读规则目前没有 HTTP 面。

**验证过是有牙的**：撤掉 upload owner 检查失败 3 条，撤掉 document owner 检查
失败 3 条，撤掉 KB 检查失败 2 条，三者全撤失败 8/10（通过的 2 条正是对照组），
撤掉读授权失败 3 条。并发竞态那条单独验证过：把写授权挪到条件插入**之前**，
它连续 6 次全部失败。

写这些测试时改掉了自己两条虚的断言：一条下载 owner 原来的 artifact 来证明「内容
还在」——但接管会写**新的** artifact 并把文档指过去，原 artifact 两种情况下都还在，
所以那条断言恒真；改成直接查库断言版本数、revision 与 digest。另一条只断言 404
正文里没有文件名，而放行时正文本来也没有文件名，同样恒真；改成状态码和正文一起断言。

**已知残留**：`document_id` 由调用方指定，因此邻居仍可通过「用别人的 id 得到
404、用新 id 得到 201」区分出某个 document id 是否存在。消掉它要改成服务端铸造
document id，那是 API 形状变更而不是授权变更，不在本 PR。P1-2（Artifact 对象
授权）与 P1-3（相同内容重传忽略 ACL 撤销）仍然开着，各自一个 PR。

## P1-3 相同内容重传时的 ACL 调和

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。核验报告 §4 的 P1-3。

digest 相同的那条提前返回分支现在先调和 ACL：授权集合有变化时，原子地替换 ACL 行、
推进 revision、写一条 `acl_changed` 事件，然后仍然返回既有 version。

**重传相同内容正是表达「同一份文档、换一批读者」的方式。** 此前这条路径直接返回
旧 version，ACL 行一次都没碰，也不发事件——「内容不变、撤销某人授权」于是完全
不生效，而且是静默的：索引会继续把文档答给一个 owner 已经取消授权的人，直到有人
碰巧上传了不同的字节。`acl_changed` 从 PR-013 起就定义在 `OutboxEventKind` 和
数据库 CHECK 约束里，一直没有写入方——和 P0-2 的 `PermissionRequested` 同一种形状。

**授权变更要占一个 revision。** 消费者按每文档一个单调计数器排序事件；ACL 事件与
内容事件若能共用 revision，乱序到达就无法与重复到达区分。代价是 version 行在
revision 空间里变稀疏（`[1, 3]`，2 是一次授权变更），这是对的：version 记录内容，
那次 revision 没有改变内容。因此顺带订正了两处 docstring——原来把「不推进 revision」
写成了无条件的幂等性质。

比较集合而非序列，所以重排授权列表不算变更；内容与 ACL 都没变时仍然什么都不做。

回归测试 7 条，全部在持久化层。撤销的效果目前**没有 HTTP 面可观测**（没有 GET
document 路由，artifact 下载的对象授权还是 P1-2），所以没有放形式上的 HTTP 测试。

**验证过是有牙的，而且是双向的**：撤掉调和失败 4 条；只撤掉「集合未变则直接返回」
那道早退失败 3 条，其中一条是原有的幂等测试。第二个方向是必需的——没有它，
「每次重传都推进 revision 并发事件」这种过度实现同样能让前 4 条变绿。

## P1-2 Artifact 对象授权

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-2，§6 修复顺序第 3 项的后半。

`ArtifactStore.put`/`put_stream` 记录 `owner_id`，`get`/`head` 接收 `principal_id`。
artifact 归存储它的 principal 所有，其他人一律 not found——与「不存在」「别的
tenant 的」三者同形。此前同 tenant 的任何人只要知道 artifact id 就能下载，而 id
会出现在 tool result、事件 payload 和 URL 里；UUID 难猜不是授权。

**owner 放在存储自己的元数据里，不放进 `ArtifactRef`。** 这是这条唯一需要设计
判断的地方：`ArtifactRef` 随消息和事件流转，而「谁能读这些字节」是被存对象的属性，
不是指针的属性——指针若携带它，等于把答案连同问题一起发出去。附带好处是领域模型
没变，golden 基线和 `DOMAIN_SCHEMA_VERSION` 都不受影响。

**没有按审计原文走 PostgreSQL。** 审计写的是「在 PostgreSQL 持久化 owner/对象
关系」。`artifacts` 表虽在 schema 里却至今没有写入方，`LocalArtifactStore` 一直用
blob 旁的 sidecar；把授权搬进数据库会让文件存储适配器依赖数据库引擎，而那张表按
其模块 docstring 的说法还没「挣得自己的位置」。记进存储自身的元数据，对未来的 S3
适配器是同一形状（对象元数据），也不引入这层依赖。这是有意偏离修复方向，不是遗漏。

sidecar 改为带 `format` 标记的信封，不认识的信封视为不存在。

回归测试 20 条：共享契约 6 条 × 2 个 store（in-memory 与 filesystem 都跑，规则对
每个后端都被钉住）、sidecar 专属 5 条、HTTP 面 3 条。

**验证过是有牙的**：同时撤掉两个 store 的 owner 检查失败 10 条（跨 store、跨层）；
撤掉 `format` 兜底失败 1 条；撤掉 `isinstance` 兜底失败 1 条——每道防线各有一条
测试专属于它。写的过程中又抓到自己一条恒真断言：原本用集合收集三种拒绝的文案并
断言集合只有一个元素，但被放行的那次什么都不加，集合照样只有一个元素；改成按顺序
记录三次结果，放行记 `"allowed"`。

**已知残留，是明确的功能缺口而不是疏漏**：artifact 归存储它的 principal 所有，
**文档 ACL 触及不到它**——没有东西能把 artifact 反查回引用它的 document version。
于是被授权读文档的 principal 看得到文档存在，却下载不了字节。fail-closed 且不完整，
顺序如此。已用 `test_a_read_grant_does_not_yet_reach_the_bytes` 把这个限制钉住，
反查落地时必须**有意地**改掉它；补齐需要 `document_versions.artifact_id` 上的索引
和一个新的 DocumentStore 方法，属于独立变更。

P1-4（下载整体读入内存，不是真正的分块流式）仍然开着，就在同一条路径上。

## P1-5 Runtime 预算硬上限

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-5，§6 修复顺序第 4 项。
**审计的四个阻断面到此全部关闭。**

三处独立的行为变化，共同的形状是：**上限必须在花费之前生效**。事后记账的不是上限，
副作用已经发生了——和 P0-2 是同一句话，只是发生在调度层而不是授权层。

**一、tool-call 配额在 dispatch 之前预留。** 此前一轮提出 N 个调用就全部执行、之后
才记账；余量只剩 1 而提出 3 个时，3 个 handler 全跑，账上还写 3，比上限本身还大。
现在按余量计算准入数，超出的立即以 `budget_exceeded` 拒绝——仍然发 `ToolProposed`
（模型确实提议了），仍然回答它的 id（每个 id 都欠一个结果）。**只对准入的调用记账**：
因上限被拒的调用不该自己消耗上限。

**二、每回合合并 usage 之后复检预算。** 循环顶部那次检查在回合之前，看不到这一回合
花了什么。于是 `max_total_tokens=1`、模型上报 120 tokens 时 run 报 `completed`；若这
一回合还提议了工具，工具照跑。复检放在错误/取消分支之后、`if turn.calls` 之前，两种
后果同时消掉。

顺带的行为变化：**步数用尽的那一回合，其工具不再执行**。最后一步给了模型，工具结果
已经没有读者，执行它们等于用真实副作用换取会被丢弃的输出。已用测试钉住。

**三、无法度量的成本上限被拒绝。** `cost_micro_usd` 没有任何生产者，成本预算永远停在
0、永远不触发——和 P0-2 的 `requires_approval` 同一种形状，一个只写字段，沉默的方向
恰好是放行。这里**不实现计价器**（按 model revision 的价格表是独立变更），而是拒绝
接受一个执行不了的上限：调用方要的是保证，静默地不提供比说出来更糟。引入计价器的那个
PR 负责删掉这个分支。

**明确的残留**：token 上限仍然只能在模型调用**之后**核对——一次调用花多少 token，
调用前不可知。所以单次调用可以冲过上限、随后 run 立刻失败。上限约束的是 run 走多远，
不是单次 provider 调用返回多少。计价器仍未实现。

回归测试 10 条（4 + 3 + 1 + 2，含 3 条对照）。**验证过是有牙的**：撤掉配额预留失败
3 条、撤掉回合后复检失败 3 条、撤掉成本拒绝失败 1 条、把记账改回按提议数失败 1 条。

值得记一笔：改完之后既有 610 条测试**一条都没红**。审计说的「预算测试没有覆盖越界
批次」正是这个意思——现有两条上限测试的预算都恰好卡在整数倍上（`max_tool_calls=4`、
每轮 2 个调用），从没构造出越界的那一批，所以这一整类缺陷全程绿灯。

## P1-6 / P1-7 Gateway 的尺寸上限与时间界

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-6 与 P1-7，§6 修复顺序第 5 项的
一部分。两条各自一个提交。

**P1-6：Policy 改写受参数字节上限约束。** `authorize()` 的改写分支从 `_validate()`
改为 `_check()`——与原始参数、与 Hook 改写同一道检查。此前 Hook 改写走完整检查而
Policy 改写只重跑 schema，于是 `max_argument_bytes` 约束的是模型能发多少、不是
Policy 能替换成多少：上限 64 字节时一次改写把 10,000 字节送进 handler，run 正常
完成。schema 说 `query` 是字符串，没说一万个字符不行。3 条测试，撤掉失败 2 条。

**P1-7：Policy 与 Hook 受 run deadline 约束并 fail closed。** 三处：

1. Policy 引擎此前完全无界——部署方提供的代码坐在每次调用的必经路径上，卡住就把
   run 拖过它自己的 deadline。现在界是 `min(gateway 自带 policy 超时, run 剩余)`。
   gateway 保留自带默认值（5 秒），这样调用方忘记传剩余时间时**仍然有界**；默认成
   None 才是同一类 fail-open。
2. Policy 抛出的异常归一化为拒绝。**复现时发现比审计写的更严重**：异常直接逃出
   `authorize()`，调用方拿到 traceback 而不是终态结果，违反 `AgentExecutor` 的协议
   约定；而且正文原样带出——复现消息里就有 `dsn=postgres://u:sk-ant-canary@h/db`。
   现在只有异常**类型名**过界，与 Hook Bus 早已遵守的规则一致。
3. Hook 取 `min(自身超时, run 剩余)`，逐个重算。此前每个 hook 都持有完整超时，只剩
   2 秒的 run 仍可能在一个 hook 里花 5 秒——本该结束 run 的 deadline，恰恰是 run
   唯一管不住的东西。剩余为 0 时不启动任何 hook 或 policy 调用，并拒绝该次调用。

拒绝理由里带上**是哪一个界耗尽的**，因为两种后果相同但指向不同的排查方向——和
`ModelCallDeadline.source` 同一个判断。

9 条测试（gateway 6 + hook bus 3，含 2 条对照）。**有牙验证有一点特别**：撤掉
policy 归一化后，能终止的 3 条失败，另外 2 条**永远挂住**（后台跑 8 秒未结束才强
杀）——无界等待正是缺陷本身，它表现为挂起而不是断言失败。hook 那侧同理：撤掉后
1 条失败、1 条挂 30 秒。

## P1-8 重复 tool_call_id 在派发前被拒绝

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-8。

两处行为变化：

1. **模型 turn 完成后、准备/授权/执行之前检查 ID 唯一性。** `tool_call_id` 就是
   「结果回答的是哪一次调用」本身；两次调用共用一个，就无法说哪个结果属于哪一次。
   此前只有 `align_results()` 会发现，而它跑在 handler **之后**——模型重复一个 ID，
   工具就按重复次数各执行一次，run 再死在记账上。复现确认**两次 handler 调用**。
   现在整轮失败、handler 零调用。唯一性是**每轮**的：ID 只需在同侪之间可区分，
   跨轮复用照常执行（有测试钉住）。
2. **`align_results()` 的失败归一化为终态 outcome。** 复现确认了审计的补充：
   `ToolPairingError` **逃出了 `run()`**，调用方拿到 traceback 而不是终态
   `AgentOutcome`——违反 `AgentExecutor` 协议约定，Graph node 因此拿不到可记录、
   可路由的结果。与 P1-7 里 policy 异常逃逸是同一种形状。

5 条测试，含 2 条对照。**关于那条 backstop 需要说明**：预检让重复 ID 永远到不了
`align_results()`，所以第二处修改在正常路径上**不可达**。不可达而又没人跑过的分支
是主张不是保证，所以测试里放了一个故意坏掉的 gateway（返回带错误 `tool_call_id`
的结果）实际走一遍——生产代码里没有任何东西会这么做。

**验证过是有牙的**：撤掉预检失败 2 条，撤掉 backstop 失败 1 条，两处各有专属测试。

## P1-9 DeepSeek SSE frame fail closed

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-9。

三处行为变化：

1. **无法解码的 `data:` frame 结束整条流**（此前静默跳过）。跳过不是中性的：工具
   参数按片段拼接，从中间丢掉一片，剩下的仍可能拼成一份**完全合法的 JSON**——
   一个模型从未发出的调用，带着没人选过的参数。已复现：插入一个损坏 frame 后，
   handler 拿到 `{"document_id": "doc_SAFE"}`，而模型本意更长。注释行、空行、
   `[DONE]` 仍然跳过，它们本来就不携带数据。
2. **领域校验错误归一化为终态 error。** `BoundedText` 限制单个 delta 为 4096 字符，
   超过时构造 `ModelTextDelta` 抛的 `ValidationError` **直接逃出了 `ModelPort`**。
   provider 自己的限制不是本进程的契约，Port 的调用方不该拿到 Pydantic traceback。
3. **累积工具参数有上限**（默认 256 KiB 字符，构造参数可调）。那是这里唯一随
   provider 发送量增长的东西。不新增配置项，与 gateway 的 `policy_timeout_seconds`
   同一种做法。

**有一条测试把缺陷本身写成了预期行为**：
`test_unreadable_frames_are_skipped_rather_than_fatal`，断言的正是要修掉的东西，
一直是绿的。已替换，旧名字与这段历史记在新测试的 docstring 里。这比 P0-1 那种
「守错对象」更直接——不是守护了错误的对象，而是守护了错误本身。

8 条测试（含 2 条对照：注释与空行仍然跳过、上限之内的参数照常拼装）。**验证过是
有牙的**：撤掉 frame fail-closed 失败 3 条，撤掉 `ValidationError` 归一化失败
1 条，撤掉参数上限失败 1 条。

## P2-1 DeepSeek 可靠性配置接入 Adapter

状态：**已实现并通过本地测试**。核验报告 §4 的 P2-1。

`DeepSeekProfile` 增加 `timeout_seconds`、`max_retries`、`tool_calling_required`，
adapter 实际消费它们。此前三者在 Settings 里有定义、有校验，却没有任何消费者——
部署方可以配一个超时或重试次数，两样都不会发生。

**重试语义是唯一需要判断的地方。** 流式调用一旦吐出过事件就不能重试：调用方已经看到
的字节收不回来。所以只有**第一个事件之前**的失败可以重试——传输故障与可重试状态
（5xx、429）。`_attempt()` 用 `_RetryableFailure` 表达，`stream()` 是外面的重试循环。
400 之类不重试：请求本来就错，再发还是错。退避按次翻倍，立刻重试 429 等于要求被更狠
地限流；`sleep` 可注入，测试不必真等。

`tool_calling_required` 只在有 tools 时才发 `tool_choice: "required"`——没有工具却
要求必须选一个，是没人能满足的请求。

Settings → `DeepSeekProfile` 的投影仍不存在，因为 DeepSeek 还没装配进进程
（WP02-06/07）。本条修的是 adapter 侧的配置语义对齐。

9 条测试（含 2 条对照）。**验证过是有牙的**：撤掉 timeout / tool_choice / 可重试状态
信号 / 「已发事件则不重试」守卫，分别失败 1 / 1 / 2 / 1 条。

最后那条一开始**没咬住**：我写的测试用了一个格式损坏的响应体，走的是 P1-9 的坏 frame
路径，到不了那个守卫。换成真正在中途抛 `ReadError` 的响应流之后才成立。
## P2-2 关闭任意可关闭的流

状态：**已实现并通过本地测试**。核验报告 §4 的 P2-2。

`_stream_model` 的 `finally` 从 `isinstance(stream, AsyncGenerator)` 改为一个
`_Closable` Protocol（只要求有 `aclose`）。

`ModelPort` 承诺的是 `AsyncIterator` 而不是 `AsyncGenerator`，所以一个返回其它
可关闭迭代器的 adapter——比如包着一个必须释放的连接的那种——**从来没有被关过**。
这类泄漏表现为压力下连接耗尽，离真正出问题的那行很远。`aclose` 才是协议，
`AsyncGenerator` 只是最常见的满足者。

2 条测试（含 1 条对照：没有 `aclose` 的流不能让 run 出错——关闭是「能则关」，
不是「必须有」）。**验证过是有牙的**：改回只关 `AsyncGenerator` 失败 1 条。
## P1-4 Artifact 分块流式下载

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-4。

`ArtifactStore` 增加 `iter_chunks()`，两个 store 各自实现，下载路由改用它。此前
`Path.read_bytes()` 把最多 100 MiB 读完再包进 `StreamingResponse`——名义流式，
峰值内存是每个并发下载一整个对象。

`iter_chunks()` **不是 `async def`**：授权必须在调用时发生，而不是拉第一块时。
只在首次迭代才拒绝的协程会让路由先承诺 200、再发现无可发送，那对客户端与网络中断
无法区分。

7 条测试（共享契约 5 条跑两个 store，HTTP 面 2 条）。

**HTTP 那条写了三遍才对**，前两遍都是恒真断言，都在有牙验证时被自己抓出来：
内容小于默认分块（一片本就正确）→ 换成大于两个分块；httpx `ASGITransport` 把响应
缓冲成一片（两种实现都报 1）→ 改成直接驱动 ASGI 数 `http.response.body`；数所有
body 消息（Starlette 总补一条**空**终止消息，单次整体 yield 也是 2 片）→ 只数非空片。

**验证过是有牙的**：路由改回「整体读入 + 单片 yield」失败 1 条，撤掉 `iter_chunks()`
授权检查失败 1 条。
## P1-10 Outbox lease 与 fencing

状态：**已实现并通过本地测试（含真实 PostgreSQL 与 Alembic 往返）**。核验报告 §4 的
P1-10，也是审计要求「在真正启用 ingestion worker 之前」完成的一条。

claim 成为**租约**并带 fencing token。Alembic `0003` 给 `outbox_events` 增加
`lease_until` 与 `claim_token` 及按到期时间的部分索引。

**为什么单有到期不够。** 一个只是卡住的 worker（长 GC、网络分区）在租约到期时仍然
活着。它回来后会 ack 一个**另一个 worker 此刻正在处理**的单元，把别人的在途工作标记
为完成，而别人真正做完的结果反而像重复劳动。所以每次 claim 铸一个 token，ack 必须带
当前的那个。

`StaleExecutionError` 从 PR-003 起定义、docstring 明写用途，一直**没有生产者**——
第三个这种形状（前两个是 P0-2 的 `PermissionRequested` 与 P1-3 的 `acl_changed`）。

到期一律读**数据库时钟**：两个 worker 对时间的分歧正是同一租约被握两次的方式。
ack 靠 **rowcount** 判断，因为匹配不到任何行的 UPDATE 是成功的。

迁移会释放迁移前已 claim 未 ack 的行——它们没有租约也没有 token，既不可回收也不可
ack，正卡在这次修复要消除的状态里。释放是安全的：未 ack 的事件本就是欠着的工作，
最坏是被重复应用一次，而 ingestion 侧无论如何必须幂等。

**仍缺 heartbeat**：诚实做慢活的 worker 无法延长租约、会丢掉它。那属于 ingestion
worker 本身；在它存在前，租约应设得比最慢的工作单元更长。已写进模块 docstring。

6 条测试（含 2 条对照）。**验证过是有牙的**：撤掉过期回收失败 3 条，撤掉 fence
失败 2 条。

## PR-016 Dense Retrieval Kernel（WP04 起步）

状态：**已实现并通过本地测试（含真实 Qdrant）**。WP04-04 与 WP04-05 的索引侧；
只开内部 Port，**不注册任何外部 Chat/RAG 路由**——按计划 §9，检索接口要等
PR-017 的 ACL 二次重验落地后才对外可见。

新增 `ports/vector_index.py` 与 `adapters/vector/qdrant.py`：collection schema、
幂等 upsert、带 tenant / knowledge-base / ACL 过滤的 dense 搜索、按文档删除。

三个判断写在代码里：

1. **point id 是 chunk id 的 UUIDv5。** Qdrant 只收无符号整数和 UUID，而
   at-least-once 投递配上生成式 id，等于每次重试都堆一份近似重复，检索会把它们
   一起返回。稳定 id 才让重投是幂等的。
2. **过滤在查询语句里，不在返回之后。** 先取 limit 再在 Python 里筛，会返回比
   请求更少甚至为空的结果——取决于邻域碰巧怎么排——而且把租户边界挪进了调用方。
   边界和计数属于同一条语句。有一条测试专门钉这个：两个不可见 chunk 比可见的更
   靠近查询向量，limit=1 时必须返回那个可见的。
3. **payload 过滤是缩小候选，不是授权。** 模块 docstring 明写：真正的授权在
   PostgreSQL，Qdrant 返回后还要按 document/version 重验（PR-017）。把派生副本
   当权限权威，正是陈旧索引变成数据泄漏的方式。

**没有承诺 revision 顺序保证。** Port 里我一开始写了「旧 revision 不覆盖新的」，
随即改掉了：Qdrant 没有条件写，任何检查都是 read-then-write，会输掉它声称能赢的
竞态。顺序属于 ingestion worker 的单写者锁（WP05-07），这里只记录 revision 供那
套协议比较。

**测试基础设施**：CI 的 postgres job 改名为 `stateful`，加了 Qdrant 服务容器
（按 digest 固定，与其它镜像一致）。Qdrant 镜像不带 shell，容器级 health check
用不了，改在 runner 上轮询 `/readyz`。无服务时跳过 93 项并显式报出，不是静默跳过。

14 条测试，全部对真实 Qdrant 跑。**验证过是有牙的**：撤掉 tenant 过滤失败 1 条，
撤掉 knowledge-base 过滤失败 1 条，撤掉 ACL 过滤失败 2 条，撤掉维度校验失败 1 条。

依赖新增 `qdrant-client`；许可证 allowlist 相应扩了三个既有宽松条款的不同拼写
（numpy 的复合串、protobuf 的 `3-Clause BSD License`），做法与 PR-012 引入 httpx
时一致。
