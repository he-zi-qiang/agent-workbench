# WP14-01 MCP Adapter 实施与验收计划

决策依据：[ADR-025](./adr/0025-mcp-adapter.md)。本计划以代码能够闭环为准，不把“装了
SDK”“注册了 binding”误写成“Agent 已经能安全调用”。

## 0. 交付状态

| 增量 | 主要行为 | 状态 |
|---|---|---|
| MCP-1 | 配置、schema 1.8、显式 remote tool allowlist | 已完成 |
| MCP-2 | 本地命名与第三方 schema gate | 已完成 |
| MCP-3 | 官方 SDK v2 client、分页发现、静态 binding | 已完成 |
| MCP-4 | API/Worker 一致授权目录、writer 动态工具源 | 已完成 |
| MCP-5 | 安全重放边界、结果与 artifact 映射 | 已完成 |
| MCP-6 | Worker 异步生命周期与失败回滚 | 已完成 |
| MCP-7 | 协议 E2E、启动 benchmark、文档与全量门禁 | 已完成 |

合格标准不是“远端函数返回了字符串”，而是下面整条链都能被测试观察：

```text
配置 allowlist
→ API 提交时冻结具体 ToolName
→ Worker 启动发现并取交集
→ writer profile 再与 Task envelope 取交集
→ 模型提出工具
→ Gateway 校验/授权
→ MCP tools/call
→ ToolResult / ArtifactRef
→ ToolCompleted 事件
```

## 1. 已锁死的实现边界

### 1.1 依赖方向

- `domain/`、`ports/`、`runtime/`、`workflows/` 不得 import `mcp` 或 `mcp_types`；
- SDK 类型只存在于 `adapters/mcp/client.py`；
- `registry_source.py` 只接收 `bootstrap.projections.MCPServerConfig` 已投影出的字段值，
  不 import raw `Settings` 或配置对象；
- MCP 必须形成普通 `ToolBinding`，不得直接调用模型、不得自带 Agent executor；
- API 进程只从配置纯函数解析工具名，不建立 MCP session、不执行 MCP 工具。

### 1.2 运行范围

- 只进入 Task Worker；Chat 无 MCP；
- 只给 `writer/synthesize` 动态暴露；六个 v1 profile 的静态 `tool_names` 仍为空；
- `retryable_effects=false` 的 server 不连接、不注册、不写进新 Task 信封；
- 默认配置 `optional_labs.mcp_adapter=false` 且 `[mcp].servers=[]`。

### 1.3 权限范围

- 工具固定 `external/safe/exclusive`，scope 为 `mcp:<alias>`；只有明确声明整个节点重放时
  可再次调用的 server 才进入这条路径；
- 模型所见工具 = writer 动态源 ∩ Worker 注册表 ∩ Task 持久信封；
- principal 必须有 `mcp:<alias>`；
- 当前图审批只管最终导出，不声称已实现 Tool 级动态审批；
- MCP v1 不使用副作用账本；当前账本无法回放完整 ToolResult，不能拿“已成功”状态阻止
  节点重放后重建模型上下文。

## 2. 分增量实施

### MCP-1：配置与跨进程一致目录

文件：

- `src/agent_workbench/bootstrap/settings.py`
- `src/agent_workbench/bootstrap/projections.py`
- `config/config.default.toml`
- `config/ownership.yaml`
- `docs/configuration.md`

实现：

1. `MCPServerSettings` 包含 `alias/transport/endpoint/tools/retryable_effects/
   timeout_seconds`；`tools` 是必填、非空的显式 allowlist；
2. alias 唯一，remote name 经归一化后也必须唯一；endpoint 拒绝凭据、query、fragment，
   且非 loopback 必须使用 HTTPS；
3. 有 server 但 lab 开关关闭时启动失败；开关打开但 server 为空合法；
4. schema 版本升到 1.8，所有叶子字段登记唯一 owner 与 lifecycle；
5. `configured_mcp_tool_names()` 只解析 `retryable_effects=true` 的配置；
6. `project_task()` 把具体名字写入默认 Task authorization envelope；
7. `project_task_worker()` 投影 server 与三个结果大小上限，Adapter 不读取 raw settings。

有牙测试：

- 缺失/空 allowlist、非法 endpoint、重复 alias、归一化碰撞都失败；各自配合法对照；
- 两个配置工具得到两个稳定、具体的本地名；
- `retryable_effects=false` 不进入信封；
- `mcp_tools=()` 时旧信封的序列化逐字节不变。

### MCP-2：命名与 schema 闸门

文件：

- `src/agent_workbench/adapters/mcp/naming.py`
- `src/agent_workbench/adapters/mcp/schema_gate.py`

实现：

1. `tool_name_for(alias, remote)` 产生 `mcp_<alias>_<remote>`；大小写、`-`、`.`、空格
   采用唯一固定映射，其余非法字符与超长结果返回 `SkipReason`；
2. `admit()` 先要求 object schema，再复用 `assert_schema_supported`；
3. 只在 MCP Adapter 捕获 `UnsupportedToolSchema`，公共校验器不放宽。

有牙测试：合法/非法成对；两个 server 的同名工具不冲突；同一输入确定性；MCP 的
`oneOf` 被跳过，同时自研工具的 `oneOf` 仍使 Gateway 装配失败。

### MCP-3：SDK v2、分页发现与静态注册

文件：

- `src/agent_workbench/adapters/mcp/client.py`
- `src/agent_workbench/adapters/mcp/registry_source.py`
- `src/agent_workbench/adapters/mcp/result_mapping.py`

实现：

1. 使用 `mcp>=2.0,<3` 的 `Client`；生产 URL 选择 Streamable HTTP；`cache=None`；
2. SDK 对象在 client 模块翻译成项目自有 `RemoteToolPage/RemoteCallResult`；
3. 一次启动发现遍历分页，最多 100 页/1000 工具，防重复 cursor；
4. 发现只选择 allowlist 内工具，再过名字、重复项、schema、description 校验；
5. 每个合格工具形成普通 `ToolBinding`；同 server 共享一个进程级 `asyncio.Lock`；
6. 连不上、超时或整个目录不可靠时记录 `mcp_discovery_failed` 并返回空快照；单工具
   不合规则记录 `mcp_tool_skipped`，其他工具继续。
7. SDK 回退到仍携带旧版 `execution.taskSupport` 的 server 时，`required` 工具跳过；本版
   没有 MCP Tasks 句柄与恢复协议，`optional` 只走同步调用。2026-07-28 wire 已移除该字段。

有牙测试：

- 官方 SDK 内存 server 完成 v2 `server/discover` / `tools/list` / `tools/call`；另有 legacy
  initialize 回退测试；
- 多页目录能完整收集；重复 cursor、页数/工具数超界拒绝整个快照；
- allowlist 外工具不会注册；配置缺失项会告警；
- 一个非法名字、一个非法 schema、一个合法工具时恰好留下一个且两条 skip 原因可定位；
- `taskSupport=required` 被跳过而 `optional` 对照仍可注册；
- 同 server 两个并发 Task 调用不会重叠；不同 server 不共享锁。

### MCP-4：Profile 与授权信封闭环

文件：

- `src/agent_workbench/workflows/agent_profiles.py`
- `src/agent_workbench/workflows/agent_nodes.py`
- `src/agent_workbench/workflows/task_handlers.py`
- `src/agent_workbench/bootstrap/projections.py`

实现：

1. `AgentProfile` 增加声明式 `dynamic_tool_sources`；只给 writer 配 `mcp`；
2. Worker 把实际注册的 MCP 名字传给 synthesize handler；
3. `build_agent_request()` 继续通过既有 `permitted_tools` 与 Task envelope 取交集；
4. 历史信封有名字但当前 registry 没有时，不 advertise 已消失工具；模型即使伪造调用，
   Gateway 仍按 unknown-tool 失败关闭。
5. `tool_calling_required` 只强制开场轮；已有 ToolResult 的下一轮保留 tools 但回到 auto，
   让真实 Provider 可以完成报告而不是被迫重复调用到预算耗尽。

有牙测试：

- writer 可见当前 Worker 与信封共同拥有的 MCP 名；
- 信封缺名时 writer 不可见；
- framer/planner/researcher/critic 均不可见；
- 静态 profile 名单仍为空，防止 MCP 配置关闭时行为漂移。
- DeepSeek 两轮真实 Adapter 契约断言首轮 required、结果轮 tools 仍在而 required 已移除。

### MCP-5：安全重放边界与结果映射

文件：

- `src/agent_workbench/adapters/mcp/registry_source.py`
- `src/agent_workbench/adapters/mcp/result_mapping.py`

实现：

1. `retryable_effects=true` 明确定义为：server 的全部 allowlisted tool 在整个节点重放时
   都允许再次调用，即使模型重放时参数略有变化；只读、天然幂等或远端稳定业务键去重才
   应开启；
2. binding 使用 `idempotency=safe` 且不带 operation key；当前 ledger 没有完整 ToolResult
   replay，不能在节点 checkpoint 前崩溃后阻止重调；
3. 结果映射保留文本，二进制/大文本落盘；多个 artifact block 组成确定性 ZIP；resource
   link 不抓取；structured content 只作无 content 时 fallback；
4. SDK materialize 后的总结果、inline 文本和 artifact 分别执行语义大小上限；非法 MIME
   回退；owner/tenant 只取 principal；远端错误文本不透传；该上限不冒充 transport body
   或进程内存硬上限。

有牙测试：

- binding 明确为 safe 且没有 operation key；同一节点重放可以再次 dispatch 并重建结果；
- 事件为 `ToolProposed → PermissionResolved → ToolStarted → ToolCompleted`；
- 单一合法 OOXML MIME 原样保留，非法 MIME 回退；
- 多块结果每块都在 manifest/ZIP；同输入 ZIP 字节相同；
- tenant/owner 不能由远端内容覆盖；总响应和 artifact 两层超界都安全失败。

已知边界：节点重放前已落盘但未 checkpoint 的 artifact 可能无人引用；仓库当前没有 GC，
不能声称已清理。完整 ToolResult ledger replay / 远端幂等键属于后续工作包。

### MCP-6：Worker 异步启动与资源回滚

文件：

- `src/agent_workbench/apps/task_worker/composition.py`
- `src/agent_workbench/apps/task_worker/main.py`

实现：

1. `build_task_worker_dependencies` 改成 async，由 `serve()` 直接 await；禁止在活动 loop
   内 `asyncio.run`；
2. `AsyncExitStack` 持有 engine、guard、HTTP/Qdrant 与所有 MCP Client；
3. 某个 server 连接/发现失败只跳过该 server，先前成功连接继续存活；若 MCP 发现之后的
   Gateway/Workflow 等本地装配失败，已经打开的全部资源由栈回滚；
4. server 连接/发现失败是单 server fail-soft；本地配置、注册表重名等本仓 bug 仍 fail-fast；
5. 连接成功但发现结果为空时立即关闭候选 client；只有贡献 binding 的连接转交 Worker
   总资源栈；
6. `dispose()` 统一关栈，不重复关闭同一 client。

有牙测试：正常启动/关闭、一个 server 失败但另一个继续、协商超时、空目录立即关闭、连接后
组合失败、开关关闭零连接等路径都要断言进入与退出次数，而不是只断言“没抛异常”。

### MCP-7：E2E、benchmark 与证据

1. `tests/e2e/test_mcp_task_e2e.py` 用官方 SDK 内存 server、真实 Runtime、真实
   ToolGateway、Task 型 request 与授权信封跑一次**协议—Runtime 集成回合**；断言事件名
   是实际存在的 `ToolCompleted`，并验证重整后的 tool name、scope 与 artifact 所有权；
   它不冒充 PostgreSQL Task Registry/claim/checkpoint E2E；
2. 至少保留一个 Streamable HTTP loopback smoke，证明生产 transport，不只证明项目自有
   fake protocol；若 CI 环境不允许绑定端口，可标记为显式 integration gate，但内存协议
   E2E 不得 skip；
3. `scripts/benchmark_mcp_startup.py` 测量固定工具数下“SDK `server/discover` + 目录发现 + binding
   构造”的中位数/p95，输出 JSON；`docs/status.md` 记录 commit、Python/SDK、工具数、轮次
   与结果，原始 JSON 入库，不能只写一句“开销很小”；
4. 更新依赖许可证 allowlist：`cffi` 是 `MIT-0`，`cryptography` 是
   `Apache-2.0 OR BSD-3-Clause`，精确加入，不允许 `UNKNOWN`；
5. 更新 ADR、配置文档、状态与 WP14 勾选项。

## 3. 验收命令

聚焦回归先跑：

```bash
.venv/bin/python -m pytest \
  tests/adapters/test_mcp_*.py \
  tests/config/test_mcp_settings.py \
  tests/workflows/test_agent_profiles.py \
  tests/workflows/test_agent_nodes.py \
  tests/runtime/test_agent_runtime.py \
  tests/apps/test_task_worker_entrypoint.py \
  tests/persistence/test_task_worker_composition.py -q
```

静态与架构门禁：

```bash
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m pyright
.venv/bin/python -m pytest tests/architecture tests/config -q
```

许可证：

```bash
# 执行 `.github/workflows/ci.yml` 的 "Enforce dependency license policy"
# 同一条 pip-licenses 命令；allow-only 是仓库级政策，禁止在工作包文档里
# 另维护一份会漂移的缩减副本。
```

无服务全量：

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/e2e
```

真实 PostgreSQL/Qdrant 与 E2E：

```bash
AGENT_WORKBENCH_TEST_DSN='postgresql+asyncpg://agent:ci-only@127.0.0.1:5433/agent_workbench_test' \
AGENT_WORKBENCH_TEST_QDRANT_URL='http://127.0.0.1:6333' \
.venv/bin/python -m pytest tests/ -q
```

最后运行 benchmark 并把原始 JSON 复制到证据包，而不是手写数字：

```bash
.venv/bin/python scripts/benchmark_mcp_startup.py --tools 20 --rounds 30
```

## 4. 明确不在本 WP 内

- MCP stdio、OAuth、热更新、prompts/resources 浏览、sampling、roots、elicitation；
- Tool 级动态人工审批与跨进程 MCP server 全局锁；
- Chat Mode MCP；
- 让最终 Task 报告自动变成 `.docx`（MCP 产出的中间 `.docx` 可落盘，最终
  `export_artifact` 仍输出 `report.md`）；
- 为兼容第三方而绕过/放宽公共 schema 校验器；
- 把 MCP SDK 模型存入事件、checkpoint、Task Registry 或业务 domain。
