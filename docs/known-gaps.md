# 已知缺口

截至 **2026-08-12**，`main@e3281b4`（PR #113）；配置 schema `1.14`，Alembic
迁移 25 个（head `0025_agent_invocation_count`）。

## 这份文档解决什么问题

[实施状态](./status.md) 记录**做成了什么**，逐 PR 累积，五千余行。
[架构基线](./architecture-baseline.md) 第 17 节记录能力**处在哪一级**
（Planned / Implemented / Tested / Demonstrated）。两份都在，但它们回答不了
一个读者最常问的问题：**没做的那些，为什么没做，以及做完了算什么样。**

一份只列"未实现"的清单没有用——它读起来像待办，而待办不区分"还没轮到"、
"故意不做"和"以为做了其实没有"。这三种缺口的处理方式完全不同：第一种排期，
第二种要在代码里留下拒绝的痕迹，第三种是缺陷，必须立刻修。

所以本文档给每一条缺口标注**四种分类之一**，并要求每条都附上仓库里的位置。
没有位置的条目不许写进来：那是印象，不是缺口。

### 四种分类

| 分类 | 含义 | 处理方式 |
|---|---|---|
| **拒绝** | 不做是设计决定，代码里有显式拒绝并写明理由 | 不进路线图。若要改，先写 ADR |
| **未接线** | 能力建成并测过，但生产路径不走它，或默认关着 | 排期，且要先补"凭什么可以打开"的证据 |
| **未实现** | 仓库里没有对应代码 | 排期 |
| **口径不实** | 配置或文档声称的，与仓库事实不符 | **缺陷**，立刻修，不排期 |

"口径不实"单独成一类，是因为它和其余三类的危害不同。未实现的东西读者看得见；
声称已实现的未实现的东西，读者看不见——它把一个缺口伪装成一个能力。

---

## A. RAG 与评测

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| A-01 | LlamaIndex 检索 Adapter 默认关闭 | 未接线 |
| A-02 | LlamaIndex ingestion 未接入 | **拒绝** |
| A-03 | ADR-033 之后未重跑等价评测 | 未实现 |
| A-04 | RAGAS 全链缺失 | 未实现 |
| A-05 | `ragas_enabled = true` 虚假启用 | **口径不实（本次已修）** |
| A-06 | 评测只判检索，不判答案 | 未实现 |

### A-01 LlamaIndex 检索 Adapter 建成，但默认关闭

**证据**：[config.default.toml](../config/config.default.toml) `[rag.llama_index]`
段 `enabled = false`，注释写明关闭理由；Adapter 代码与契约测试在
`src/agent_workbench/adapters/llama_index/`。

**为什么**：[ADR-017](./adr/0017-llamaindex-primary-rag.md) 第 3 步要求，切换前
必须有一个**能把两条检索路径区分开**的度量。这不是流程洁癖：两条路径都能返回
"看起来对"的结果，缺的是判定它们是否等价的手段。适配器存在不等于框架集成完成。

**做完的判据**：同一份 52 题 gold set、同一次运行、同一份代码下，Reference 与
LlamaIndex 两条路径各出一份报告，差异落在可解释范围内（见 A-03）。

### A-02 LlamaIndex ingestion 未接入，`add` / `delete` 显式拒绝

**证据**：[vector_store.py:180](../src/agent_workbench/adapters/llama_index/vector_store.py:180)
`add()` 抛 `NotImplementedError`，理由是"a second write path into the same
collection is what ADR-017's migration rules forbid"；`delete()` 同理。

**这是拒绝，不是遗漏。** 同一个 Qdrant collection 上开第二条写入路径，意味着两套
chunk 版本、两套 parser 版本可以并存而无人发现。摄取仍然只有一条路径。

**做完的判据**：不适用。要改，先改 ADR-017 的迁移规则。

### A-03 ADR-033 修复排序后，未用同一份 gold set 重跑等价评测

**证据**：[evals/rag/gold.jsonl](../evals/rag/gold.jsonl) 52 题。
`evals/rag/reports/` 下 `dense-reference.json` 与 `hybrid-reference.json` 的
时间戳是 2026-08-10 20:05，而 `hybrid-llama_index.json` 是 2026-08-03 07:20、
`dense-llama_index.json` 是 2026-08-10 12:33。

**为什么这是缺口**：[ADR-033](./adr/0033-fusion-ranks-are-ours.md) 改了融合排序。
LlamaIndex 侧的两份报告产生于那次修复**之前或之间**，因此现有的四份报告不构成
一次等价比较——它们不是同一份代码下的同一次测量。runner 本身要求报告记录
index identity 与 gold set digest 正是为了让这种不可比性能被看出来。

**做完的判据**：一次运行内产出 Reference / LlamaIndex 两侧四份报告，digest 一致，
写入 `evals/rag/reports/` 并在 [status.md](./status.md) 记录。整轮约 30–70 分钟。

### A-04 RAGAS 依赖、runner、judge calibration、报告均不存在

**证据**：`pyproject.toml` 的 `dependencies`、`optional-dependencies` 与
`dependency-groups` 中无 `ragas`；`src/agent_workbench/evaluation/` 下只有
`metrics.py` 与 `runner.py` 两个文件。

**做完的判据**：依赖入 optional extra（沿用 `embedding` extra 的分层约定——CI 不装，
真模型证据只来自本机）、离线 runner、judge 校准集与一份可复现报告，四件齐了才能把
A-05 的 flag 重新打开。

### A-05 `ragas_enabled = true` —— 口径不实，**本次已修**

**修复前**：[config.default.toml](../config/config.default.toml) `[evaluation]`
段写着 `ragas_enabled = true`，而 A-04 所列四件东西一件都不存在。配置是读者
判断能力边界的一手材料，这一行让它说了假话。

**修复**：值改为 `false`，且
[settings.py](../src/agent_workbench/bootstrap/settings.py) 里的类型从
`bool` 收窄为 `Literal[False]`——与相邻的 `ragas_offline_only: Literal[True]`、
`online_judge_in_ci: Literal[False]` 同一体例。收窄类型而不是只改默认值，是因为
`bool` 允许任何 overlay 把它设回 `true`，那样这条缺陷会以另一个文件的形式复发；
`Literal[False]` 让它**构造期就失败**。

**测试**：`tests/config/test_settings.py::test_ragas_cannot_be_enabled_while_no_runner_exists`。
三条断言，第三条才是有牙的那条——它断言 `pyproject.toml` 里**没有** ragas 依赖。
把 flag 锁死只证明注解生效；把锁和"依赖不存在"绑在一起，才能在有人装了 RAGAS 却
忘记重开 flag 的那一天失败。两个对照组实测：改回 `true` 红，加一条
`ragas>=0.2` 依赖也红。

### A-06 现有评测只判检索，不判最终答案

**证据**：[metrics.py:136](../src/agent_workbench/evaluation/metrics.py:136)
`RETRIEVAL_METRICS` 恰好五项——`recall_at_1`、`recall_at_3`、
`full_coverage_at_3`、`mrr`、`retrieval_latency_ms`。
[runner.py:13](../src/agent_workbench/evaluation/runner.py:13) 的模块 docstring
自己写明："Nothing here judges an answer."

**这一条是有意的分离**，但缺口是真的：忠实度与引用准确性需要模型在环，属于另一个
runner 和另一套证据。混进来会让检索回归和生成回归长得一样。

---

## B. Reliable Core

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| B-01 | LISTEN/NOTIFY 只有发送端 | 未接线 |
| B-02 | Event-loop lag watchdog | 未实现 |
| B-03 | 七点故障矩阵只覆盖四点 | 未实现 |
| B-04 | 无真杀 OS 进程的恢复测试 | 未实现 |
| B-05 | 事件 schema upcaster 与坏行隔离 | 未实现 |

### B-01 LISTEN/NOTIFY 只有发送端，两条消费路径都在轮询

**证据**：发送端在
[notifications.py](../src/agent_workbench/adapters/persistence/notifications.py)
（`notify_task_ready`），调用点如
[approvals.py:313](../src/agent_workbench/adapters/persistence/approvals.py:313)。
配置侧一整套已就位：`database.listen_dsn`、`listen_pool_mode`、
`listener_connections_per_process`、`listen_connection_scope`、
`listener_healthcheck_seconds`、`coordination.wakeup_backend =
"postgres_listen_notify"`、`notify_payload_mode = "cursor_only"`。
消费端不存在——[events.py:122](../src/agent_workbench/apps/api/routes/events.py:122)
的 docstring 自陈："a LISTEN/NOTIFY wakeup backend, which nothing consumes yet:
until it does, this is the honest behaviour rather than a claim about it."

**当前行为**：Task Worker 与 SSE 都靠轮询，延迟由轮询间隔决定。**功能正确，延迟
不必要地高。** 通知只是唤醒，载荷是 cursor 而非事实，所以接上消费端不改变正确性
论证——听漏了仍然由 cursor catch-up 兜住。

**做完的判据**：专用 LISTEN 连接 + reconnect/catch-up（实施计划 WP09-05），并有
一条"监听端完全丢通知，cursor catch-up 仍完整"的测试对着真 PostgreSQL 跑。

### B-02 Event-loop lag watchdog 未实现

**证据**：全仓无 `watchdog` / `loop_lag` 符号。

**做完的判据**：采样事件循环滞后并作为指标导出，超阈值时记录而非杀进程。

### B-03 基线要求七个故障窗口，只覆盖四个

**证据**：[fault_injector.py:12](../src/agent_workbench/ports/fault_injector.py:12)
的 `FailpointName` 恰好四个值：

| Failpoint | 必须证明 |
|---|---|
| `after_claim_commit_before_advisory_lock` | 假 running 可被回收 |
| `after_node_before_checkpoint` | 旧 Worker 完成 node 后仍不能落 checkpoint |
| `inside_checkpoint_put` | checkpoint 事务内写入原子 |
| `after_graph_complete_before_registry_commit` | reconciliation 幂等完成 |

[implementation-plan.md:992](./implementation-plan.md:992) 写着"当前四个规范故障
窗口"，同一文件也写明 Reliable Core 还必须完成"完整七点故障矩阵"。缺的三类窗口
属于**审批**、**产物 ledger** 与 **Qdrant outbox**——它们的 Adapter 落地后才会
成为可配置 failpoint，在那之前未知名称必须失败关闭（这一点已经成立）。

**做完的判据**：三个新窗口进 `FailpointName`、测试 profile 与负向配置测试同步扩展。

### B-04 没有真正杀死 OS Worker 进程再恢复的测试

**证据**：现有恢复证据是在**同一个 pytest 进程内**重建 engine / worker。
`tests/` 下没有 `Popen` + `SIGKILL` 形态的 Worker 恢复用例。

**为什么这是缺口**：同进程重建能验证状态机，验不了进程级资产——连接池、
advisory lock 的会话归属、lease 在连接骤断后的实际释放时机。这三样恰好是
"Worker 被 kill -9"时最可能出问题的地方。

**做完的判据**：拉起真 Worker 子进程 → 在指定 failpoint 处 `SIGKILL` →
另一个 Worker reclaim 并跑完 → 断言无重复副作用。

### B-05 事件 schema upcaster 与坏行隔离未实现

**证据**：注意区分两件事。**Chat 轮次**层面的坏行隔离**已经有了**——
[chat_recovery.py:77](../src/agent_workbench/application/chat_recovery.py:77)
"a bad row cannot poison later candidates"，测试见
`tests/persistence/test_chat_expiration.py:325`。**事件日志**层面的 upcaster 与
隔离策略没有。

**做完的判据**：旧版本事件读出时按 schema 版本升级；无法解析的行进隔离区并计数，
不阻塞后续读取。

---

## C. Multi-Agent

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| C-01 | 调用次数上限只有配置，无持久账本 | 未接线 |
| C-02 | 跨 retry 预算、partial failure、父子取消 | 未实现 |
| C-03 | 动态 supervisor / spawn / mailbox | 未实现 |
| C-04 | CrewAI Adapter 与对比 benchmark | 未实现 |

### C-01 `max_agent_invocation_attempts_per_task` 只有配置，没有账本

**证据**：[projections.py:424](../src/agent_workbench/bootstrap/projections.py:424)
`MultiAgentConfig` 的 docstring 直接说明了这件事——"Three fields, and
deliberately not the fourth"，因为该上限要跨 retry 与 reclaim 计数，需要一个
持久的 per-Task 计数器，"projecting it here would put it one import away from
looking enforced"。

**这条注释本身就是正确做法**：宁可让配置项停在 settings 里不投影，也不要让它
在投影层出现、看起来像被执行了。

**做完的判据**：一张能跨 retry/reclaim 累计的持久计数表，投影随之补上第四个字段。

### C-02 跨 retry/reclaim 的总预算、显式 partial failure、父任务到子调用的取消

**分类**：未实现。三者与 C-01 同源——都需要一个比"单次进程内计数"更持久的账本。

### C-03 动态 supervisor、spawn、mailbox 未实现

**当前事实**：Multi-Agent 是**固定图**——v1 与 `v2_general` 两张，提交时选图并冻结。
动态编排（运行期决定拉起哪些 agent、agent 之间投递消息）不存在。

### C-04 CrewAI Adapter 与对比 benchmark 未实现

**证据**：`pyproject.toml` 无 `crewai` 依赖。

### 一条不是缺口的说明：Redis

**Redis 不存在不是缺陷。** 当前以 PostgreSQL 作为唯一事实源是一个合理选择，
并且是被显式论证过的。真正缺的是 B-01 的 LISTEN 消费端，而不是换一个中间件。

---

## D. 产品与生产能力

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| D-01 | Chat turn-scoped 附件、Task 输入 Artifact 附件 | 未实现 |
| D-02 | Chat Session 服务端列表 / 重命名 / 删除 | 未实现 |
| D-03 | 知识库重命名 / 删除 / 文档删除 / ACL UI | 未实现 |
| D-04 | Word 读取与不可变编辑 | 未实现 |
| D-05 | Langfuse、生产身份认证、S3 Artifact、远程部署 | 未实现 |
| D-06 | Chat 历史 compaction | 未接线 |

### D-01 真正的 Chat 轮次级附件与 Task 输入 Artifact 附件

**当前事实**：`routes/uploads.py` 只有 `POST ""` 与 `POST /{upload_id}/complete`
两个端点。上传能力在，但"这一轮对话带这几个文件"和"这个 Task 以这份文件为输入"
两条产品语义没有接上。

### D-02 Chat Session 的服务端列表、重命名、删除与完整历史元数据

**证据**：[chat.py](../src/agent_workbench/apps/api/routes/chat.py) 的路由只有三个：
`POST /sessions`、`POST /sessions/{session_id}/messages`、
`GET /sessions/{session_id}/messages`。没有列表、没有改名、没有删除。

### D-03 知识库重命名、删除、文档删除与共享 / ACL 管理 UI

**证据**：[knowledge_bases.py](../src/agent_workbench/apps/api/routes/knowledge_bases.py)
只有 `POST ""`、`GET ""`、`GET /{id}`、`GET /{id}/documents`。**全部是读与创建，
没有一个改与删。**

### D-04 Word 只能创建，不能读取，也没有不可变编辑

**证据**：[word_mcp/server.py:44](../src/agent_workbench/apps/word_mcp/server.py:44)
只声明了**一个**工具（`render_document`），输入契约是
[contract.py](../src/agent_workbench/apps/word_mcp/contract.py) 里那个封闭的
结构化 schema，"No path, URL, tenant, owner, or artifact field is accepted"。

**注意与已有能力区分**：`.docx` 的**服务端文本预览**已经落地
（`GET /v1/artifacts/{id}/preview`，2026-08-11）。那是**读出文字**，不是
Word 文档读取与编辑，能力表里也不得混为一谈。

### D-05 Langfuse、生产身份认证、S3 Artifact Adapter、远程部署

**当前事实**：可观测走 OTel（已落地）；Artifact 存本地文件系统；身份认证在生产
意义上不存在；部署只有本机 Compose。这四项在架构基线里一直是 Planned。

### D-06 Chat 历史 compaction

**证据**：领域侧**已经定义**——
[events.py:428](../src/agent_workbench/domain/events.py:428) 的
`ContextCompacted`（"Compaction derives a shorter context; it never edits the
record."），以及 run 状态里的 `compacting`。**但没有任何代码发射这个事件。**

这是典型的"未接线"：协议先于实现落地是对的，但在实现出现之前，事件类型的存在
不构成能力。

---

## E. 测试与发布证据

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| E-01 | Playwright 只验外壳，后端全 mock | 未实现 |
| E-02 | 缺真实 Word MCP Server 的官方 Client 闭环 | 未实现 |
| E-03 | CI 不跑 E2E / 离线评测 / Compose / 恢复矩阵 | 未实现 |
| E-04 | 首个 evidence manifest 未生成 | 未接线 |
| E-05 | 文档中的数字过时 | **口径不实** |

### E-01 Playwright 的四次执行全部 mock 后端

**证据**：[web/e2e/shell.spec.ts](../web/e2e/shell.spec.ts) 122 行，两个 `test(`；
`playwright.config.ts` 两个 project（`chromium` 与 `mobile`/iPhone 13），
所以是 **2 个用例 × 2 个 project = 4 次执行**。三处 `page.route(...)` 把
`/v1/knowledge-bases`、`/v1/knowledge-bases/kb_portfolio/documents`、`/v1/tasks`
全部拦成固定响应。

**它验的是**：外壳在桌面与移动布局下可用、知识库能进入 Chat、辅助页面可达。
**它不验的是**：真实提交 Chat、真实提交 Task、审批、下载。

### E-02 缺少对真实 Word MCP Server 的官方 Client 闭环测试

**当前事实**：renderer 与通用 MCP 各自有测试，但没有一条用官方 MCP Client
连上真实 Word MCP Server 跑完一次 `render_document` 的用例。

### E-03 CI 的覆盖边界

**证据**：[.github/workflows/ci.yml](../.github/workflows/ci.yml) 四个 job——
`frontend`、`quality`、`stateful`（真 PostgreSQL + Qdrant）、`secret-scan`。

**不跑**：完整 E2E、离线 RAG 评测、Compose 启动、进程恢复矩阵。前两项各有理由
（E2E 需要真后端起栈；离线评测需要 embedding extra，按分层约定 CI 不装），
后两项是单纯的缺口。

### E-04 首个 evidence manifest 尚未生成

**这一条要更正一个常见误解：工具已经存在。**
[bootstrap/evidence.py](../src/agent_workbench/bootstrap/evidence.py) 实现了
`write` 与 `verify` 两个子命令，有配套测试
[test_evidence_manifest.py](../tests/bootstrap/test_evidence_manifest.py)，
默认输出 `artifacts/evidence/<gate>/manifest.json`。它区分**派生事实**
（配置 revision、policy 指纹、图版本、模型与索引身份、commit——全部自动取，
"nothing here can be wishful"）与**附件**（按 SHA-256 与大小记录），并且有两条
拒绝：附件不存在就不写；工作树脏就不写，除非显式 `--allow-dirty`。

**缺的只是从没跑过一次。** 于是仓库里所有数字仍以散文形式存在——状态文档里的
测试计数、README 里的评测数字——每一条都为真，每一条都不可核查。

### E-05 文档中的数字过时 —— 口径不实

**证据**：本文档合入时已修掉一半——[docs/README.md](./README.md)、
[配置管理契约](./configuration.md) 与本文件的锚点都已推到 `main@e3281b4`
（PR #113）、schema `1.14`、Alembic head `0025_agent_invocation_count`，
测试计数改用 CI 实测值（PR #116 与 PR #113 两次独立运行逐位相同：确定性
`2050 / 719`、真实服务 `1012 / 2`）。

**仍然落后的**：[架构基线](./architecture-baseline.md) 第 17 节。该节已经不再
钉具体 commit（它自己写明了理由：hash 一往前走就成了考古），但其中的门禁表
仍是更早一次本机运行的数字。

**为什么这一条不会"修完就消失"**：数字过时是持续现象，不是一次性缺陷。真正
消除它的是 E-04 的 evidence manifest——把数字从散文变成可校验的引用，让"过时"
在 CI 里失败，而不是靠人记得来改。在那之前，这一条每次基线变动都会复发一遍。

**为什么归入口径不实而不是"文档没更新"**：这些数字是读者用来判断"我读的这份
文档描述的是不是我手上这份代码"的锚点。锚点错了，整份文档的可信度都要打折。

---

## 优先级建议

按"单位工作量能消除多少不可核查性"排序，而不是按功能大小。

1. **E-04 生成首个 evidence manifest**。工具已在，成本最低，收益是把此后所有
   数字从散文变成可校验的引用。
2. **E-05 刷新过时数字**。机械工作，但它决定读者是否信任其余文档。
3. **A-03 重跑等价评测**。它同时是 A-01 能否打开的前置条件。约 30–70 分钟机器时间。
4. **B-01 LISTEN 消费端**。边界清楚，可对真 PostgreSQL 验证，且不改变正确性论证。
5. **C-01 调用账本**。它是 C-02 三项的共同前置。
6. **B-04 真杀进程的恢复测试**。它验的是现有测试结构性验不到的那部分。

其余条目（D 组大部分、C-03、C-04、B-03）需要新的 Adapter 或新的产品决策，
不适合在证据链补齐之前动。

---

## 维护规则

- 一条缺口关闭时，**从本文档删除**并在 [status.md](./status.md) 记录，不要在这里
  留"已完成"的条目——那会让本文档退化成第二份状态文档。
- 新增缺口必须带仓库位置。没有位置的条目不许写进来。
- "口径不实"类不排期。发现即修，修完在本次提交里连带更正本文档。
