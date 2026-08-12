# Agent Workbench

中文 | [English](README.en.md)

一个 clean-room 实现的通用 Agent 平台，提供两种产品形态：**Chat**（带权限校验的
知识库问答）与 **Task**（可恢复、可审批的自动化工作流）。

架构上只有一条主张：**自研 Agent Runtime 持有唯一的 Tool Loop**。LangGraph、
LlamaIndex、MCP 一律经 Port/Adapter 接入，负责各自那一段，不接管核心循环。

| 你是谁 | 从哪读 |
|---|---|
| 想看成色与证据 | [**十分钟版本**](docs/HIGHLIGHTS.md)——真实运行的事件流、门禁数字、四个技术判断 |
| 想立刻跑起来 | [快速开始](#快速开始)，一条命令，不联网、不连数据库 |
| 想知道**没做什么** | [**已知缺口**](docs/known-gaps.md)——四类分类，每条附位置与"做完"的判据 |
| 想读设计依据 | [文档地图](docs/README.md)、[架构基线](docs/architecture-baseline.md)、[ADR 索引](docs/adr/) |

---

## 一、功能

### 1.1 Chat：带权限校验的知识库问答

- **多轮对话**，会话与消息持久化在 PostgreSQL，`chat_turns` 是幂等事实源。
- **检索问答**：固定两步检索（`chat.retrieval_shape` 可选 `agentic`，**默认
  `fixed`**——固定形态才可复现评测）。答案给出引用，每条带 `chunk_id`、
  `document_id` 和 `document_version`。
- **权限贯穿全程**：检索候选按 ACL 过滤，答案发布前**再复核一次** source revision
  与授权；撤权与答案发布由文档行锁线性化。reranker 跑在授权之后，因此不可能引入
  提问者无权读的段落。
- **答不上来就说答不上来**，而不是给一个像模像样的答案。这是评测里单独考核的一项。
- **联网兜底**：语料答不上时可调用外部搜索（默认关闭）。用了网页的回答**不计为接地**，
  界面上区分显示。
- **每一轮列出被授权的工具**，调用过的高亮；工具调用显示"工具名 + 这次调的是什么"
  （如 `web_search · 北京今天天气`），失败显示错误消息而不是错误码。
- **流式输出**走 SSE，断线可按游标续读。

### 1.2 Task：可恢复、可审批的工作流

提交一个目标，Agent 自己拆解、检索、干活、产出文件，中途可以停下来等人批准。

**两张图，提交时选定并冻结**：

| 图 | 节点链路 | 用途 |
|---|---|---|
| 固定研究图 | `understand → plan → route →`{`research_internal`\|`research_external`}`→ synthesize → critic → quality_gate → approval → export` | 检索、综合、自我批评式的研究报告 |
| `v2_general` | `understand → work → review →`(`approval`)`→ export` | 通用干活：读工具、写工作区、渲染文档 |

- **提交预判（triage）**：`POST /v1/tasks/triage` 让模型先判该走哪张图，判不准就问人，
  失败回落默认值。
- **人在环中（HITL）**：`export` 这类外部副作用需要人批准。图在 LangGraph interrupt
  处停住，决定写进权威账本，跨进程恢复后重新施加。
- **任务工作区**：一个 Task 内可变的文件名压在不可变的字节上。写一个名字产生新
  manifest，manifest 本身也是 artifact——所以"工作区的哪一版"是 checkpoint 能持有的
  一个 id，节点重放看到的是它入口那一版。
- **一次性沙箱**（默认关闭）：一次调用一个容器，文件进文件出，无网络、只读根、
  非 root、丢弃 capability，内存/CPU/进程数/墙钟都有上限。
- **只读取用外部世界**（默认关闭）：`fetch_page` 与 `download_document` 都是 GET，
  取用前过**解析后**的地址闸门——只有全局可路由地址放行，重定向逐跳过闸。
- **产物导出**：`.docx` 等文件进 ArtifactStore，可在控制台直接读（文字预览）与下载。
- **全程留痕**：每次工具调用都留下 `ToolProposed → PermissionResolved → ToolStarted
  → ToolCompleted` 四件套，**被拒的那次也留痕**，而不是消失。

### 1.3 知识库与摄取

创建知识库 → 上传文件 → 异步摄取（解析、切块、向量化、写 Qdrant）→ 可检索。
支持 PDF、Word、Markdown、纯文本。

- 文档按 **revision** 管理，改版与撤权通过 revision 栅栏生效。
- 摄取 Worker 用 PostgreSQL `SKIP LOCKED` 竞争领取，带 lease/heartbeat/fencing。
- **摄取失败会说出口**：`documents` 表按 revision 记 `failed_revision` +
  `failure_code`，文档状态有 `failed` 一档，而不是永远显示"正在索引"。
- 知识库**提前声明自己是不是只读的**：只读时整块不渲染上传入口。

### 1.4 Web 控制台

React + TypeScript，六个页面：**Chat**、**Work**（任务时间线与生命周期）、
**Knowledge**（知识库与上传）、**Approvals**（待审批决定）、**Evaluation**
（评测报告）、**System**（运行时状态）。

### 1.5 接口与工具

**HTTP API**（FastAPI）：`/v1/chat`（会话、消息、SSE）、`/v1/tasks`（提交、查询、
时间线、取消、triage）、`/v1/knowledge-bases`、`/v1/uploads`、`/v1/search`、
`/v1/approvals`、`/v1/artifacts`（含 `/preview`）、`/health/live|ready`。

**命令行**：`agent-cli`（演示与提交）、`agent-api`、`agent-task-worker`、
`agent-ingestion-worker`、`agent-config-check`、`agent-evidence`，以及三个自有 MCP
server：`agent-word-mcp`、`agent-web-mcp`、`agent-sandbox-mcp`。

**Agent 可用工具**：`knowledge_search`、`web_search`、`external_search`、
`workspace_list/read/write/edit/grep`、`sandbox_run`、`export_artifact`，
以及经 MCP 接入的 `mcp_web_fetch_page`、`mcp_web_download_document`、
`mcp_word_render_document`。哪个 server 的工具进哪个 Agent 由配置的
`audience` 声明（`research` / `synthesis`），加一个读取器是改配置不是改代码。

**可观测**：OpenTelemetry trace 与 metrics（Port + OTLP Adapter，核心层不导入 SDK）。

---

## 二、架构

### 2.1 分层与依赖方向

```
┌─ 核心层 ───────────────────────────── 禁止 import 任何框架（CI 强制）─┐
│  runtime/    ClaudeLikeAgentRuntime —— Tool Loop、Policy Gateway、    │
│              预算与 deadline、取消、并行只读调度、exclusive 屏障      │
│  domain/     不变量写进类型（授权信封、预算、事件、工作区…）          │
│  workflows/  控制流是一份声明（两张图的节点、边、reducer）            │
│  application/ 用例编排（Chat 发布、Task 生命周期、恢复）              │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ 依赖
                  ┌────────▼────────┐
                  │     ports/      │  Protocol 契约，唯一的跨层接缝
                  └────────▲────────┘
                           │ 实现
┌──────────────────────────┴─────────── 框架只能待在这里 ──────────────┐
│  langgraph/   编译控制流声明，不接管 Tool Loop                        │
│  llama_index/ 只做检索，不生成答案                                    │
│  mcp/         官方 SDK v2，启动时冻结成普通 ToolBinding               │
│  persistence/ PostgreSQL —— 会话、任务、事件、checkpoint、outbox      │
│  vector/      Qdrant —— dense / hybrid / 本进程内 RRF 融合            │
│  embedding/ reranking/ models/ telemetry/ tools/ …                    │
└──────────────────────────────────────────────────────────────────────┘
```

这条边界是一条**会让 CI 变红**的测试
（[`tests/architecture/test_dependency_boundaries.py`](tests/architecture/test_dependency_boundaries.py)），
而且它连**方法调用**都禁——理由见[十分钟版本 §3.1](docs/HIGHLIGHTS.md#31-让越权在类型上不可能而不是靠信任)。

### 2.2 一次 Chat 问答的流转

```
提问 ──► Turn 幂等创建（Idempotency-Key，同会话活跃 Turn 不交错）
      ──► 检索：dense + sparse 双臂 ──► 本进程内 RRF 融合（按 (-score, chunk_id) 定序）
      ──► PostgreSQL ACL 过滤（授权发生在这里）
      ──► reranker 重排已授权候选（返回分数，不返回段落）
      ──► top_k ──► 渲染给模型 ──► 生成答案与引用
      ──► 发布门：复核 source revision + ACL ──► 与 assistant history、Turn 终态
          在同一个 PostgreSQL 事务里提交
```

发布门是最后一道：撤权发生在生成之后、发布之前时，答案被扣下（`AnswerWithheld`）
而不是发出去。

### 2.3 一次 Task 运行的流转

```
提交（tenant-scoped 幂等键 + 输入 fingerprint，授权信封随 Task 存下）
  └─► TaskSubmitted ──► Worker 用 SKIP LOCKED 竞争领取 ──► TaskClaimed(epoch)
        └─► LangGraph 按冻结的 graph version 执行
              每个节点：AgentExecutor ──► Tool Gateway（授权信封 + Policy）
                                      ──► 工具执行 ──► 事件 + checkpoint
              需要审批时：interrupt ──► waiting_approval ──► 人做决定
        └─► 崩溃/超时 ──► lease 过期 ──► 另一个 Worker 换 epoch reclaim
                       ──► 从 checkpoint 续跑，不从头再来
  └─► TaskSucceeded / TaskFailed（显式终态，没有"看起来成功"）
```

**可靠性机制**：执行租约（lease）+ 心跳 + epoch fencing、事务 Outbox、
自研 PostgreSQL checkpointer（带 fencing）、retry / dead-letter、advisory
execution guard、per-stream gap-free 事件序列与幂等 `event_key`。

节点在**领取时**拿到的不可变 `ExecutionLease` 下写入——不是每次向 Registry 问最新
epoch，否则失去租约的 Worker 会用顶替者的 epoch 通过账本围栏。

### 2.4 技术栈

| 层 | 选型 | 职责边界 |
|---|---|---|
| Agent Runtime | **自研** | Tool Loop、Policy、预算、取消——**不外包** |
| 工作流控制面 | LangGraph | 编译控制流声明；`TaskState` 字段即图通道 |
| 检索 | 自研 + LlamaIndex Adapter | LlamaIndex 只做检索契约，**默认未启用** |
| 向量库 | Qdrant | dense / sparse 存储；融合在本进程内做 |
| Embedding | BGE-M3 | dense + lexical，缺权重**拒绝构造** |
| 重排 | BGE reranker | 跑在授权之后，返回按位对齐的分数 |
| 模型 | DeepSeek（OpenAI 兼容） | 流式；服务端 `web_search` 不引入第二把 key |
| 持久化 | PostgreSQL 16 + Alembic | 会话、任务、事件、checkpoint、outbox |
| 工具协议 | MCP SDK v2 | Streamable HTTP，启动时冻结成本地 binding |
| 前端 | React + TypeScript + Vite | Chat/Work/Knowledge/Approvals/Evaluation/System |
| 可观测 | OpenTelemetry | Port + OTLP Adapter，核心层不导入 SDK |

配置为**单一 schema（当前 `1.14`）**，跨域校验在启动时完成；声称的能力与代码不符
会**在配置加载阶段失败**，而不是躺在那里没人读。

---

## 三、快速开始

前置：Python 3.12 与 `uv`。

**零依赖演示**——不需要数据库、不需要联网、不需要 API key，输出逐字节可复现：

```bash
uv run agent-cli demo
```

想看策略拒绝时 handler **完全不会被调用**：

```bash
uv run agent-cli demo --deny
```

**本地门禁**——先 `uv sync --frozen --group dev --no-editable`，把 `.env.example`
复制为 `.env` 并替换占位值，然后：

```bash
uv run agent-config-check --profile development && uv run ruff format --check . && uv run ruff check . && uv run pyright && uv run pytest
```

**完整本机拓扑**（PostgreSQL、Qdrant、API、Worker、控制台）见
[本机运行手册](docs/running-locally.md)；容器化演示见
[Compose 部署](docs/deployment.md)——API 只映射到 `127.0.0.1:8000`。

---

## 四、边界

> [!WARNING]
> **当前 Identity Adapter 只信任请求头**，因此 `agent-api` 只能用于受控的本机开发，
> 不得暴露到局域网或公网。监听地址与 Compose 端口均限制为 loopback，但那只是防止
> 意外暴露的机制，**不是身份认证**（[ADR-044](docs/adr/0044-no-remote-no-production-identity.md)）。

能力状态只按 `Planned → Implemented → Tested → Demonstrated` 升级，
**没有可链接的测试或演示证据不得升级**。当前明确未完成的包括：生产身份认证与远程
部署、RAGAS runner、Langfuse、CrewAI 对照 benchmark、动态 Multi-Agent supervisor
与 agent spawn、Chat 历史 compaction、旧 Qdrant Point 的物理清理。LlamaIndex 检索、
MCP、沙箱、只读取用与联网搜索**均默认关闭**，各有其不能打开的理由。

当前 Compose 只用于本机演示，不能作为生产部署或生产级多 Worker 的证明。

**逐条的分类、仓库位置与"做完了算什么样"，见[已知缺口](docs/known-gaps.md)**；
实测门禁数字与真实运行证据见[十分钟版本](docs/HIGHLIGHTS.md)。

---

## 五、文档

| 文档 | 用途 |
|---|---|
| [十分钟版本](docs/HIGHLIGHTS.md) | 真实事件流、门禁数字、技术判断 |
| [已知缺口](docs/known-gaps.md) | **没做的部分**，四类分类，附判据 |
| [实施状态](docs/status.md) | 逐 PR 的实现与测试证据 |
| [架构与技术选型基线](docs/architecture-baseline.md) | 产品边界、分层、可靠性协议 |
| [配置管理契约](docs/configuration.md) | 配置来源、密钥规则、快照语义 |
| [本机运行手册](docs/running-locally.md) ／ [Compose 部署](docs/deployment.md) | 怎么跑起来 |
| [前端设计基线](docs/frontend-design.md) | 前端结构、协议边界、响应式策略 |
| [ADR 索引](docs/adr/) | 33 份实施期决策记录（0012–0044） |
| [完整文档地图](docs/README.md) | 分层索引与按角色的阅读路径 |

---

## 许可证与来源边界

以 [Apache License 2.0](LICENSE) 发布。使用或分发时请保留
[NOTICE.md](NOTICE.md)——Apache-2.0 第 4(d) 条要求随附它。依赖各自的许可证不受本
仓库许可证影响，判定规则见 [compliance.md](docs/compliance.md)。

本仓库为 clean-room 实现，边界见 [NOTICE.md](NOTICE.md) 与
[合规说明](docs/compliance.md)。
