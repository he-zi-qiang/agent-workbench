# ADR-025：MCP 工具以显式目录进入 Task Worker，并冻结成本地 ToolBinding

- 决策点：第三方 MCP 工具如何进入自研 Agent Runtime，而不绕过 Tool Gateway、Task
  授权信封、重放边界与 artifact 所有权
- 状态：**接受（2026-08-09 实现修订）**
- 日期：2026-08-09
- 影响：新增 `adapters/mcp/`；配置 schema `1.6 → 1.7 → 1.8`；Task Worker
  组合根改为异步启动；`writer/synthesize` 获得受信封收窄的动态 MCP 工具源；
  `SUPPORTED_KEYWORDS` **不变**

## 1. 背景与不变量

MCP 解决“怎样发现和调用外部工具”，不替本项目决定“模型是否有权调用、调用是否可恢复、
结果属于谁”。因此 MCP SDK 只能停在 Adapter 层，协议对象必须先翻译为本项目自己的
`ToolSpec`、`ToolBinding` 和 `ToolResult`，再进入唯一的 `ToolGateway`。

这条接缝有六个现实冲突：

| 本项目契约 | MCP 远端现实 |
|---|---|
| `ToolName` 只接受 `^[a-z][a-z0-9_]{0,63}$` | 名字可为任意字符串，不同 server 可重名 |
| 输入 schema 只接受真正实现了语义的关键字子集 | 常见 `oneOf`、`$ref`、`format` 等 |
| 风险、并发、permission scope 必须显式 | MCP 不提供本项目需要的风险语义 |
| `StaticToolRegistry` 在进程生命周期内不变 | `tools/list` 是可变化的远程目录 |
| Task 授权信封在 API 提交时已持久化 | 只有 Worker 应建立 MCP 调用连接 |
| 图节点只在边界 checkpoint | 节点内已完成的远端结果可能在崩溃后需要重建 |

以下不变量高于“多接几个工具”：

1. API 不执行 MCP 工具，MCP SDK 不进入 domain/runtime/workflow 核心；
2. 历史 Task 的权限不因部署升级或远端目录变化而变宽；
3. 未通过本地 schema 校验的工具不能进入 Gateway；
4. 任何外部调用都必须经过同一个提议、授权、执行和事件协议；
5. tenant 与 owner 只能来自服务端运行上下文，不能信任模型或远端返回值；
6. 开关关闭或没有配置时，既有 Task 行为与授权信封逐字节不变。

## 2. 决策

### 2.1 使用官方 Python SDK v2，只在 Adapter 层支持 Streamable HTTP

依赖为 `mcp>=2.0,<3`，不安装 CLI extra。`adapters/mcp/client.py` 是唯一看见 SDK
类型的模块，并立即把 SDK 对象翻译成项目自有、冻结的数据类。架构守卫禁止 core 导入
`mcp` 或 `mcp_types`。

生产配置只接受 HTTP URL。`stdio` 会派生本地进程，并引入文件系统、环境变量与子进程
生命周期的新威胁模型，不能伪装成一个 transport 字符串顺手开放。OAuth 也不在本决策内。

Client 禁用 SDK 目录缓存；每个 Worker 启动时完成当前 v2 `server/discover` 协商（旧 server
回退 legacy initialize）与一次**有界目录快照**。
“一次发现”包括遍历 `tools/list` 的所有分页，不是只发一个 RPC：最多 100 页、1000 个
工具；重复 cursor、超界、超时或协议错误会拒绝该 server 的整个快照。

本项目尚未实现 MCP Tasks。协商到仍携带旧版 `Tool.execution` 元数据的 server 时，远端
工具若声明 `taskSupport="required"`，发现阶段就跳过；`optional` 或未声明的工具按同步
`tools/call` 处理。当前 2026-07-28 wire 已不含这个旧字段，但保留 guard 可以防止 SDK
回退到 2025-11-25 server 时把长任务伪装成普通同步调用。

### 2.2 API 与 Worker 通过显式 allowlist 共享目录事实

只让 Worker 做 `tools/list`，同时要求 API 在提交时持久化具体 `allowed_tools`，若没有一份
共同事实就无法闭环。因此配置 schema 1.8 为每个 server 增加必填的 `tools` allowlist：

```toml
[[mcp.servers]]
alias = "office"
endpoint = "https://mcp.internal.example/mcp"
tools = ["render_document", "lookup_template"]
retryable_effects = true
timeout_seconds = 30
```

- API **不连接 MCP**，只用纯函数把配置名解析成具体本地名，并随 Task 信封存盘；
- Worker 启动时取“配置 allowlist ∩ 本次远端目录 ∩ 本地 schema 可接受集合”；
- 远端额外广告的工具不会被注册；配置里存在而远端缺失的工具会留下结构化日志；
- Task 提交后删掉工具，历史信封不会改写，但当前 Worker 注册表没有该名字，Gateway 按
  既有 unknown-tool 协议返回失败结果，不抛出破坏 Agent 对话的异常；
- Task 提交后新增工具，只影响之后提交的 Task，不追溯扩大历史信封。

这里冻结的是**工具名授权集合**，不是远端能力内容。相同 remote name 的 description、schema
或实际行为可能在 Worker 重启后的新目录快照里变化；本版用本地 schema gate 防止不支持的
形状进入 Gateway，但没有把 schema digest 写入 Task。若场景要求历史 Task 绑定精确工具
版本，需要版本化 capability catalog / schema digest，不能把当前名字级冻结描述成已实现。

### 2.3 名字稳定地映射为 `mcp_<alias>_<remote>`

`alias` 是部署者选择的本地名字，不采信远端 server name。映射规则固定为：大写转小写，
`-`、`.`、空格转 `_`；其余非法字符、空名字、超长名字拒绝。配置加载时还会拒绝同一
server 内归一化碰撞，例如 `foo-bar` 与 `foo.bar`。

运行时仍防御远端重复项和本地碰撞。某一个名字失败只跳过该工具，不拖垮同 server 的其他
工具；所有跳过都记录 alias、remote name 与可操作原因。

### 2.4 schema 失败关闭，但第三方坏工具不拖垮 Worker

MCP schema 先过 `adapters/mcp/schema_gate.py`：顶层必须是 object，并复用
`runtime.schema_validation.assert_schema_supported`。适配层只捕获
`UnsupportedToolSchema`，把它变成该工具的 skip reason。

`SUPPORTED_KEYWORDS` 不因 MCP 放宽：

- 仓库自研工具 schema 不合规仍使 Gateway 装配失败，这是代码 bug；
- 第三方 MCP 工具 schema 不合规则只跳过该工具，这是外部兼容性事实。

若要支持 `oneOf` 或 `$ref`，必须在公共校验器里真正实现其语义并单独测试，不能给 MCP
开“相信远端”的旁路。

### 2.5 MCP 是 Task-only 动态能力，只暴露给 `writer/synthesize`

v1 六个静态 `AgentProfile.tool_names` 继续保持空元组；外部研究与最终导出仍走固定图节点。
本 ADR 只为 `writer` profile 增加 `dynamic_tool_sources={"mcp"}`，组合根把本次启动实际
发现的 MCP 名字交给 synthesize node。

模型最终看到的集合是三重交集：

```text
writer 允许动态 MCP
∩ 当前 Worker 真正注册的名字
∩ Task 提交时冻结的 authorization envelope
```

framer、planner、两个 researcher 和 critic 看不到 MCP；Chat 也不装配 MCP。这样既保留
固定 LangGraph 是唯一跨 Agent 编排者，也让“生成文档/转换格式/查询外部系统”在写作阶段
通过统一 Agent loop 展示出来。

主模型的 `tool_calling_required=true` 只作用于开场 provider turn。Runtime 把 MCP
ToolResult 送回模型后仍广告同一目录，但 Adapter 将 `tool_choice` 恢复为 auto；模型可以
继续调用，也可以产出最终报告。若每轮都发 required，MCP 会被迫耗尽工具预算而无法收敛。

这里明确替代旧 profile 的一条说明：旧说明把“所有 v1 agent 永远没有工具”当成保护专用
graph port 的办法；现在更精确的规则是“静态 profile 没有工具，只有 ADR 明示的动态工具源
可以在提交信封与实时注册表双重收窄后加入”。

### 2.6 权限、审批和风险不由 MCP 推断

所有 MCP binding 固定为：

- `risk="external"`；
- `idempotency="safe"`；
- `concurrency="exclusive"`；
- `permission_scopes=("mcp:<alias>",)`；
- `timeout_seconds` 取 server 配置。

Task principal 必须持有相应 `mcp:<alias>` scope，且工具名必须在持久信封里。现有 Task
信封对 MCP 采用 `max_tool_risk="external"`，不把 external 放进
`approval_required_risks`：也就是说本版授权边界是**提交时明确许可 + principal scope +
Gateway 实时复核**，图中的人工审批仍只管最终报告导出。Tool 级动态审批属于另一个 Optional
Lab，不能在文档里暗示已经实现。

`ToolSpec.exclusive` 只约束一次 Agent run 内的批次。实现额外为同一 server 的所有 binding
共享进程内 `asyncio.Lock`，因此一个 Worker 进程里的多个 Task lane 也不会同时驱动同一
session。它**不提供跨 Worker 进程的全局串行**；多进程部署必须由远端 server 支持多 client，
或另行引入分布式 server 锁。

### 2.7 只有显式声明可安全重放的 server 才进入 Task 图

`retryable_effects` 没有默认值。声明为 `false` 的 server 不建立 Task binding，也不写进
新 Task 的授权信封，因为未知结果后自动重试可能重复远端副作用。

`true` 的含义刻意比“HTTP 可以重试”更强：该 server **全部 allowlisted tool** 都允许在整个
`synthesize` 节点重放时再次调用，即使模型在重放中生成的参数与上次略有不同。部署方只应
在工具是只读、天然幂等，或远端自己用稳定业务键去重时作此声明；本项目无法从 MCP schema
推断这一事实。

因此这批 binding 使用 `idempotency="safe"`，不携带 `operation_key`，也不进入当前外部
副作用账本。原因不是降低可靠性要求，而是当前账本只持久化操作状态与可选 artifact id，
**没有可回放的完整 `ToolResult`**。若远端调用成功后 Worker 在节点 checkpoint 前退出，
拿“已成功”状态阻止再次调用会让 inline 结果永久丢失；安全重放能重新构造模型上下文。

代价也明确：重放可能再次调用远端，且若上次已写 artifact、尚未 checkpoint，新一轮可能
产生一个无人引用的 artifact。本项目当前没有 artifact GC，不能把它写成已解决；这是本版
已知遗留。真正的 exactly-once MCP 需要远端幂等键，或让账本持久化并回放完整 ToolResult，
另开工作包实现。

### 2.8 结果必须有界、可归属且不静默丢 block

MCP 可返回文本、structured content、image/audio、embedded resource 与 resource link，
而本项目 `ToolResult` 只有一个 `ArtifactRef`。第一版映射如下：

- 普通文本在阈值内进入 `content`；大文本写入 artifact；
- image、audio、blob 与大 embedded text 写入 artifact；
- resource link 只渲染名称与 URI，**不自动抓取**第二个远端资源；
- structured content 只在没有普通 content 时作为确定性 JSON fallback，避免重复塞上下文；
- 只有一个 artifact-shaped block 时保留合法 media type；非法 MIME 回退
  `application/octet-stream`；
- 多个 artifact-shaped block 组成含 `manifest.json` 的确定性 ZIP，以适配单 ArtifactRef
  契约，不丢弃任何 block；
- SDK 已 materialize 的总结果受 `policy.max_tool_result_bytes` 限制，归一化产物受
  `artifact_store.max_artifact_bytes` 限制；超界返回安全的 `output_too_large`；这些是
  **语义上限而非 HTTP body/进程内存硬上限**，SDK 在适配器统计前已经解析响应；
- artifact 的 tenant/owner 只取 `invocation.context.principal`，远端不能指定路径或所有者；
- 远端 `is_error` 与异常只返回通用错误，不把可能含密钥的远端异常文本写入事件。

## 3. 启动与资源生命周期

Task Worker 组合根必须是 async：SDK 协商、分页发现与连接上下文都不能从运行中的事件
循环里用 `asyncio.run` 偷渡。

所有成功打开的 MCP client、HTTP client、Qdrant client、guard 与 engine 进入一个
`AsyncExitStack`。任何后续 server 或 Gateway 装配失败都会回滚已经打开的资源；正常退出时
先停止 Worker，再按栈反序关闭连接。server 不可达或发现失败是该 server 的 fail-soft，
Worker 仍以其余可用工具启动。连接成功但目录没有任何合格 binding 时，候选 client 立即
关闭；只有实际贡献工具的连接才转交 Worker 总资源栈并存活到 `dispose()`。

## 4. 后果与边界

- Optional Lab 默认仍关闭；未配置部署的信封与行为不变；
- MCP 是“协议适配器”，不是第二套 Agent executor；LangGraph 仍编排 Task，Runtime 仍拥有
  model/tool loop；
- 远端 schema 兼容率会低于直接信任 schema 的框架，但每个被接受的调用都经过真实校验；
- endpoint 与 allowlist 是部署信任决定，不是内容安全证明：远端 description/result 仍是
  不受信任的模型输入，可能包含 prompt injection。Gateway 限制可调用能力与作用域，最终
  导出仍经过既有 HITL，但本版不声称能判断远端文本的语义真实性；
- `.docx` 等中间产物可以进入 artifact store；最终 `report.md` 是否改成 Word 仍需独立 ADR；
- 不支持 stdio、OAuth、热更新、MCP Tasks、prompts/resources 主动浏览、sampling、roots、
  elicitation、Tool 级人工审批、transport body 硬上限或跨进程全局串行；
- `mcp` v2 的加密依赖链引入 `cffi` 的 `MIT-0` 与 `cryptography` 的
  `Apache-2.0 OR BSD-3-Clause` 元数据拼写；CI allowlist 精确登记这两种既有宽松许可组合，
  不使用 `UNKNOWN` 绕过。

## 5. 被否决的方案

**Worker 发现后再回写 API 信封。** Task 在 API 提交时已持久化，跨进程回写会把一次提交
变成竞态，并破坏“提交时权限快照”。显式 allowlist 更简单且可审计。

**把远端广告的所有工具自动加入信封。** 远端增加工具就会扩大新 Task 权限，API 又无法
验证它看到的是同一目录；不接受。

**把 MCP 工具放进所有 Agent profile。** 会让研究、评审和写作边界失效，也会绕开固定图
节点的职责；只给 writer 动态源。

**把 MCP 塞进现有副作用账本，成功后只返回“已经执行”。** 当前账本不保存完整
`ToolResult`，因此不能重建节点内模型上下文；它还会把等待进程锁超时（RPC 尚未发出）误当
成未知远端结果。等账本拥有可回放结果，或远端协议提供稳定幂等键后再做 exactly-once；
本版只接纳部署者明确声明可安全重放的工具。

**放宽 schema 或信任远端。** 会让 Gateway 的“已校验”变成假承诺；不接受。

**运行时热更新 StaticToolRegistry。** 已写事件无法再回答当时模型看见了哪些工具；等后续
有版本化目录、OAuth 与历史可解释性方案时再开新 ADR。
