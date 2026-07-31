# Agent Workbench

中文 | [English](README.en.md)

Agent Workbench 是一个面向校招与作品集展示的 clean-room 通用 Agent
平台项目，目标是提供两种产品模式：

- **Chat Mode**：多轮对话、知识库问答和带权限校验的 RAG；
- **Task Mode**：可恢复的 LangGraph 工作流和可控 Multi-Agent 协作。

项目的自研 Agent Runtime 保持框架无关。LlamaIndex、LangGraph、LangChain
以及后续对比框架都通过明确的 Port/Adapter 接入，不接管核心 Tool Loop。

## 当前状态

截至 2026-07-29，开发工作位于
**`pr-050-postgres-checkpointer` 自 `5d943af` 之后的 A–F 汇合增量**；它不是 `main`
发布快照，但最终汇合工作树已经通过统一的静态、无状态和真实
PostgreSQL/Qdrant 门禁。当前可确认的实现包括：

- 框架无关的 Domain、Ports、Fake Adapter 与可复现 CLI 演示；
- 自研 `ClaudeLikeAgentRuntime`：Tool Loop、schema/Policy Gateway、预算与
  deadline、取消、并行只读调度、exclusive 屏障和 Hook Bus；
- DeepSeek OpenAI-compatible 流式 Adapter、配置投影与 API 装配；
- PostgreSQL ConversationStore、Alembic 迁移、Document/Version/ACL、
  事务 Outbox、`SKIP LOCKED` 竞争领取和摄取 Worker 组件；
- Local ArtifactStore，以及 FastAPI Upload/Artifact/Health/Chat/SSE API；
- PostgreSQL EventLog 的 per-stream gap-free sequence、显式 envelope schema version、
  生产者时间戳回放和 stream-local durable `event_key` 幂等写入；
- BGE-M3 Dense Embedding、Qdrant Dense/Hybrid 检索和离线 RAG 评测；
- BGE reranker：跑在**授权之后、`top_k` 之前**；Port 返回按位置对应的分数而不是
  重排后的列表，因此"reranker 不可能引入提问者无权读的 passage"由构造成立。
  超时、异常与分数条数不符都窄回退到已授权顺序，没有任何一条路径扩大授权范围；
- 缺少 `sparse_linear.pt` 时**拒绝构造** sparse 编码器：FlagEmbedding 会静默换上
  一个随机初始化的投影，让下游每一道检查都通过而毫无意义，错误信息里带着取回
  权重的命令；
- Task 工作流的 checkpoint-safe `TaskState` 与 `TaskWorkflowPort`；
- 固定研究图的**条件路由与确定性 fan-in reducer**（框架无关，不依赖 `langgraph`）：
  fan-in 是排序并集，因此合并结果与分支完成顺序无关、重放一个分支不产生重复引用；
  revision 预算耗尽的 quality gate **不返回下一个节点**，而不是放行去审批——批准一份
  critic 明确否决的草稿，会让质量门恰在最该起作用时变成形式；
- LangGraph Adapter：**编译**控制流声明而不是重述它，因此重放 checkpoint 的图与
  控制流测试断言的图是同一份；`TaskState` 字段即图通道，两个引用通道挂与控制流
  同一个排序并集 reducer；未注册的 graph version 失败关闭而不回退到最新图；
  `resume` 不重传初始状态；
- Task Agent node 只经 `AgentExecutor` 到达模型（不持有 registry / model port，
  否则就是第二个没有预算与 Policy 保证的 Runtime）；**失败的运行同样记录 run id
  与 usage**，否则 Task 会在一个看起来从没动过的预算里无限重试；`completed` 但没有
  产物被判为失败而不是空成功；prompt 是状态投影而不是转录，先前节点的输出留在
  artifact store 里；
- 固定 2-step Chat 的 ACL 双重检查、答案发布门、source revision 读取栅栏、已提交
  会话消息的多轮回放，以及 PostgreSQL `chat_turns` 幂等事实源；
- 最终 source revision/ACL 复核、`AnswerCommitted/AnswerWithheld`、assistant
  history 和 Turn 终态在同一 PostgreSQL 事务中提交；撤权与答案发布由文档行锁
  线性化；
- Chat API 强制 `Idempotency-Key`，同一会话的活跃 Turn 不交错；已提交请求重试不再
  重跑模型，`release_pending` 重试会重新验证持久化的 evidence revision；
- `running` Turn 使用固定执行 lease；所有离开 `running` 的 writer 都在 Turn 锁内
  复核数据库时间，claim 不机会式回收，迟到的 prepare/cleanup 不写裸终态；
- 硬崩溃遗留项由 `ChatExpirationCoordinator` 使用 PostgreSQL `SKIP LOCKED` 回收；
  每个 Turn 的 `failed(deadline, stale_execution)`、lease 清理和 durable
  `ChatTurnExpired` 在独立事务中原子提交，一个毒化候选不会阻断后续候选；
- 答案发布与过期竞争共用有界终态键
  `chat-turn:{sha256(turn_id)}:terminal`，因此同一 Turn 不能同时提交两种终态事件；
  `ChatTurnExpired` 是 Chat ledger 事实，不是第二个 Runtime `RunFailed`；
- 后台 pending-release recovery 会重新执行最终 ACL/revision 栅栏并原子发布，
  不依赖原客户端用同一幂等键重试；即使 embedding/model 不可用也继续恢复；
- 与固定检索共用 `RetrievalService` 的 `knowledge_search` Tool Adapter。
- **A/B 已完成：**Task 工作流具有显式成功/失败终态和正确的 revision 预算语义；
  Task 提交使用 tenant-scoped 幂等键与输入 fingerprint，API 查询按 owner/tenant
  失败隐藏；
- **C 已完成：**TaskInput 以 Artifact 持久化，Task API、CLI、独立
  `agent-task-worker` 入口、poll loop 与显式 demo composition 已形成单 Worker
  纵向切片；
- **D/E 主体完成并通过回归：**真实 Task handlers、内部研究与 evidence Artifact、
  PostgreSQL `SKIP LOCKED` claim、lease/heartbeat/epoch、stale reclaim、
  retry/dead-letter、advisory execution guard、fenced checkpointer 和生命周期事件
  已进入工作树，并通过真实 PostgreSQL/Qdrant 全量状态测试；
- **F 部分完成：**Qdrant 启动校验、常驻摄取 Worker 的
  claim/heartbeat/fencing、Task 生命周期时间线以及本机 Compose 演示拓扑已经实现。

这些能力仍有明确边界：

- 旧 Qdrant Point 已被 revision 栅栏阻止读取，但 replace/delete 物理清理尚未完成。
- Chat 的历史 token window/compaction 和模型实际引用校验尚未实现；
  `knowledge_search` 尚未装配为可用的 Agentic Retrieval Mode。
- EventLog 能拒绝未知 schema version，但尚未实现旧版本 upcaster、poison-row
  隔离/跳过策略。
- 三臂消融的 `hybrid-rerank` 臂尚未跑：hybrid 在当前 38 题 gold set 上已打满
  1.000，rerank delta 必然为 0；要测出它得先有更难的 gold set。
- 外部搜索目前只有 provider-neutral Port、Tool/Policy 接入和失败关闭 Adapter，
  **没有真实搜索服务 Provider**。
- `waiting_approval`、恢复决策和事件类型已经存在，但 Approval 的创建、决策 API、
  授权复核及恢复闭环尚未贯通，不能表述为已完成 HITL。
- OTel/Langfuse、CrewAI 对比、动态 Multi-Agent supervisor/workers、UI、
  生产身份认证和生产部署仍未完成；LlamaIndex/LangChain 业务 Adapter 也尚未形成
  可演示纵向切片。
- 当前 Compose 只用于本机演示，不能作为生产部署或生产级多 Worker 证明。
- 当前汇合工作树门禁为：Ruff format/lint 通过，Pyright `0 errors`，无外部服务
  `1054 passed / 409 skipped`，真实 PostgreSQL + Qdrant
  `1452 passed / 11 skipped`；剩余状态测试跳过项需要本地真实 BGE 权重。

> **安全警告：** 当前 Identity Adapter 只信任请求头，因此 `agent-api` 只能用于
> 受控的本机开发，不得暴露到局域网或公网。监听地址以及 Compose 端口映射均限制为
> loopback（默认 `127.0.0.1`），但那只是防止意外暴露的机制，不是身份认证——真实
> 身份提供方仍未实现。

完整增量、测试证据、已知缺陷和未实现边界见
[实施状态](docs/status.md)。

## 快速体验

```bash
uv run agent-cli demo
```

脚本化模型离线运行，不联网、不连数据库；同一条命令的输出逐字节可复现。
想看被策略拒绝时 handler 完全不会被调用：

```bash
uv run agent-cli demo --deny
```

## 本地配置检查

前置条件：Python 3.12 和 `uv`。

1. 安装锁定后的开发环境：
   `uv sync --frozen --group dev --no-editable`。
2. 将 `.env.example` 复制为 `.env`，替换仅用于本地开发的占位值。
3. 执行：

```bash
uv run agent-config-check --profile development
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

配置检查只验证结构和安全不变量，不会连接 PostgreSQL、Qdrant 或在线模型。
测试和静态检查在依赖同步完成后可以离线运行。

## 本机容器部署

本机 PostgreSQL、Qdrant、迁移和 API 可通过 `docker compose up --build` 启动；API
只映射到 `127.0.0.1:8000`。Task/Ingestion worker 仅在显式 `demo` profile 中以
`--demo` 启动，不代表生产 worker 部署。完整命令、限制与 secret 注入方式见
[本机 Compose 部署](docs/deployment.md)。

## 设计依据

- [文档索引](docs/README.md)
- [架构与技术选型基线 v1.3](docs/architecture-baseline.md)
- [代码实施计划 v1.0](docs/implementation-plan.md)
- [配置管理契约](docs/configuration.md)

clean-room 边界见 [NOTICE.md](NOTICE.md) 和
[docs/compliance.md](docs/compliance.md)。

当前实现证据记录在 [docs/status.md](docs/status.md)。
