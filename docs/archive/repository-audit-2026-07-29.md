# Agent Workbench 当前实现完成度与缺陷报告

> [!IMPORTANT]
> 本报告第 1～9 节是 **A–F 修复开始前**、基于
> `pr-050-postgres-checkpointer@5d943af` 的审计快照。为保留缺陷发现、修复动机和
> 证据链，历史正文没有按当前工作树倒写；其中“尚不存在”“存在缺陷”等表述应按
> 修复前事实阅读。当前增量进度以紧随其后的进度表和
> [实施状态](../status.md)顶部快照为准。

> - 复核日期：2026-07-29
> - 当前工作分支：`pr-050-postgres-checkpointer`
> - 当前分支 HEAD：`5d943af2bb521b44bc7a7550ee768908d0677cf1`
> - 主分支基线：`main@f5800d2449cc924b4be1a7b8f9a396db5f452c65`
> - 对应远端变更：GitHub PR `#62`
> - 复核范围：源码、迁移、配置、API/CLI 装配、测试、CI、评测与文档
> - 复核方法：静态审查、类型/格式门禁、全量测试、关键路径最小复现和并行专项审查

## A–F 修复进度（2026-07-29 当前工作树）

本表描述 `5d943af` 之后的 A–F 汇合增量，不把目标文档、接口占位或测试夹具
等同于产品完成。

| 修复组 | 进度 | 当前工作树的增量 | 剩余边界 |
|---|---|---|---|
| **A：Task 工作流终态语义** | **完成** | 增加显式 workflow disposition；revision 使用正确预算计数；critic 拒绝且预算耗尽时失败关闭 | 纳入最终统一回归 |
| **B：Task 提交幂等与租户隔离** | **完成** | tenant-scoped 唯一键、输入 fingerprint、冲突复用持久化身份以及 owner/tenant API 隔离已落地；迁移推进到 `0014` | 纳入最终统一回归 |
| **C：单 Worker 产品纵向切片** | **完成** | TaskInput Artifact、Task Router/CLI、`agent-task-worker` 入口、poll loop、composition root 与显式 demo 路径已落地 | 仍只适合受控开发环境 |
| **D：真实 Task Agent handlers** | **主体完成并通过回归** | `plan`/`critic` 结构化处理、内部研究、evidence Artifact、TaskRunContext、取消与提交时 principal scopes 已接入；外部检索通过 Tool/Policy 边界 | 真实外部搜索 Provider 尚未实现，当前缺失时失败关闭 |
| **E：可靠 Task Core** | **主体完成并通过状态测试** | `SKIP LOCKED` claim、lease/heartbeat/epoch、stale reclaim、retry/dead-letter、专用 advisory execution guard、fenced checkpointer、生命周期事件和确定性 failpoint 已落地；迁移推进到 `0016` | HITL 与外部副作用 ledger 仍属于后续工作 |
| **F：产品化补全** | **部分完成** | Qdrant startup/readiness 不变量、摄取 Worker 入口及 outbox claim/heartbeat/fencing、Task 时间线和本机 Compose 拓扑已落地 | HITL、真实外部搜索、OTel/Langfuse、CrewAI、UI、生产身份与生产部署未完成 |

最终汇合工作树已经记录统一门禁：Ruff format/lint、Pyright、Compose 静态校验和
Alembic 唯一 head 均通过；无外部服务测试为 `1054 passed / 409 skipped`，真实
PostgreSQL + Qdrant 测试为 `1452 passed / 11 skipped`。这些数字是本次修复后的
证明；下文历史数字仍只属于修复前快照。

## 1. 状态定义

本报告使用三个互斥分类，避免把“已经定义接口”误写成“产品已经完成”。

| 分类 | 判定标准 |
|---|---|
| **已完成** | 有真实实现、有生产或演示入口、有装配路径，并有与能力相匹配的测试证据 |
| **未完成/部分完成** | 只有 Domain、Port、Adapter、可调用组件或测试夹具，尚未形成可运行纵向切片；或者目标能力完全没有代码 |
| **有缺陷** | 已实现路径的实际行为与它声明的契约不一致，存在正确性、安全性、租户隔离或恢复可靠性问题 |

“组件完成”不等于“产品完成”。例如 PostgreSQL Checkpointer 可以作为一个完成的
Adapter 组件，但只要没有 Task API、Worker 进程和自动故障接管，就不能说 Task Mode
已经完成。

## 2. 执行摘要

当前最准确的项目定位是：

> 框架无关自研 Agent Runtime + 已装配的固定两步 Chat/Dense RAG Alpha；
> 当前分支新增了 PostgreSQL Task 持久化和单 Worker 基础组件，但 Task Mode 尚未形成
> 产品级端到端链路，也尚未具备多 Worker 自动恢复能力。

相对 `main`，当前分支增加 33 个文件变更，约 `7908` 行新增代码，主要落在：

- PostgreSQL LangGraph Checkpointer；
- Task Registry 和 Task 生命周期；
- TaskService、授权查询和统一事件时间线；
- 单 Worker 的 run/resume/reconcile；
- Task 提交语义快照；
- FakeAgentExecutor 和跨进程 checkpoint 恢复测试。

这些增量是真实工程进展，但 PR 当前对“死 Worker 由其他进程接管完成”的描述超过了
代码实际能力。现阶段只证明了 checkpoint 可以被另一个进程读取和继续；没有证明
一个处于 `running` 的失联 Task 会自动重新进入可领取状态。

## 3. 已完成

### 3.1 工程基础与配置

| 能力 | 完成层级 | 证据 |
|---|---|---|
| Python 3.12、uv 锁文件、严格 Pyright、Ruff | 工程完成 | `pyproject.toml`、`.github/workflows/ci.yml` |
| Domain / Port / Adapter 分层和依赖方向测试 | 工程完成 | `tests/architecture/` |
| Pydantic Settings、default/test/production profile、配置 ownership | 工程完成 | `src/agent_workbench/bootstrap/settings.py`、`config/ownership.yaml` |
| `agent-config-check` 离线配置验证 | 入口完成 | `src/agent_workbench/bootstrap/config_check.py` |
| `pydantic-settings>=2.14.2` 安全下限 | 依赖约束完成 | `pyproject.toml` |
| Alembic 迁移链 | 组件完成 | `migrations/versions/0001` 至 `0012`，当前唯一 head 为 `0012_task_submitted_semantics` |
| CI 质量门 | 工程完成 | 锁文件、格式、lint、类型、配置、pytest、CLI golden、license、Gitleaks |
| PostgreSQL/Qdrant 服务测试 | CI 完成 | CI 的 `stateful` Job 真实启动 PostgreSQL 16 和 Qdrant |

### 3.2 自研 Agent Runtime

| 能力 | 完成层级 | 证据 |
|---|---|---|
| `Model → Tool → ToolResult → Model` 主循环 | 运行时完成 | `src/agent_workbench/runtime/agent_runtime.py` |
| Tool schema 校验、Policy Gateway、授权 Envelope | 运行时完成 | `runtime/schema_validation.py`、`runtime/tool_gateway.py` |
| Token/步骤/Tool 调用预算和 deadline | 运行时完成 | `runtime/budgets.py`、`runtime/agent_runtime.py` |
| 取消传播到 Runtime、模型和 Tool 调用 | 运行时完成 | `ports/cancellation.py`、Runtime 测试 |
| 并行只读 Tool、exclusive 屏障和确定性结果顺序 | 运行时完成 | `runtime/tool_scheduler.py` |
| Hook Bus | 运行时完成 | `runtime/hook_bus.py` |
| 统一 EventSink 和 AgentOutcome | 契约完成 | `ports/event_log.py`、`domain/runs.py` |
| Fake Model/Fake Tool 离线栈 | 演示完成 | `agent-cli demo` 和 golden tests |
| DeepSeek OpenAI-compatible 流式 Adapter | Adapter 与 API 装配完成 | `adapters/models/deepseek.py`、`bootstrap/model_factory.py` |

这里的“Runtime 完成”指 v1 约定的单次进程内 Tool Loop。Runtime mid-loop 跨进程恢复
和任意动态 Agent spawn 本来就不属于当前 v1 范围。

### 3.3 Chat Mode

| 能力 | 完成层级 | 证据 |
|---|---|---|
| FastAPI 会话创建与 Chat 请求 | 产品入口完成 | `apps/api/routes/chat.py` |
| 固定两步 `retrieve → model → release` | 产品装配完成 | `application/chat.py`、`apps/api/dependencies.py` |
| PostgreSQL ConversationStore | 持久化完成 | `adapters/persistence/conversation_store.py` |
| 已提交消息的多轮历史回放 | 应用完成 | Chat application/contract tests |
| `Idempotency-Key` 和同会话 Turn 不交错 | 应用与数据库完成 | `chat_turns` 迁移与 Chat tests |
| 最终 ACL/source revision 复核 | 安全发布完成 | `application/answer_release.py` |
| Answer、assistant history、Turn 终态和 durable event 同事务 | 一致性完成 | PostgreSQL release adapter/tests |
| `running` Turn 固定 lease、过期回收 | 恢复完成 | `adapters/persistence/chat_expiration.py` |
| `release_pending` 无客户端流量恢复 | 恢复完成 | `application/chat_recovery.py` |
| SSE 断点游标与 durable EventLog | 产品入口完成 | `apps/api/routes/events.py`、`adapters/persistence/event_log.py` |

Chat 当前可以被称为“固定两步 RAG Chat Alpha”，不能称为完整 Agentic Chat。

### 3.4 RAG、摄取和评测中已经完成的组件

| 能力 | 完成层级 | 证据 |
|---|---|---|
| 文档上传、Artifact 与 ACL 持久化 | API/持久化完成 | Upload/Artifact routes、`adapters/persistence/documents.py` |
| Parser、Chunker、Dense Embedding 流程 | 组件完成 | `application/ingestion.py` |
| BGE-M3 Dense Adapter | Adapter 完成 | `adapters/embedding/bge.py` |
| BGE-M3 sparse lexical Adapter 与权重加载守卫 | Adapter 完成 | `adapters/embedding/bge_sparse.py`、ADR-013 |
| Qdrant Dense/Hybrid 查询和 metadata filter | Adapter 完成 | `adapters/vector/qdrant.py` |
| PostgreSQL 授权重验与 source revision 读取栅栏 | 检索完成 | `application/retrieval.py` |
| BGE reranker 位于授权之后、`top_k` 之前 | 组件完成 | `adapters/reranking/bge_reranker.py` |
| `knowledge_search` Tool Adapter | Adapter 完成 | `adapters/tools/knowledge_search.py` |
| 固定语料、38 题 gold set、IR metrics 和 dense/hybrid 报告 | 离线评测完成 | `evals/rag/`、`scripts/run_rag_eval.py` |

其中 Hybrid、IngestionWorker 和 `knowledge_search` 只是组件完成，尚未全部接入产品入口，
具体见第 4 节。

### 3.5 当前分支已经完成的 Task 组件

| 能力 | 完成层级 | 证据 |
|---|---|---|
| checkpoint-safe `TaskState`、Plan/Review/Budget schema | Domain 完成 | `domain/tasks.py` |
| 固定研究图和条件路由 | 控制流组件完成 | `workflows/research_graph.py` |
| 并行研究分支的 sorted-union fan-in | 控制流组件完成 | `merge_refs()` 及测试 |
| graph version registry 与 mismatch fail-closed | Adapter 完成 | `adapters/langgraph/workflow.py` |
| `resume` 不重复提交初始状态 | Adapter 完成 | LangGraph workflow tests |
| PostgreSQL checkpoint/blob/write 三表 | 持久化组件完成 | migration `0010` |
| async PostgreSQL CheckpointSaver | Adapter 完成 | `adapters/langgraph/checkpointer.py` |
| checkpoint/blob 同事务写入和 pending writes 恢复 | Adapter 完成 | PostgreSQL saver tests |
| Task 状态机和条件状态迁移 | Domain/Repository 完成 | `domain/task_registry.py`、`task_registry.py` |
| Task row 与 `TaskSubmitted` 同事务提交 | 持久化完成 | `PostgresTaskRegistry.submit()` |
| TaskService 的 owner+tenant 查询隔离 | 应用服务完成 | `application/tasks.py` |
| Task timeline 的 cursor/limit/跨 stream 校验 | 应用服务完成 | `TaskService.timeline()` |
| 单 Worker `claim → reconcile → run/resume → settle` | 可调用组件完成 | `workers/task.py` |
| checkpoint/registry 漂移的纯恢复判定 | 应用逻辑完成 | `application/task_recovery.py` |
| FakeAgentExecutor | 测试/演示组件完成 | `runtime/fake_executor.py` |
| 提交时运行语义与 policy identity 落库 | 数据模型完成 | migration `0012` |

这些 Task 项目均属于“组件完成”，尚未达到产品纵向切片完成。

## 4. 未完成或只有部分实现

### 4.1 Chat/RAG 未完成项

| 未完成项 | 当前实际状态 | 完成条件 |
|---|---|---|
| 上传后自动可检索 | `IngestionWorker.drain()` 仅可手工构造调用 | 增加常驻 Worker 入口、poll loop、启动自检和产品 E2E |
| Qdrant collection 自举 | `ensure_collection()` 有实现但启动链未调用 | API/ingestion 启动时校验或创建 collection |
| Hybrid Chat | sparse 写入/检索组件存在，API 只装配 dense encoder | Bootstrap 注入 sparse encoder，并跑真实产品链测试 |
| Agentic Retrieval | `knowledge_search` 存在，但 API Tool Registry 为空，Chat 的 `tool_names=()` | 注册 Tool、授权 Tool、放宽步骤预算并加入最终 evidence gate |
| 历史 token window/compaction | 只有状态和 compact profile | 实现 ContextEngine、摘要 Artifact、恢复与评测 |
| 可验证 Citation | 当前返回检索包的 citations，未验证模型实际使用 | 增加引用协议、answer claim 映射和 citation precision/recall |
| 旧 Qdrant Point 物理清理 | revision 栅栏阻止读取，但旧 Point 保留 | replace/delete、GC reservation 和恢复测试 |
| EventLog upcaster/poison-row 隔离 | 能拒绝未知版本，但不能升级/跳过毒化事件 | upcaster registry、隔离策略和回放测试 |
| 实时 SSE 唤醒 | 当前 SSE 轮询数据库 | 接入 PostgreSQL LISTEN/NOTIFY，事件事实仍从表读取 |
| Claude Provider | Port 保持 provider-neutral，但只有 DeepSeek Adapter | 增加 Anthropic/Claude Adapter 和 contract tests |
| LlamaIndex RAG Adapter | 仓库无依赖、无 Adapter、无测试 | 明确 LlamaIndex 只负责 ingestion/query adapter，不接管 Agent loop |

### 4.2 Task Mode 未完成项

| 未完成项 | 当前实际状态 | 完成条件 |
|---|---|---|
| Task HTTP/CLI | 没有 Task Router 或 CLI command | submit/get/cancel/timeline API 与鉴权测试 |
| Task Worker 进程 | 只有 `run_once()`，没有 console script/poll loop | `agent-task-worker`、优雅退出、健康与 lag |
| Task Bootstrap | 没有装配 Registry、Saver、Workflow、Worker | 独立 composition root 和 startup smoke |
| Task 初始状态加载 | `load_state` 只由测试注入 | 从 `input_ref`、语义快照和当前授权构造 `TaskState/TaskRunContext` |
| Graph 真实 handlers | 默认 handler 是 passthrough | 注入 Agent node、EventSink、CancellationToken、ArtifactStore |
| `plan`/`critic` | 没有结构化输出 decoder | schema-constrained decode、错误处理和恢复测试 |
| 研究工具 | artifact nodes 的 `tool_names=()` | internal/external research Tool 和最小权限 Envelope |
| Synthesize 读取证据 | prompt 只有 objective/plan | 按 evidence refs 从 ArtifactStore 取回受控上下文 |
| HITL Approval | approval 节点是 placeholder | ApprovalStore/API、interrupt、原子 requeue、幂等 resume |
| Task 事件时间线 | 实际基本只有 `TaskSubmitted` | claim/node/checkpoint/approval/terminal durable events |
| Task index generation reservation | 只有通用 snapshot | 保存 concrete collection/index/generation，resume 不解析 alias |
| 自动硬崩溃恢复 | `running` Task 不会重新变成可领取 | lease/heartbeat/reaper/reclaim/fencing |
| 多 Worker | 源码明确只安全单 Worker | `SKIP LOCKED`、epoch、session advisory lock、执行 guard |
| 外部副作用恢复 | 没有 tool execution ledger | intent/result ledger、稳定 operation key 和人工核对状态 |
| Checkpoint retention | `adelete_thread` 和 orphan cleanup 未实现 | 三表事务删除、保留策略和 GC |

### 4.3 Multi-Agent、框架覆盖、运维与作品集展示

| 未完成项 | 当前实际状态 |
|---|---|
| 自研 supervisor + workers + mailbox | 只有固定图的并行研究节点，不是动态 supervisor/worker 协作 |
| CrewAI 对比 | 只有配置约束；无依赖、Adapter、benchmark 或报告 |
| LangChain Adapter | LangGraph 带来生态依赖，但项目没有业务级 LangChain Adapter |
| RAGAS | 没有依赖或 judge pipeline；当前只有确定性 IR 评测 |
| Task/Multi-Agent benchmark | 配置指向的 `evals/tasks/cases.yaml` 不存在 |
| OpenTelemetry | 只有配置字段，无 SDK、trace exporter 或埋点 |
| Langfuse | 只有 optional profile 配置，无 Adapter |
| Web UI | 没有前端目录或最小控制台 |
| Dockerfile/Docker Compose | 仓库没有镜像和 Compose 文件 |
| 生产身份认证 | 仅开发请求头 Identity，API 强制 loopback |
| S3/Presigned Upload | Settings 接受 `s3`，运行装配会拒绝 |
| 生产部署 | production profile 能校验，但 remote API 启动会 fail closed |

Redis 和 AutoGen 不列为缺失项：Redis 已被当前 PostgreSQL-only 控制面决策明确排除；
AutoGen 也不在已锁定的 v1 对比范围内。

## 5. 已实现路径中的缺陷

### 5.1 阻断当前 Task PR 正确性的缺陷

#### D-01：质量门拒绝结果最终被标记为成功

`route_quality_gate()` 在 critic 仍要求修改且预算耗尽时返回 `None`。LangGraph Adapter
把它映射为普通 `END`，随后：

```text
END
→ pending_nodes 为空
→ CheckpointPosition.finished = true
→ reconcile = settle_succeeded
→ task_runs.status = succeeded
```

证据：

- `src/agent_workbench/workflows/research_graph.py:99`
- `src/agent_workbench/adapters/langgraph/workflow.py:137`
- `src/agent_workbench/application/task_recovery.py:166`
- `src/agent_workbench/workers/task.py:153`

修复要求：为图结果增加明确的 success/failed/rejected disposition，或者写入可恢复的
终态原因；不能再用“没有下一节点”同时表示成功与拒绝。

#### D-02：revision budget 没有真正推进

`begin_revision()` 已定义但没有生产调用方。`quality_gate → synthesize` 不增加
`revision_count`，导致 `max_revisions` 不生效，最终由 LangGraph recursion limit
偶然终止。

最小复现中，`max_revisions=2` 时 `synthesize` 和 `critic` 各执行 7 次，最终得到
`GraphRecursionError`。

修复要求：revise edge 必须原子应用 `begin_revision()`，清除旧 review，并增加“精确执行
N 次修改而非依赖 recursion limit”的测试。

#### D-03：TaskService 的幂等重试必然冲突

`TaskService.submit()` 每次都生成新 `thread_id`，Registry 又把 `thread_id` 放进
`_SUBMISSION_IDENTITY`。相同用户、相同 key、相同 input 的第二次调用会得到
`TaskSubmissionConflictError`。

证据：

- `src/agent_workbench/application/tasks.py:126`
- `src/agent_workbench/adapters/persistence/task_registry.py:53`

修复要求：冲突时使用已存 Task 的 `thread_id` 和 event stream；只比较真正标识客户端
请求的字段，不比较每次重试都会重新产生的服务端字段。

#### D-04：submission dedup 唯一键缺少 tenant

数据库、`ON CONFLICT` 和查询都使用：

```text
(owner_id, submission_dedup_key)
```

而应用授权边界使用 `(tenant_id, owner_id)`。不同 tenant 中相同 owner ID 会互相冲突，
形成跨租户 DoS/存在性侧信道。

修复要求：新增不可回写的后续 Alembic revision，把约束和仓储条件统一成：

```text
(tenant_id, owner_id, submission_dedup_key)
```

#### D-05：PR 宣称的死 Worker 自动接管并不存在

当前 claim 只有 `queued → running`；`task_runs` 没有 lease、worker owner、epoch 或
heartbeat。进程在 claim 后被杀死，Task 会永久留在 `running`，其他 Worker 无法领取。

当前恢复测试证明的是“另一个 Saver 可以读取持久 checkpoint”，部分 Worker 测试还通过
测试 SQL 手工把状态改回 `queued`。这不等于产品自动恢复。

修复要求二选一：

1. 当前 PR 缩小范围并改名为 durable Task foundation；或
2. 实现 lease/heartbeat/reclaim/advisory lock/fencing 后再保留自动恢复声明。

### 5.2 高优先级可靠性与装配缺陷

#### D-06：同一 thread 的并发 Saver 写入没有 fencing

Saver 的单次 `aput` 是事务性的，但没有 task epoch、lease guard、thread lock 或 CAS。
两个执行者从相同 parent 出发可以写出分叉 checkpoint；latest 查询最后只会选择其中一个。

`get_next_version()` 的随机后缀只避免 blob 主键碰撞，不能提供单执行者语义。

#### D-07：Task 结算与取消竞争会让 Worker 抛异常

`_fail()` 捕获 `TaskTransitionRejectedError` 并读取当前终态；`_settle()` 的
`mark_succeeded/park_for_migration/await_approval` 没有相同处理。取消若发生在最终
reconcile 与状态更新之间，Task 状态本身正确，但 Worker 调用会异常退出。

#### D-08：Qdrant collection 启动自检没有装配

`QdrantVectorIndex.ensure_collection()` 存在，测试也显式调用，但 API Chat
Bootstrap 只构造 Adapter，不执行 collection 创建/schema 校验。全新环境可能通过
API 启动，直到第一次检索才失败。

#### D-09：`qdrant.read_alias` 配置未生效

配置声明 read alias，但 Chat 装配把 retrieval collection 绑定到
`write_collection`。blue/green alias 切换不能影响实际读取目标。

#### D-10：多 IngestionWorker 可发生旧 revision 迟到覆盖

Worker 只在 PostgreSQL 中读取 snapshot 时持锁，Qdrant upsert 发生在锁外。旧 Worker
可能在新 revision 写入之后迟到 upsert。当前源码也明确限定只安全单 Worker，因此该项
在单 Worker profile 下是边界，在启用并发前是必须修复的缺陷。

#### D-11：配置声明的 Task 可靠性能力没有运行实现

默认配置声明：

- `claim_strategy = "skip_locked"`
- lease/heartbeat；
- advisory lock；
- fenced checkpointer；
- tool execution ledger。

当前没有 Task Bootstrap 消费这些配置，Registry 也没有对应字段/协议。配置验证通过只说明
schema 合法，不能作为能力已经启用的证据。

### 5.3 中优先级契约和维护缺陷

| 缺陷 | 影响 |
|---|---|
| Checkpointer 缺 blob 时静默丢 channel | 数据损坏可能被解释成可恢复状态，应 fail closed |
| 默认 LangGraph serde 信任数据库 payload | checkpoint 表写权限是反序列化代码执行边界，需要受限 serde 或明确威胁模型 |
| Checkpointer 读取不是显式一致快照 | 在相同 checkpoint ID 被并发覆盖时可能拼接不同提交的 row/blob |
| `adelete_thread`/retention 未实现 | checkpoint、blob 和无 FK writes 永久增长 |
| Task timeline 基本只有 `TaskSubmitted` | 用户看不到 claim、node、checkpoint 和 terminal 过程 |
| TaskRun/TaskSubmission 未纳入 VersionedModel 反射契约 | 持久化模型缺少统一 schema version/golden round-trip 门禁 |
| Citation 未校验模型实际引用 | API 返回“检索到的来源”，不是“回答实际引用的来源” |
| 旧 Qdrant Point 不物理删除 | 不影响当前读取正确性，但增加存储和合规清理压力 |
| SSE 使用数据库轮询而非 LISTEN/NOTIFY | 当前量级可运行，但与配置和目标架构不一致 |

### 5.4 文档缺陷

当前 README、英文 README、文档索引和实施计划的顶部状态仍停留在较早提交：

- 写着 `main@341cbf5`，真实 main 已是 `f5800d2`；
- 写着 PostgreSQL Checkpointer、Task Registry、Task Worker 不存在；
- 部分状态又声称 WP06 所有退出条件已经满足；
- 当前配置和文档分别把未实现能力写成 enabled/已完成。

准确表述应是：

> Task 的 Domain、LangGraph Adapter、PostgreSQL Saver、Registry、Service 和单 Worker
> 组件已经存在于当前未合并分支；Task API、真实 handlers、Worker 进程、自动失联恢复、
> HITL 和多 Worker fencing 尚未完成。

## 6. 按架构里程碑判断

| 里程碑 | 当前判断 | 原因 |
|---|---|---|
| M0 工程与契约基线 | **完成** | 配置、Domain/Ports、CI、迁移、Fake stack 齐备 |
| M1 自研 Runtime | **基本完成** | v1 Tool Loop 完成；Claude Adapter/compaction 属后续 |
| M2 Chat + RAG | **部分完成，可演示 Alpha** | 固定 Dense Chat 可运行；摄取常驻、Hybrid/Agentic、Citation 未贯通 |
| M3a 单 Worker Task MVP | **未通过退出条件** | 组件较多，但无产品入口、真实 handlers，且有终态/幂等缺陷 |
| M3b 可靠 Task Core | **未完成** | lease、epoch、fencing、reclaim、failpoints 尚未落地 |
| M4 HITL 与幂等副作用 | **未完成** | Approval 和 tool execution ledger 不存在 |
| M5 Multi-Agent/框架对比 | **未完成** | supervisor/workers、CrewAI benchmark 不存在 |
| M6 可观测、UI 与部署 | **未完成** | OTel/Langfuse/UI/Docker/生产认证尚未实现 |

## 7. 测试与门禁结果

本轮复核得到：

```text
ruff check .                         PASS
ruff format --check .                PASS（235 files）
pyright                              PASS（0 errors）
python -m compileall                 PASS
alembic heads                        0012_task_submitted_semantics
pytest（排除沙箱 socket.bind 用例）   952 passed / 365 skipped / 1 deselected
git diff --check main...HEAD         PASS
```

本地唯一原始失败是测试进程在受限沙箱里执行 `socket.bind` 得到
`PermissionError`，不是业务回归。

本地 skips 主要来自未提供 PostgreSQL DSN、Qdrant URL 和本地 BGE 权重。GitHub CI
的 stateful Job 已使用真实 PostgreSQL/Qdrant 执行 persistence/API/vector suites，
当前三个远端 Check 均为绿色。CI 仍没有：

- 真实 DeepSeek/Claude provider E2E；
- 真实 BGE-M3 权重门禁；
- Task Worker 进程启动 smoke；
- 杀死 Worker 后自动接管测试；
- Task/Multi-Agent benchmark。

## 8. 建议的增量修复顺序

### PR-A：Task 工作流终态语义

- 增加明确的成功/失败 disposition；
- revise 时调用 `begin_revision()`；
- 精确断言修改次数；
- critic 拒绝且预算耗尽必须写 `failed`。

### PR-B：Task 提交幂等与租户隔离

- 移除 thread_id 对正常 retry 的破坏；
- 冲突路径使用已存 thread/event stream；
- 新迁移增加 tenant-aware unique key；
- 增加同租户 retry、跨租户同 owner/key 和并发 submit 测试。

### PR-C：单 Worker 产品纵向切片

- Task Router、Task CLI；
- `agent-task-worker` 入口和 poll loop；
- Settings → SubmittedSemantics；
- Registry + Saver + Workflow + Worker composition root；
- FakeExecutor 下提交、执行、查询、timeline 的一条 E2E。

### PR-D：真实 Task Agent handlers

- `plan`/`critic` 结构化 decode；
- internal/external research tools；
- evidence Artifact 读取；
- EventSink、CancellationToken、TaskRunContext；
- Runtime 预算和 Policy 在 resume 时重新取最严格交集。

### PR-E：可靠 Task Core

- `SKIP LOCKED` claim；
- lease、heartbeat、epoch、stale reclaim；
- 专用 session advisory lock；
- FencedCheckpointer/Registry/Tool ledger；
- 确定性 clock、barrier 和 failpoint；
- 真杀 Worker 后由另一个进程完成。

### PR-F：产品化补全

- Ingestion Worker；
- Qdrant bootstrap/read alias/GC；
- Hybrid 与 Agentic Retrieval；
- HITL；
- OTel、Task benchmark、CrewAI 对比；
- Docker Compose 和最小 UI。

## 9. 简历表述边界

当前可以诚实写：

> 自研框架无关 Agent Runtime，支持受策略约束的工具调用、预算、取消和并行调度；
> 基于 FastAPI、PostgreSQL、Qdrant 和 BGE 构建具备幂等发布、ACL/revision 二次校验及
> 崩溃恢复的固定两步 RAG Chat；使用 LangGraph 和自研 PostgreSQL Checkpointer 构建
> 可持久化 Task 工作流基础组件。

当前不能写：

- 已完成通用 Chat + Task 双模式平台；
- 已实现死 Worker 自动接管；
- 已实现可靠 Multi-Agent supervisor/workers；
- 已完成 LlamaIndex/LangChain/CrewAI 三框架集成；
- 已实现生产级 Sandbox；
- 已完成 OTel/Langfuse 可观测性；
- 已完成 Docker 一键部署和 UI。

完成 PR-A 至 PR-E 后，才适合把 Task Mode 描述为“可恢复、多 Worker、具备 fencing 的
通用 Agent 工作流”。
