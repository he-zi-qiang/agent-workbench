# ADR-026：Word 文档是 MCP 返回的不可变 Artifact，不是可写的本机路径

- 决策点：如何把 `.docx` 生成能力接入现有 MCP Adapter，同时保留 Tool Gateway、权限信封、
  审计事件和 ArtifactStore 的所有权边界
- 状态：**接受（本地 Optional Lab）**
- 日期：2026-08-09
- 影响：新增项目自有 Word MCP Server、本地 profile 与运行走查；不改变 Agent Runtime、
  LangGraph、Task Registry 或 Artifact API 的事实源

## 1. 背景

Codex 桌面环境安装了 Documents skill，并不等于 Agent Workbench 的 Task Worker 获得了
同一能力。Skill 是开发工具的能力包；Work 模式只认识启动时由 MCP 发现、再冻结为本项目
`ToolBinding` 的工具。把 skill 路径、Microsoft Word GUI 自动化或用户文件路径直接塞进
Worker，都会绕过 ADR-025 已经建立的协议和权限边界。

第一步只需要展示一条小而完整的能力链：writer 根据结构化内容生成 `.docx`，结果成为当前
Task principal 拥有的 Artifact。读取现有文档与编辑文档以后再增量加入，不能为了“看起来
功能齐全”同时开放任意文件读写。

## 2. 决策

### 2.1 项目自有、loopback-only 的 Streamable HTTP Server

本地进程固定监听 `127.0.0.1:8765`，协议端点为 `/mcp`，健康端点为 `/health`。它使用
官方 MCP Python SDK 暴露唯一 remote tool：`render_document`。独立的、显式 opt-in
`config.word-local.toml` profile 配置：

```toml
[optional_labs]
mcp_adapter = true

[[mcp.servers]]
alias = "word"
transport = "http"
endpoint = "http://127.0.0.1:8765/mcp"
tools = ["render_document"]
retryable_effects = true
timeout_seconds = 60

[model.main]
tool_calling_required = false
```

由 ADR-025 的稳定命名规则，它在 Worker 内成为
`mcp_word_render_document`，permission scope 为 `mcp:word`。只有 writer/synthesize
可以看到它；API 不连接 Word MCP，只根据同一份显式 allowlist 把工具名冻结进新 Task 的
authorization envelope。

这仍是 Optional Lab。常规 `config.local.toml`、默认与 production profile 均不启用，
远程部署也不复用这个无认证的 loopback server。Word profile 同时关闭主模型“首轮必须调用
工具”的部署开关：否则所有新 Task 都被迫提议 Word 工具，而没有 `mcp:word` 的普通 Task
会必然失败。是否生成文档由 writer 根据 objective 决定，scope 只负责权限上限。

### 2.2 输入是结构化文档描述，不接受路径

工具接收有界的结构化 JSON：必填 `title` 与 `sections`，可选 `subtitle`；每个 section
必填 `heading`，可选 `paragraphs`、`bullets` 与一个 headers/rows table。每层 object 都
`additionalProperties=false`，字符串长度、section 数、段落/项目/行列数量均有上限。它不
接收输入路径、输出路径、tenant、owner、artifact id 或 shell 参数。MCP Server 只负责生成
OOXML 字节，不决定这些字节属于谁，也不覆盖任何用户已有文件。

这让模型最多能决定“文档内容是什么”，不能决定“写进调用者机器的哪个位置”。路径沙箱不
因此多开一个例外；Microsoft Word GUI、AppleScript/COM 和 Office 宏均不进入服务端。

### 2.3 `.docx` 字节经既有结果映射进入 ArtifactStore

成功时 Server 只返回一个 `EmbeddedResource(BlobResourceContents)`：URI 是由文档 SHA-256
派生的 `urn:agent-workbench:word:<sha256>`，media type 固定为
`application/vnd.openxmlformats-officedocument.wordprocessingml.document`，blob 为
base64 DOCX；不同时返回重复的 text/structured content。现有
`adapters/mcp/result_mapping.py` 将二进制结果转成 `ToolResult.artifact`。Artifact 的
tenant/owner 只取 Worker 里的 `PrincipalContext`，并写入配置的 ArtifactStore。远端返回
内容不能指定本机路径或伪造所有者。

因此完整调用仍产生统一的：

```text
ToolProposed → PermissionResolved → ToolStarted → ToolCompleted
```

`ToolCompleted.artifact` 是取回文档的唯一事实；本地 Server 自己的临时文件不是交付接口，
也不承诺在一次调用后存在。

### 2.4 首版是“生成”，不是原地编辑

`render_document` 对相同结构化输入生成等价文档结果，不修改既有 Artifact。配置中的
`retryable_effects=true` 表示 synthesize 节点崩溃重放时允许再次渲染；它不宣称跨调用只
产生一个 Artifact，ADR-025 已记录 checkpoint 前崩溃可能留下无人引用 Artifact 的遗留。

后续读取与编辑遵守不可变语义：读取必须从已授权 Artifact 获取；编辑必须读取旧 Artifact、
生成新版本并保留来源关系，不能在原对象或任意本机文件上覆盖。加入这些工具前另行定义
schema、大小限制与版本关系。

### 2.5 MCP 可用与真实 Task 可用是两项验收

无模型 key 时，可以启动 Word MCP 并完成 `/health`、初始化与 `tools/list`；这只证明协议
服务可发现。`scripts/dev.sh worker` 在同一环境里仍诚实地运行 demo graph，demo graph 不会
调用 Word MCP。

真实 Task 验收必须同时满足：

1. PostgreSQL 已迁移，API 和真实 Task Worker 都使用 Word profile 启动；
2. Word MCP 在 **Worker 启动前**可达，因为工具目录只在启动时冻结；
3. 配置了真实模型 Provider；
4. 提交 principal 持有 `mcp:word`（若还要最终 Markdown 导出，同时持有
   `artifact:export`）；
5. 时间线出现上述四个工具事件，`ToolCompleted` 带 `.docx` ArtifactRef，且另一个
   principal 无法下载它。

## 3. 后果与非目标

- 项目展示的是“自研 Runtime 如何安全接外部文档能力”，而不是再实现一套 Agent executor；
- `.docx` 经过与其他工具结果相同的 Gateway、scope、事件与 Artifact 所有权控制；
- loopback health 和 MCP 目录可以无 Provider 独立排错；
- 本轮不提供 Chat Word 工具、不上传任意模板、不读取/覆盖现有 Word 文件、不驱动桌面版
Microsoft Word，也不把 Codex skill 当作部署依赖；
- MCP Server 若在 Worker 启动后才启动，需要重启 Worker重新冻结目录；本版没有热发现。

## 4. 被否决的方案

**让 Worker 直接调用 Documents skill。** Skill 属于开发环境，不是应用运行时协议，部署
路径也不稳定；拒绝。

**让模型传入绝对输出路径。** 这会把内容生成升级为任意文件写入，并绕过 ArtifactStore
所有权；拒绝。

**用 AppleScript 控制本机 Word。** 依赖 GUI session、授权弹窗和未版本化的桌面状态，无法
做确定性恢复或无头部署；拒绝。

**首版同时做读取和原地编辑。** 会一次引入输入 Artifact ACL、OOXML 不可信输入、版本关系
与覆盖语义，掩盖生成链路本身是否闭环；按不可变版本增量开发。
