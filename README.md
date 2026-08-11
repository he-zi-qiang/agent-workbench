# Agent Workbench

中文 | [English](README.en.md)

Agent Workbench 是一个面向校招与作品集展示的 clean-room 通用 Agent
平台项目，目标是提供两种产品模式：

- **Chat Mode**：多轮对话、知识库问答和带权限校验的 RAG；
- **Task Mode**：可恢复的 LangGraph 工作流和可控 Multi-Agent 协作。

项目的自研 Agent Runtime 保持框架无关。LlamaIndex、LangGraph、LangChain
以及后续对比框架都通过明确的 Port/Adapter 接入，不接管核心 Tool Loop。

> **只有十分钟？** 读 [十分钟版本](docs/HIGHLIGHTS.md)：一条真实运行留下的事件流、
> 一张架构图、三个技术判断，以及实测门禁与明确边界。
> 本页往下是完整叙述，逐 PR 证据在[实施状态](docs/status.md)。

## 当前状态

**截至 2026-08-11，基线为 `main@bf31815`（PR #112），配置 schema `1.13`。**
没有做的部分不在这一节里找——它们按“拒绝／未接线／未实现／口径不实”四类
逐条记在[已知缺口](docs/known-gaps.md)，每条附仓库位置和“做完”的判据。
以下段落是到 PR #87 为止的增量叙述，保留原时点。

截至 2026-08-09，当前工作树已包含 Task/HITL/副作用账本收尾、三处围栏修复（PR #68）、
React Chat/Work 控制台（PR #69）、LlamaIndex 检索 Adapter 与路由阈值评测
（PR #72、#73）、Chat 联网搜索与工具额度语义（PR #74），以及
[ADR-025](docs/adr/0025-mcp-adapter.md) 的 MCP Optional Lab。ADR-018～023
（无接地对话形态、运行步骤透明度、外部检索、Chat 兜底分支联网、工具额度语义、
自由回答也能联网）已合入主线；MCP 增量把配置 schema 从 `1.7` 升到 `1.8`，用显式
remote-tool allowlist 保证 API 提交与 Worker 启动发现得到同一组可审计名字。
WP15 已落地前三个阶段：[ADR-028](docs/adr/0028-task-workspace.md) 的任务工作区、
[ADR-029](docs/adr/0029-ephemeral-sandbox.md) 的一次性沙箱（schema `1.8` → `1.9`），与
[ADR-027](docs/adr/0027-read-outward-write-inward.md) 的只读取用外部世界
（schema `1.9` → `1.10`，PR #83–#86）。随后
[ADR-032](docs/adr/0032-the-external-researcher-is-an-agent.md) 补上了这条线上最后一段
没接通的地方：`researcher_external` 拿到工具时真的跑 agent 循环（PR #87）。
最新实现证据和历史增量分开记录在 [实施状态](docs/status.md)；下列能力均有代码或
测试作为依据：

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
- **MCP Adapter Optional Lab：**官方 SDK v2 的 Streamable HTTP 工具在 Task Worker
  启动时冻结成普通 `ToolBinding`，再经过自研 Runtime、Tool Gateway、提交授权信封、
  `mcp:<alias>` scope、安全重放边界与事件流；只有 `writer/synthesize`
  接受动态 MCP 目录，其他 Agent 与 Chat 不会获得这些工具。默认关闭。
- **任务工作区：**一个 Task 内可变的名字压在不可变的字节上。写一个名字产生新的
  manifest，manifest 本身也是 artifact，所以"工作区的哪一版"是 checkpoint 能持有的一个
  id；节点重放看到的是它入口那一版，而不是上次没跑完的写入。只有 `writer/synthesize`
  拿到 `workspace_list/read/write`。
- **一次性沙箱 Optional Lab：**一次调用一个容器，文件进文件出，无网络、只读根、非 root、
  丢弃 capability、内存/CPU/进程数/墙钟上限。隔离是常量不是配置项——断网是重放保证成立
  的前提，不是可调的加固项。Task 侧的 `sandbox_run` 从工作区读输入、把产物写回工作区，
  沙箱进程自己不认识工作区、租户和所有者。默认关闭；无容器运行时的部署少一个能力，而不是
  起不来。
- **只读取用外部世界 Optional Lab：**`web_mcp` 的 `fetch_page` 与 `download_document` 都是
  GET、都不带 `operation_key`。取用前先过**解析后**的地址闸门：只有全局可路由的地址放行，
  其余（含组播与 `169.254.169.254`）落在 `is_global` 的补集里——黑名单要有人记得去扩，补集
  不用；重定向在适配器里逐跳过闸，而不是把目的地的选择权交给 HTTP 客户端。**DNS rebinding
  明确不在防护范围内**，关掉它要改传输层。两个工具而不是一个带模式的工具：PDF 走 HTML 抽取
  会得到一团读起来像成功的乱码。
- **哪个 server 的工具进哪个 Agent 由配置声明：**`[[mcp.servers]].audience` 说的是用途
  （`research` / `synthesis`）而不是协议，于是再加一个读取器或渲染器是改配置不是改 profile
  代码。audience **不改变授权信封**——信封是 Task 的上限，audience 是哪个 Agent 够得到它；
  profile 按**实际注册到的**工具加宽，不按配置，否则节点会去请求一个 Gateway 解析不到的工具。
- **外部研究节点在拿到工具时是一个 Agent（ADR-032）：**改动是纯加法——保留原来那次确定性
  搜索，仅当这个 Worker 真注册了 research 受众的工具时再跑一次带工具的 agent run，两半的
  证据用图自己的 fan-in reducer 合并。目录为空的部署一步不多走。它交出的必须是 JSON 证据
  条目而不是散文，因为 `synthesize` 读的是 `EvidenceBundle`；`{"items":[]}` 是允许的答案，
  解析不了则让节点失败——把"读不出"降级成"没读到"，下一个节点会在沉默上写出一份有模有样的
  报告。
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
- **三处围栏不再由被检查方满足（PR #68）：**epoch 比当前 attempt 更旧的 `intended`
  行转人工核对而不是被下一个 Worker 读成"还没做过，去做"；图节点在**领取时**拿到的
  不可变 `ExecutionLease` 下写入，而不是每次向 Registry 问最新 epoch——重读会让失去
  租约的 Worker 用顶替者的 epoch 通过账本围栏；`knowledge_search` 的 journal 记录
  **渲染给模型的**段落而不是检索到的全部，否则超出结果预算被丢掉的段落也能通过引用
  校验；
- **F 主体完成：**Qdrant 启动校验、常驻摄取 Worker 的 claim/heartbeat/fencing、
  HITL Approval、OpenTelemetry、React 控制台、Task 生命周期时间线以及本机 Compose
  演示拓扑已经实现。

这些能力仍有明确边界：

- 旧 Qdrant Point 已被 revision 栅栏阻止读取，但 replace/delete 物理清理尚未完成。
- Chat 的历史 token window/compaction 尚未实现。引用校验与 Agentic Retrieval
  已经落地：`chat.retrieval_shape` 可选 `fixed`/`agentic`（**默认仍是 `fixed`**，
  因为固定两步才可复现评测），引用只在模型点名且**被展示过**时给出。
- EventLog 能拒绝未知 schema version，但尚未实现旧版本 upcaster、poison-row
  隔离/跳过策略。
- 三臂消融的 `hybrid-rerank` 臂尚未跑：hybrid 在当前 38 题 gold set 上已打满
  1.000，rerank delta 必然为 0；要测出它得先有更难的 gold set。
- 外部搜索已接上真实 Provider（[ADR-020](docs/adr/0020-external-web-search.md)）：
  DeepSeek 服务端 `web_search`，走 Anthropic-compatible endpoint，不引入第二把
  API key。[ADR-021](docs/adr/0021-chat-web-search.md) 把它扩到 Chat 的兜底分支，
  [ADR-023](docs/adr/0023-direct-chat-reaches-the-web.md) 再扩到自由回答——两条
  路径的共同点是手里没有证据，而不是走了哪个类；`routed` 的接地分支仍然够不到它。
  两处都是模型可以不用的工具，用了网页的回答不算接地。`research.enabled` **默认关闭**，
  因为这个字段同时决定 Task 授权信封的宽度，且信封随 Task 存下、每次恢复重新施加。
- HITL Approval 已贯通 LangGraph interrupt、权威账本、版本化决定 API、授权复核与
  跨进程恢复；React Work 页面只按服务端权威记录提供决定操作。
- **框架口径已由 [ADR-017](docs/adr/0017-llamaindex-primary-rag.md) 锁定：**自研 Agent
  Runtime、LangGraph Task 控制面、LlamaIndex ingestion/retrieval、Qdrant 单次 RRF，
  并以 RAGAS 作为离线 LLM-judge 辅助。LlamaIndex 不接管 Tool Loop 或最终回答，
  应用层继续负责 ACL/revision fence 与答案发布。
- **当前实现边界：**LlamaIndex **检索**适配器已落地（`adapters/llama_index/`）：它拥有
  query embedding、Retriever 契约与 Document/Node 映射；Qdrant 仍是唯一融合方，授权与
  答案发布仍在应用层。检索契约测试按 `CandidateRetrieverPort` 参数化，两条路径在真实
  Qdrant + PostgreSQL 上跑同一套 ACL、source revision 与引用断言。
  **但它没有成为默认**：`rag.llama_index.enabled = false`。ADR-017 要求切流量以等价评测
  为前提，而那次评测**测不出来**——并列的融合分数返回次序不稳定，每个检索器与**自己**
  的不一致（9-10/38 题）都宽于两条路径之间的差异。没有证据表明 LlamaIndex 更差，也没有
  证据表明它等价；"测不出来"不是切流量的理由。
  **ingestion 仍未迁移**——`IngestionPipeline` 没有接入，LlamaIndex 的 VectorStore
  适配器明确拒绝写入，因为一条没有对照基准的第二写入路径正是 ADR-017 迁移规则要防的；
  **RAGAS runner 仍未落地**。因此能力表里 LlamaIndex 与 RAGAS **整体保持 Planned**：
  适配器存在不等于框架集成已完成。
  迁移前的自研实现保留为明确命名的 `ReferenceVectorIndexRetriever`，是当前默认路径，
  同时充当迁移基准。
- **已知的可复现性缺口：**并列检索分数没有确定性次序，因此同一个问题两次提问可能得到
  不同的上下文与不同的引用。这既让 §15 要求的"固定数据集和可展示指标"打折扣，也是上面
  那次等价评测无法给出结论的直接原因。修法是在适配器边界给并列项定序，属于独立变更。
- OpenTelemetry 的 trace/metrics 已落地（Port + OTLP Adapter，核心层不导入 SDK）。
  Langfuse、动态 Multi-Agent supervisor、生产身份认证与生产部署仍未完成。
- MCP 第一版不支持 stdio、OAuth、热更新、MCP Tasks、Tool 级动态审批、transport body
  硬上限或跨 Worker 进程的全局锁；
  `retryable_effects=false` 的 server 不进入可调用路径。它是协议 Adapter，不是第二套
  Agent executor，也不改变 PostgreSQL 的恢复事实源。
- React 控制台已实现 Chat / Work 两条主流程，以及 Knowledge / Approvals /
  Evaluation / System 辅助页；其前端协议、安全发布语义和响应式设计见
  [前端设计基线](docs/frontend-design.md)。
- Task 产出的 **`.docx` 可以在控制台里直接读**：服务端用 `python-docx`（Word MCP
  的渲染依赖）把正文与表格提取成 Markdown（`GET /v1/artifacts/{id}/preview`，
  仅 docx，其它类型 415），阅读列内联渲染，旁边是下载键。**它是文字预览而不是排版
  渲染**——不含样式、图片与页眉页脚，界面上也这么写，并报告文档里有几张表格被略过。
- Chat 的每一轮会列出**这一轮被授权的工具**，调用过的高亮；工具调用一行显示
  "工具名 + 这次调的是什么"（如 `web_search · 北京今天天气`），失败显示错误消息
  而不是错误码——`provider_unavailable` 同时是"没配 provider"和"找到 5 页一页也读
  不了"，只显示码会把网络故障读成缺功能。
- 当前 Compose 只用于本机演示，不能作为生产部署或生产级多 Worker 证明。
- 前端增量当时的门禁为：Ruff format/lint 通过、Pyright `0 errors`、无外部服务
  `1264 passed / 568 skipped`；前端 ESLint/严格 TypeScript/production build 通过，
  Vitest `45 passed`、Playwright 桌面/移动端 `2 passed`。该次工作树在真实
  PostgreSQL + Qdrant 下为 `1821 passed / 11 skipped`（11 项需要 BGE 权重）；
  两组数字来自不同环境，只能分别引用，不能相加。
- 沙箱不联网、不支持跨调用状态、不做 GPU，也不保证逐字节确定性重放（脚本自己可以用
  `time.time()`/`random`）。只读取用不做填表、点击、任何 POST，也不驱动桌面软件界面；
  JS 渲染的页面与截图需要浏览器内核，按 ADR-027 §3.5 明确不做——**SPA 页面取不到正文是
  已知边界不是 bug**。WP15 的阶段四（`workspace_edit`、`workspace_grep`，
  [ADR-030](docs/adr/0030-working-nodes-are-governed-by-cost.md)）与阶段五
  （第二张图 `v2_general`，[ADR-031](docs/adr/0031-a-second-graph.md)）都已落地。
- 一条实测出来的成本边界，而且撞过两次：**会干活的节点装不进为"读输入然后回答"定的
  默认上限**。读网页的节点一页正文 20–50 KB、两次读约 28000 tokens；v2 的 `work`
  节点更甚——它一次调用要读工具、写工作区、再渲染文档。默认
  `multi_agent.max_tokens_per_agent_invocation=16000` 让 run 停在半句 JSON 上，
  默认 `runtime.max_steps=12` 让它停在渲染之前，两种都是**工具全部成功、节点仍然
  失败**。默认值不动，只有 `config.web-local.toml`（120000）与
  `config.word-local.toml`（120000 + `max_steps=40`）抬高，注释里带实测依据。
- 当前门禁（`main@0ee1700` 实测，2026-08-11）：对着真实 PostgreSQL 5433 + Qdrant 6333
  为 **`2629 passed / 11 skipped`**（11 项需要 `embedding` extra 与本地 BGE 权重）；
  同一工作树在本机无外部服务时是 `1996 passed / 644 skipped`。前端 Vitest
  **`114 passed`**，tsc 严格模式与 ESLint 均通过。Ruff format/lint 通过（441 files），
  Pyright strict `0 errors / 0 warnings`。两组数字来自不同环境，只能分别引用，
  不能相加。
- **每个 PR 都有一组真实服务证据**：CI 的 `Migrations, PostgreSQL and Qdrant-backed stores`
  job 先 `alembic upgrade head`，再对着真实 PostgreSQL 16 与 Qdrant 跑
  `tests/contracts tests/persistence tests/api tests/vector`，共 920 项、2 项环境跳过
  （其中 1 项需要 `embedding` extra 与本地 BGE 权重，CI 不装该 extra）。它不覆盖
  `tests/e2e`、Task Worker 端到端与需要模型 Provider 的路径。三组数字来自三种环境，
  只能分别引用，不能相加。
  这个 job **不是每次都全绿**：`test_the_hybrid_and_dense_paths_agree_on_the_tie_break`
  会偶发失败，而它失败的原因就是下面那条"已知的可复现性缺口"——并列分数没有确定性次序。
  如实记下来，是因为把它写成一个稳定的通过数字，既高估了这个 job，也掩盖了一条真实缺陷。

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

## 许可证

本仓库以 [Apache License 2.0](LICENSE) 发布。使用或分发时请保留
[NOTICE.md](NOTICE.md)——Apache-2.0 第 4(d) 条要求随附它。

依赖各自的许可证不受本仓库许可证影响，判定规则见
[docs/compliance.md](docs/compliance.md)。

clean-room 边界见 [NOTICE.md](NOTICE.md) 和
[docs/compliance.md](docs/compliance.md)。

当前实现证据记录在 [docs/status.md](docs/status.md)。
