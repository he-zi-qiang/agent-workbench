# 已知缺口

截至 **2026-08-31**，配置 schema `1.19`，Alembic 迁移 32 个
（head `0032_events_stream_run_sequence`）——这三项本次重测。

**上一版这三个数又过期了，而且过期的方式和上上版一模一样。** 它写着"截至 2026-08-25，
schema `1.18`，迁移 31 个（head `0031_project_root_path`）"，并且在同一句里指出上上版的
`1.17` 是 E-05 那种复发——也就是说，**一句自称"本次重测"的话，第二次把自己变成了它正在
批评的那个例子**。这不再是偶然：它是 E-05 条目里写的那个结论的第三份证据——数字过期是
**持续现象**，靠人记得刷新治不好。真正的修法是 E-04（让过期在 CI 里失败），那也是本文档
[优先级建议](#优先级建议)把 E-04 排在第 1 位的全部理由。

本文档各条的**代码位置**仍核对于 `main@921dda5`，未随之重核——分开写，是因为
合成一句会让「今天量过的数字」替一份没重核的清单背书。
门禁数字不在本文档维护，见 [十分钟版本的门禁与规模一节](./HIGHLIGHTS.md#2-门禁与规模)。

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
| A-07 | 候选漏斗无人读 | ~~口径不实~~ **部分关闭**（4/5 已接线，ADR-097）|

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

### A-07 `[rag.retrieval]` 的候选漏斗被校验，然后没有任何人读它 —— 口径不实

**分类**：口径不实（2026-08-31 本次扫描新登记）。

**证据**。`[rag.retrieval]` 声明了一条五级候选漏斗，配置里写着具体的数：

```toml
[rag.retrieval]
dense_top_k = 40
sparse_top_k = 40
fused_top_k = 40
rerank_top_k = 8
answer_context_k = 8
```

[settings.py:920](../src/agent_workbench/bootstrap/settings.py:920) 的
`validate_candidate_funnel` 在启动时校验它们**彼此**单调
（`rerank_top_k <= fused_top_k <= dense_top_k + sparse_top_k`），
[test_settings.py:245](../tests/config/test_settings.py:245) 还钉着这条校验。

**五个数全部没有读者，一个都没有。** `dense_top_k` / `sparse_top_k` / `fused_top_k` /
`rerank_top_k` 在 `src/` 里除定义与互相校验之外零命中。`answer_context_k` 看起来是例外
——它被投影进 [`RetrievalConfig`](../src/agent_workbench/bootstrap/projections.py:327)
（`:909` 与 `:1209`）——**但那只是投影，不是消费**：全仓 `answer_context_k` 共 6 处命中，
全在 `bootstrap/` 内（1 处定义、2 处校验、2 处投影、1 处字段声明），
`RetrievalConfig.answer_context_k` **没有任何读者**。同一个 dataclass 里的
`chunk_size_tokens` 有 1 个消费者、`llama_index_enabled` 有 2 个，所以这不是投影层的
常态，是这一个字段的例外。
`RetrievalService` 的两处构造点——[dependencies.py:918](../src/agent_workbench/apps/api/dependencies.py:918)
与 [composition.py:611](../src/agent_workbench/apps/task_worker/composition.py:611)——
传进去的是 `candidate_retriever` / `documents` / `telemetry` / `reranker` /
`rerank_timeout_seconds`，**一个 top_k 都没有**。实际生效的是
[retrieval.py:161](../src/agent_workbench/application/retrieval.py:161)：
`limit = request.top_k * self.candidate_multiplier`，两个值都来自 dataclass 默认值
（`top_k = 8`、`DEFAULT_CANDIDATE_MULTIPLIER`），不来自配置。

**为什么它比"一个没人读的字段"更糟**：[配置说明](./configuration.md) §8 **此前**把这四个数
写成了**请求级覆盖的系统上限**（本次已改，见下）——"请求只允许在系统上限以内下调：…… dense/sparse/fused/rerank
`top_k`"。这句话描述的是一道闸，而这道闸不存在：请求里的 `top_k` 受约束于两个**硬编码
字面量**——[chat.py:105](../src/agent_workbench/apps/api/routes/chat.py:105) 的
`le=50`，与 [knowledge_search.py:49](../src/agent_workbench/adapters/tools/knowledge_search.py:49)
的 `MAX_TOP_K = 20`——而不是配置里的 `rerank_top_k = 8`。**把 `rerank_top_k` 调到 1，
一个请求照样可以要 50。** 这正是 F-26 的形状（读起来像保证，`src/` 里没有读者），区别
在于 F-26 那个字段是单值 `Literal`、改不动，而这四个数看起来**就是给人调的**。

**2026-08-31 处置：接线，见 [ADR-097](./adr/0097-a-funnel-nobody-reads-is-not-a-funnel.md)。**
五个数里**四个已经有读者**：两臂上限进 `ReferenceVectorIndexRetriever`，
`fused_top_k` 成为问索引要多少的那个数，`rerank_top_k` 成为请求可要的**真实上限**——
`docs/configuration.md` §8 那句话第一次被执行。
[tests/application/test_candidate_funnel.py](../tests/application/test_candidate_funnel.py)
八条钉住它，每条正向都配一条对照组（未配置时仍走旧的乘数与不夹取）。

**为什么不关**：`answer_context_k` 仍然没有读者。它是**默认值**而不是上限——决定
"没指定时给多少"的是 API 的 `Field(default=8)` 与 `RetrievalRequest.top_k = 8`，
都在请求构造的边界上，不在检索服务里（ADR-097 §4.3）。

**两件必须一起记住的代价**：

1. **候选池不再随请求伸缩，两个方向都变。** 默认请求（`top_k = 8`）是 32 → 40；
   但 `top_k = 3` 是 12 → 40（变宽），`top_k = 50` 是 200 → 40（**大幅变窄**）。
   说成"变宽了一点"会漏掉一半。这**改变了检索结果**，而本次落地的机器上
   `embedding` extra 未装，52 题 gold set 跑不起来。**在 A-03 重跑之前，不要把
   ADR-097 当作"检索质量已确认不变"的依据**——已有的测试证明的是机制，不是效果。
2. **`top_k > rerank_top_k` 的请求现在会被夹小。** 这是本次唯一一处收窄用户可见行为的
   地方，也正是 §8 一直声称的行为。

**剩下的判据（两条）**：

1. `answer_context_k` 要么有一条从配置到请求默认值的路径并有测试，要么从 schema 里删掉。
2. **接线带出的一处新的同形不诚实**：`knowledge_search` 的 `INPUT_SCHEMA` 向模型声明
   `top_k` 上限是 `MAX_TOP_K = 20`，而出厂 `rerank_top_k = 8`——模型可以合法地要 20、
   拿回 8，且没有任何地方告诉它为什么。修法不止一种且都不小（见
   [ADR-097](./adr/0097-a-funnel-nobody-reads-is-not-a-funnel.md) §4.2），所以登记而不顺手改。

---

## B. Reliable Core

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| B-01 | SSE 回放仍在轮询（Task Worker 那半已接上） | 未接线 |
| B-02 | Watchdog 只有 warn 一半，且未装进 Task Worker | 未实现 |
| B-03 | 七点故障矩阵只覆盖四点 | 未实现 |
| B-05 | 生产 upcaster 注册表为空，Chat 侧不披露隔离 | 未接线 |
| B-06 | 失败标着 `retryable` 却没有任何重试路径 | **已关闭** |
| B-07 | tool 参数读不出来时，说不出是被截断还是真的坏 | 未实现 |
| B-08 | 一个 MCP 服务器死掉，曾杀死整个 Worker（触发**本次已修**）；回收仍系于 Worker 存活 | 未实现 |

> 编号一经退休不再复用。B-04（无真杀 OS 进程的恢复测试）已于 2026-08-11 关闭，
> 按本文档维护规则从正文删除，落地记录在 [status.md](./status.md)：
> [`tests/e2e/test_worker_process_crash_recovery.py`](../tests/e2e/test_worker_process_crash_recovery.py)
> 用 `subprocess` 起独立 Worker、等它确实进入执行中再 `SIGKILL`（不是 SIGTERM——
> 优雅关闭证明不了任何事），由第二个进程接手，并断言被杀进程的返回码确实是 `-9`；
> 带不杀进程的对照组。

### B-01 SSE 回放仍在轮询

**这一条已经关闭了一半。** Task Worker 的消费端**已经落地**：
[notifications.py:86](../src/agent_workbench/adapters/persistence/notifications.py:86)
的 `TaskReadyListener` 持有一条专用会话执行 `LISTEN task_ready`，空队列时的等待
可以被一次唤醒提前打断，**轮询周期保留为下限**。正确性不依赖通知到达——有一条
对照组测试把通知全部丢掉，任务照样被领取；断线退回纯轮询而不是卡住。

实现过程中引入过一个真缺陷并已修掉，记在这里因为它值得被记住：asyncpg 对优雅
`close()` 也会触发 termination 回调、而且晚一个 tick，于是"断线→重连"会拆掉刚建好
的健康连接，每 5 秒一次、永不停止，且健康检查从此再不运行。修法是在回调里比对
会话身份。回归测试钉住了它：移掉那两行，两秒内会冒出 49 条 session。

**仍然缺的是 SSE 那一半**：[events.py:204](../src/agent_workbench/apps/api/routes/events.py:204)
的 docstring 仍自陈 "a LISTEN/NOTIFY wakeup backend, which nothing consumes yet"，
该路径继续靠轮询，延迟由轮询间隔决定。**功能正确，延迟不必要地高。**

**做完的判据**：SSE 回放路径也由 LISTEN 唤醒，且有一条"监听端完全丢通知，cursor
catch-up 仍完整"的测试对着真 PostgreSQL 跑。

### B-02 Watchdog 只做了 warn 一半，且没装进 Task Worker

**这一条已经关闭了一半。** `EventLoopLagWatchdog` 已实现
（[event_loop_lag.py](../src/agent_workbench/adapters/telemetry/event_loop_lag.py)，
测试 [test_event_loop_lag.py](../tests/adapters/test_event_loop_lag.py)），并已装进
API 进程（[apps/api/main.py:160](../src/agent_workbench/apps/api/main.py:160)）：
周期性量测事件循环滞后，超阈值上报指标并打一条**带实测数值**的日志。

**仍然缺的是两件事**：（1）实施计划要求的 **abort 半**——标记 unhealthy、停止 claim、
取消进行中的 run——未实现，超阈值只会 warn；（2）**没有装到 Task Worker**，
而 Task Worker 恰是长耗时同步调用最可能堵住事件循环的地方。

**做完的判据**：Task Worker 进程内同样启动 watchdog；超阈值持续到约定时长后进入
unhealthy 并停止 claim，且有一条"滞后消失后恢复 claim"的对照组测试。

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

### B-05 生产 upcaster 注册表为空，且 Chat 侧不披露被隔离的位点

**机制已经落地。**
[event_log.py](../src/agent_workbench/adapters/persistence/event_log.py) 的
`EventUpcasterRegistry` 按 `(event_type, from_version)` 注册**单步**升级，链自己
一版一版往上走，并在每轮重读 `event_type`（所以事件改过名也接得上）；缺一步就停在
洞前、保持原来那条拒绝路径。`read_isolating()` 让一条解不出来的行不再挡死整条流的
回放，**且跳过是可见的**——SSE 发一个独立的 `stream.quarantined` 帧，Task timeline
返回被跳过的序号。两处调用方都已切过去。

**仍然缺的是两件事**：

1. **生产注册表还是空的**——[event_log.py:166](../src/agent_workbench/adapters/persistence/event_log.py:166)
   的 `DEFAULT_EVENT_UPCASTERS = EventUpcasterRegistry()` 不含任何条目。机制有了，
   还没有真实的历史版本要升。这本身不是缺陷，但它意味着**升级链从未在真实数据上
   走过一次**。
2. **界面上 Chat 那一半仍然沉默**——Work 的任务时间线已经把每个没能交付的位点锚定
   在它确实收到的前后两条事件之间（"#2：在「工具调用已开始：external_search」与
   「任务成功完成」之间"），措辞是"这些事件仍在日志里，只是这次没能解码"而不是"丢了"。
   Chat 侧的 `stream.quarantined` 帧**只被用来推游标，界面上不显示**
   （见 `web/src/features/chat/sessionStream.test.ts`）。

**做完的判据**：（1）第一条真实 upcaster 进 `DEFAULT_EVENT_UPCASTERS`，并有一条
对着真实旧版本行的升级测试；（2）Chat 界面像 Work 时间线那样披露被隔离的位点，
带"没能解码"而非"丢了"的措辞。

### B-06 失败标着 `retryable` 却没有任何重试路径 —— **已关闭**（[ADR-059](./adr/0059-a-retryable-failure-is-released-not-settled.md)）

**证据**：重试机制**存在**——[task.py:165-167](../src/agent_workbench/workers/task.py:165)
的 `max_attempts = 5`、`retry_base_seconds`、`retry_max_seconds`，但它们只喂给
`registry.reclaim_expired(...)`，也就是**只覆盖租约过期**（Worker 崩了没续租）。
一个**执行失败**的 Task 不会因为 `retryable=True` 被重新排队。

在 Task 这条路径上，`ErrorInfo.retryable` 的唯一读取点是
[task.py:102](../src/agent_workbench/workers/task.py:102)，而它把这个布尔量
**拼进给人看的字符串**，没有任何控制流读它。

**观测**（2026-08-13，本地 demo profile 连真实 provider）：同一条 Task 连续三次
失败于 provider 侧的偶发网络错——`RemoteProtocolError`、`ConnectError`——三次都
带着 `retryable: true`，三次都直接终结，没有一次重试。代理在同一时间段实测
6/6 稳定在 0.8s，所以这不是网络不通，是抖动，而抖动正是 `retryable` 这个词
存在的理由。

**为什么**：目前没写。区分"该重试"和"重试了"需要先定清楚幂等边界——一个已经
调过外部工具、写过工作区的 Task 重跑一遍不等价于没跑过，而 Task 的重试单位
如果是整张图，就会把已完成节点的副作用做第二遍。

**做完的判据**：`retryable=True` 的失败按退避重新排队，重试次数进 Task 状态并
在界面上可见；带一条对照组证明 `retryable=False` 的失败**不**重试；并且写清楚
重试的单位是整张图还是失败节点，以及副作用如何不被做第二遍。

**关闭记录（2026-08-16，ADR-059）**：判据逐条兑现——运行分类为可重试的执行
失败经 `release_for_retry` 按 reclaim 的退避公式重新排队，上界是既有的
`coordination.max_attempts`（attempt_count 与租约重试共用同一个计数，界面上
「已调用智能体 N 次」照常累计）；对照组在
`tests/workers/test_task_worker_retry.py`：`retryable=False`、未分类异常、图内
主动失败三类都不重试。重试单位是整张图的一次重新认领——reconcile 从
checkpoint 的位置续跑，已完成节点不重做，幂等性由既有的 epoch 栅栏与幂等台账
承担，没有加新机制。

### B-07 tool 参数读不出来时，运行时说不出是被截断还是真的坏

**证据**：[deepseek.py](../src/agent_workbench/adapters/models/deepseek.py) 的流结束处，
`_completed_tool_calls` 排在 `finish` 已经确定**之后**。provider 报 `length` 时
`_map_finish_reason` 把它映射成 `max_tokens` 且 `failure` 为 `None`，于是控制流照常
往下走，用一段被截断的 JSON 去 `json.loads`，失败后报的是
`the provider sent unparsable arguments for <tool>`。也就是说"模型话没说完"和
"provider 送来一段坏 JSON"这两件事，在**同一句话**里收场，而它们该做的事不是一件：
前者要调上限或让模型少写，后者要重试或换 provider。

**观测**（2026-08-13，本机 console profile，真实 provider）：两条 Task 死在
`synthesize`，都是 `provider_error: the provider sent unparsable arguments for
mcp_word_render_document`——`task_d559ce35…`（04:40）与 `task_6b6cabe3…`（07:28）。
后者的数据是齐的：`output_tokens` 942、`tool_calls` 0、模型正文写完了一段中文说明才
开始发工具参数。**这一批没能判定**它究竟是不是截断：事件流不记录 provider 自报的
finish reason，也不记录那段没解开的参数文本。同一条 objective 随后重跑两次都没复现，
所以它是间歇的，不是每次必现。

**为什么**：不是漏了，是这层的重试契约挡着。`stream` 的文档写明"只有在任何事件发出
**之前**发生的失败才可重试"，而这里正文已经流出去了，重试会让调用方看见重复的文本。
要么把这条契约改掉（那要先想清楚重复文本谁来吞），要么把这次失败往上交给一个知道
怎么重跑一个节点的层——也就是 B-06 那件事。

**做完的判据**：`finish` 是 `max_tokens` 而参数没解开时，报的是"模型在输出上限上把话
说了一半"而不是"provider 送来坏参数"，并带一条对照组证明真坏的 JSON 仍然报后者；
事件流里留下足以判定的那一位（provider 自报的 finish reason）。

### B-08 一个 MCP 服务器死掉，曾杀死整个 Worker —— 触发已修，暴露仍在

**观测**（2026-08-16 晚，demo profile，本机实测；`var/` 下当时的 demo-worker 日志
已滚动，关键片段按现场记录重建）：word/web/sandbox 三个 MCP 服务器
（8765/8766/8767）之一进程死亡后，节点内的 MCP 调用抛 httpcore
`ConnectError: All connection attempts failed`；mcp 库 streamable HTTP 传输的
清理路径随即抛 `RuntimeError: Attempted to exit cancel scope in a different
task than it was entered in`（anyio cancel scope 跨任务退出缺陷），两者连同
scope 自己的 `CancelledError` 合成一个 `BaseExceptionGroup` 冒出。要点在最后
那片叶子：带 `CancelledError` 的组**不是** `Exception`，所以
[tool_executor.py:92](../src/agent_workbench/runtime/tool_executor.py:92) 那句
"handler 故障是这次调用的结果，不是 run 的异常"的 `except Exception` 接不住它，
Worker 进程整个死亡。

**后果链，也是这条真正的痛处**：正在执行的 Task 心跳停止 → 租约过期 → 但
`reclaim_expired` 的唯一调用点在 Task Worker 自己的主循环里
（[task.py:245](../src/agent_workbench/workers/task.py:245)），单 Worker 部署下
没有任何存活进程做回收 → 任务在界面上永远显示 running（实测挂着"运行 7 小时"）。
B-04 关闭时证明的是"杀一个 Worker，另一个接手"；这次是**灭队**，那条证据帮不上。

**触发一侧，本次已修**：收敛点放在 adapters/mcp 的调用边界——
[client.py:144](../src/agent_workbench/adapters/mcp/client.py:144) 的
`is_client_fault` 判定 SDK 边界故障是否可吸收（纯取消与
KeyboardInterrupt/SystemExit 照常上抛），
[result_mapping.py:64](../src/agent_workbench/adapters/mcp/result_mapping.py:64)
把可吸收故障收敛为该节点的 `tool_failed`（`retryable=True`；取消优先于收敛），
[registry_source.py:62](../src/agent_workbench/adapters/mcp/registry_source.py:62)
对发现阶段同型处理（退化为零绑定而不是杀进程）。测试把实测异常形态原样钉住：
[test_mcp_result_mapping.py:471](../tests/adapters/test_mcp_result_mapping.py:471)
先断言"这个组不是 Exception"（前提本身入试），再断言收敛结果、纯取消上抛、
进程信号不吸收、取消优先；发现阶段两条在
[test_mcp_registry_source.py:392](../tests/adapters/test_mcp_registry_source.py:392)。

`retryable=True` 是测得的，不是许愿：ADR-059 的 `release_for_retry` 读的正是
节点 ErrorInfo 的这一位；配置只放行"整节点重放也安全"的幂等工具（见
registry_source 的 idempotency 注释）；且 2026-08-17 凌晨实测，被这次崩溃搁浅的
task_9273bf 在服务器恢复后回收重跑成功。**一处诚实的边界**：那次成功是跨进程
回收（Worker 重启后认领）；同一进程内对重启后服务器的重试未实测——SDK 的
streamable HTTP session 可能已过期，工具目录也是进程启动时冻结的。

**仍开着的暴露（所以这条不整体关闭）**：回收系于 Worker 舰队存活这个结构没变。
本次移除的是"MCP 传输异常"这一个已测得的灭队触发；任何别的把最后一个 Worker
干掉的路径（OOM、下一个未知的异常形态）都会复现同样的"永远 running"。与 B-02
（watchdog 未装进 Task Worker）同根：缺的是 Worker 之外的看门者。

**做完的判据**：存在一条不依赖 Task Worker 存活的到期处置路径（API 进程定时器
或独立 sweeper，标记或回收均可），并有一条"杀掉全部 Worker → 租约过期 → 任务
状态可见地离开 running"的测试。

---

## C. Multi-Agent

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| C-01 | 调用次数上限只有配置，无持久账本 | ~~未接线~~ **已关闭** |
| C-02 | 跨 retry 预算、partial failure、父子取消 | 部分实现（仅 partial failure 未做） |
| C-03 | 动态 supervisor / spawn / mailbox | 部分实现（spawn 有了，另两项未实现） |
| C-04 | CrewAI Adapter 与对比 benchmark | 未实现 |
| C-05 | `critic` 的合法结构化输出被判成"没有可用产出" | 未实现（根因已查清，见正文）|
| C-06 | 面板只认页面握着的事件：深链、刷新保持、"这棵树完不完整" | 部分实现（前两项已关闭） |
| C-07 | 收窄到一个子运行时，阶段列表仍报全任务的进度 | ~~未实现~~ **已关闭** |
| C-08 | 委派任务跑不完——`max_output_tokens` 管的是「思考+回答」 | ~~失败中~~ **已关闭** |
| C-09 | `max_report_chars = 8000` 正好把子代理的结论段截掉 | ~~失败中~~ **已关闭**（症状级证据） |

### C-01 `max_agent_invocation_attempts_per_task` 只有配置，没有账本 —— **已关闭**

**这一条曾经在关闭之后仍写着"未接线"，是本文档自己定义的"口径不实"。** 记在这里而
不是直接删掉：读到旧版本的人需要知道它错在哪一段。旧文说这条上限"需要一张能跨
retry/reclaim 累计的持久计数表"，而实际的做法比那更好——**根本不需要新表**。

**当前证据**：
[`adapters/persistence/task_registry.py:398`](../src/agent_workbench/adapters/persistence/task_registry.py:398)
的 `reserve_agent_invocation` 在**同一条 UPDATE** 里完成 `+1` 与比较，两者在一把行锁
之下，所以两个 Worker 不可能各自读到最后一个名额。上限读自那一行**自己的**
`run_semantics_snapshot`——是这个 Task **提交时**的数，不是这个进程今天碰巧配成的数
（与 `wants_report`、`export_requires_approval` 同一条规则）。超额抛
`AgentInvocationBudgetExhaustedError`，[`workers/task.py:312`](../src/agent_workbench/workers/task.py:312)
把它判成 `dead_letter` 而不是重试——因为下一次认领会读到同一个满的计数器，再试一次
不会有帮助。

计数落在 `task_runs` 行上，所以"跨 retry 与 reclaim 累计"是**行本身**的性质，不需要
第二张表。[`tests/persistence/test_agent_invocation_budget.py`](../tests/persistence/test_agent_invocation_budget.py)
12 条，打真实 PostgreSQL。

**投影层为什么仍然只有那几个字段**：不是因为"还没有能执行它的仓储"，而是因为执行它
的地方**不读投影**——它读 Task 自己的快照。把这个数投影进进程配置，反而会让人以为进
程配置是它的来源。`bootstrap/projections.py` 的 docstring 已相应改写。

**ADR-082 让这条更要紧了**：每个委派出去的子运行都经过 `BudgetedAgentExecutor` 记一
笔，所以这个数的语义从"这张图有几个节点"变成了"**含子代理的**总调用数"。它现在既是
计数也是闸。

### C-02 跨 retry/reclaim 的总预算、显式 partial failure、父任务到子调用的取消

**分类**：部分实现。三件事要分开判。

| 子项 | 判定 | 依据 |
|---|---|---|
| **跨 retry/reclaim 的总预算** | **已实现** | 与 C-01 同一处：计数在 `task_runs` 行上，reclaim 换 epoch 不清零 |
| **父任务到子调用的取消** | **已实现**（ADR-082） | 子运行收下的是父运行**同一个** `CancellationToken`（`adapters/tools/delegate.py`），不存在传播机制可以出错。反向——单独掐一个子运行而让父继续——**明确不做**：在只读子 agent 上没有价值，且会破坏"只有一个 token"这条简洁性 |
| **显式 partial failure** | **未实现** | 一个回合里并行派出的几个子运行，若一个失败，父运行拿到的是各自独立的 `ToolResult`（失败那个是 `status="error"` 且带上它写到一半的文字）。缺的是**节点层**把"三个里成功了两个"表达成一种状态，而不是让模型从三条工具结果里自己拼 |

### C-03 动态 supervisor 与 mailbox 未实现；spawn 已实现（ADR-082）

**当前事实**：编排的**骨架**仍然是固定图——v1 与 `v2_general` 两张，提交时选图并冻结。
三件事要分开判：

| 子项 | 判定 | 依据 |
|---|---|---|
| **spawn**（运行期从一次运行里派生另一次运行） | **已实现，默认关** | `multi_agent.delegation_enabled`。`adapters/tools/delegate.py` 的 `delegate_agent` 调用同一个 `AgentExecutor`，产出新 `agent_run_id`、同 `stream_id` 的子运行，父运行发 `AgentDelegated`/`AgentCompleted`。见 [ADR-082](./adr/0082-a-delegation-is-a-run-not-a-new-loop.md) |
| **动态 supervisor**（运行期决定拉起哪几个图节点） | **未实现** | `multi_agent.topology` 仍钉死 `Literal["fixed_langgraph"]`。委派不改变图，它在一个节点的运行**内部**发生 |
| **mailbox / agent 间投递** | **拒绝** | ADR-082 §5。父子之间已有一条更强的通道——同步、有序、有事务边界的 `ToolResult`；为一棵有共同根的树装一套异步邮局，买到的是 ack 状态机和看门狗，付出的是"消息可能没送到"这个全新的失败模式 |

**spawn 这一项做完的判据**（已满足）：同一个 `stream_id` 下出现两个 `run_id`，父运行的
`AgentDelegated.child_agent_run_id` 指向子运行，且宣布过的子运行**一定**有
`AgentCompleted`——包括被取消的路径。

**仍然要注意的口径**：这一项的默认值是**关**，且关着的时候工具不注册、信封不含它、
profile 原样不变。所以"这个部署有没有 spawn"是一个配置问题，不是一个版本问题。

### C-06 面板只认页面握着的事件：深链、刷新保持、完整性三件都缺

**分类**：未实现。三件事同源——客户端那棵树没有服务端那棵树的两样东西。

`web/src/components/runTree.ts` 从页面已持有的事件重建运行树，那个选择本身是对的，
理由写在它的文件头（再发一个 `/runs` 请求是用第二个请求学第一个请求带回来的东西，而且
它只在有人问时才刷新，会让一个号称实时的面板落后于旁边的时间线）。缺的是这个选择**换不到**
的三件：

| 子项 | 现状 | 缺什么 |
|---|---|---|
| **深链进某个子运行** | 选中的 run id 只活在 React state 里 | 它不进 URL，所以刷新即丢、也发不出去。服务端为这件事造了两个口子——`GET /v1/tasks/{id}/runs` 与 `timeline?run_id=`——[前端零调用](../web/src/api/client.ts) |
| **"这棵树是不是全的"** | 面板不表态 | ADR-083 不变量 5「一棵不完整的树说自己不完整」只做了服务端那一半：响应里的 `complete` 在客户端没有对应物。而同一个页面自己已经有 `timelineGaps`（`locateTimelineGaps`），它知道哪几页没送到 |
| **一页缺失就整块消失** | 面板在 `delegated.length === 0` 时返回 `null` | 一条被跳过的 `AgentDelegated` 足以让整个面板不渲染，而读者看到的是"这个任务没派过子代理"——与"有，只是那一页没送到"完全同形 |

**做完的判据**：选中的运行进 URL 且刷新后仍在；页面能说出"你看到的是这条流的一部分"，
并且这句话是从 `skippedSequences` 推出来的而不是猜的。

**前两项已关闭**（见 status.md 第三十四批）。收窄搬进 `?run=` 查询参数——顺带把"切换任务
后收窄仍生效"一并解决掉了，因为任务 id 在路径里，换任务就是换 URL。完整性那句话从
`timeline.skippedSequences` 推出来，不是猜的。

**没有走 `/runs`，也没有需要走**：深链进的是**本页**的某个运行，而这一页无论如何都要把
时间线拉下来。真正需要那个端点的是"深链进一个大到本页不会全量加载的流"，那件事今天不
存在——`useTaskTimeline` 一直翻到 `cursor` 为空为止。

**第三项仍然开着，且是有意停在这里的**：委派落在被跳过的那一页时，面板画不出那条分支。
现在有两处补偿——收窄活着时面板一定渲染（它握着唯一的退路），以及流有缺口时面板明说
这棵树可能不全。**没有**做的是"流一有缺口就把面板顶出来"：那会让每一个有缺口的任务都长
出一块写着"可能不全"的面板，而其中绝大多数**真的**没派过子代理。页面已经有
`TimelineGapNotice` 在说这条流有洞，让第二个组件把同一件事再喊一遍，换来的是一条几乎
总在误报的横幅。

### C-07 收窄到一个子运行时，阶段列表仍报全任务的进度

**分类**：已关闭（status.md 第三十四批）。

> **本条登记时写错了一句，留在这里而不是删掉。** 旧文的"做完的判据"说这要改
> `StepStream` 这个每个阶段共用的组件，因此"是一次会波及 Chat 与 Code 的改动"。
> **不是。** `StreamStage` 本来就有一个 `note` 字段——右侧那行"现在是什么状态"的文字——
> 而它由调用方 `WorkPage` 填。收窄开着且某个阶段一条步骤都没有时，把 `note` 换成
> 「不含所选运行」即可，`StepStream` 一个字不动，Chat 与 Code 一点没碰到。
>
> 登记一条缺口时顺手估的规模，和真去找过接缝之后的规模，是两个数。写错的是前一个。

[`WorkPage.tsx:465`](../web/src/features/work/WorkPage.tsx:465) 的 `deriveLifecycle`
读的是 `timeline.events`——**整条流**——而它下面每个阶段的步骤读的是 `shownEvents`，
也就是收窄之后的那份。于是选中一个子运行时，八个阶段照旧写着「已完成 12:03」，
底下一条步骤也没有。

**这不完全是缺陷**：阶段是**任务**的骨架，不因为读者在看其中一个运行就该被改写——按
运行重算阶段，等于让"这个任务走到哪了"随着一次点击变来变去。真正缺的是**在空出现的
地方说出收窄正开着**：现在唯一说这件事的那句话长在面板里，而面板不吸顶，长任务里一滚
过去，读者就在看一条被过滤的流而不自知。

**做完的判据**（已满足）：被收窄掉的阶段自己说得出"这一段里没有这个运行的步骤"，而不是
显示成一个做完了却什么都没做的阶段。

**阶段的状态仍然读整条流，这是有意的**：阶段是**任务**的骨架，不因为读者在看其中一个运行
就该被改写——按运行重算阶段，等于让"这个任务走到哪了"随一次点击变来变去。改的只是那句
右侧文字。

### C-08 委派任务跑不完 —— **已关闭**：`max_output_tokens` 管的是「思考 + 回答」

**分类**：失败中。测试写对了，被测的东西答错了——准确地说，是这个部署的三个数配不到一起。

**这一条是第三十五批把超时修好之后才露出来的**，因果要说准：调用之前死在 120 秒，就没
机会跑到把 token 花完；现在它们跑完了，于是撞上下一道墙。不是那次改动引入的，但也确实
是它让这道墙第一次可见。

**实测**（2026-08-28，`config.demo-local.toml`，`reasoning_effort = high`，
v2_general 的 `work` 节点派出四个 analyst）：

| 运行 | in | out | 结局 |
|---|---:|---:|---|
| 三个 analyst | 223 / 947 / 1029 | 13642 / 13891 / 9166 | 完成 |
| 第四个 analyst | 861 | **16384** | `token_budget` |
| 父运行 `work` | **107736** | 29247 | `token_budget` |

三个数：

| 键 | 值 | 撞它的是谁 |
|---|---:|---|
| `[model.main] max_output_tokens` | 16384 | 一个写长了的子代理，**正好**停在这个数上 |
| `[multi_agent] max_tokens_per_agent_invocation` | 120000 | 父运行，合计 136,983 |
| `[multi_agent] max_children_per_run` | 4 | 同一轮里第 5 次委派被拒（这条是**设计如此**，不是缺口） |

**2026-08-28 补测，把诊断改了**：上一版说这需要"两份分布"，然后去调其中一个预算。
量完发现**调预算是错的方向**。

把那次父运行（`work`）的每个模型回合的输出长度拉出来：

| 回合 | 输出字符数 |
|---|---:|
| 1 | 6,443 |
| 2 | 7,503 |
| 3 | **17,857** |
| 4 | 2,540 |
| 其余六回合 | 41～173 |

**父运行自己写了约 34,000 字**，而四个子代理的报告合起来最多 32,000 字
（`max_report_chars = 8000` × 4，已经是有界的）。也就是说，**吃掉上下文的一大半是父运行
自己的话，不是子代理的**。

读第一个回合的原文就知道它在干什么：

> 好的，我按 **coordinator** 的角色执行：先给三个 analyst 各发一份互相隔离的 brief……
> # Analyst A 返回：Redis 分布式锁失败模式 …… # Analyst B 返回：……

**它把三份分析自己扮演着写了一遍**，然后才真的去调 `delegate_agent`。同一份工作做了两
次，第一次是散文，第二次是工具调用——而第一次的产物留在上下文里，之后每个回合都要重发。

`max_total_tokens` 统计的是**逐回合累计的 prompt**，所以一段留在上下文里的长文本，其代价
是它的长度**乘以**它之后还有多少个回合。107,736 这个数就是这么来的，不是"扇出本来就贵"。

**因此做完的判据变了**：先让模型不要把委派叙述一遍。这是**提示词**的问题——
`delegate_agent` 的工具描述与 `work` 节点的提示词都没有一句话说"委派是一次工具调用，不是
一段要写出来的说明"。在这条改之前调任何一个预算，买到的都只是"让它把同样的浪费做得更久"。

**2026-08-28 收尾：这条已关闭，而关掉它的是第三个诊断，不是前两个。**

三次诊断，前两次都不是病根：

| 诊断 | 做了什么 | 结果 |
|---|---|---|
| ① 预算太小 | —— | 没动。量出来问题不在这 |
| ② 父运行把委派叙述了一遍 | `delegate_agent` 描述里禁止预演；`understand`/`work` 提示词加长度约束 | **正文从 17,857 字符降到 2,127**，收紧确实生效——**但运行照样死** |
| ③ `max_output_tokens` 管的是「思考 + 回答」 | `16384 → 32768`（两个 `high` profile） | **任务第一次跑完** |

③ 是决定性的那次测量：一次 `understand` 回合 `output_tokens = 16382` 贴着上限，
而正文只有 **2,127 个字符**（约 1,500 token）——差额约 **14,900 token 全是思考**。
`thinking = "enabled"` + `reasoning_effort = "high"` 时推理 token 与回答一起记在这个
上限名下（`ModelCompleted.thinking_preview` 是它留下的痕迹）。**没有任何提示词能修它**：
②已经把正文压到八分之一，运行还是死在同一处。

16384 是在档位还是 `low` 时量的（第二十三批），而那一批同时把档位提到了 `high`，
没有回头重量这个数——与「内层那道超时没有跟着一起提」（第三十五批）是同一个漏法的
第二例。

**关闭证据**：`task_75cd1e0c…` **succeeded**。八个运行、四个子代理全部完成
（19.2k / 19.3k / 19.9k / 941，各自对 30.0k 上限），`understand` 16.5k/120.0k、
`work` 84.9k/120.0k，产出 `failure-modes-comparison.md`。

**这一跑暴露了下一条，登记为 C-09**：三份子代理报告**都在 8,000 字符处被截断，恰好
丢掉各自的「总体结论」段**——`max_report_chars` 正好卡在结论前面。父运行为此补发跟进
brief，又撞上 `max_children_per_run = 4`，最后只能自己从失败模式清单里归纳 A、B 两条
结论。这些都是那次运行**自己在报告里如实写下来的**。

### C-09 `max_report_chars = 8000` 正好卡在子代理的结论前面

**分类**：~~失败中~~ **已关闭**（2026-08-28，`9a7011f`）。**证据是症状级的，**
**不是机制级的**——见本条末尾的关闭记录，那里写明了它证明了什么、没证明什么。
下面这段是它被发现时的原貌：由 C-08 关闭时那次成功运行暴露，而且是那次运行
**自己写下来的**：

> 三份原始报告都返回了完整的失败模式枚举，但每份都在 8000 字符处被截断，恰好丢掉了
> 各自的"总体结论"段。

**连锁反应**：父运行为了拿回结论，补发了三个聚焦的跟进 brief——然后撞上
`max_children_per_run = 4`（本轮已用 3 个），两个被拒。最后 A、B 两条底线结论是父运行
自己从失败模式清单里归纳的，不是子代理的原话。**一个截断上限把一次委派变成了两次，
再把第二次挡在配额外面。**

**为什么不顺手调大**：`DEFAULT_MAX_REPORT_CHARS` 的注释（`domain/agents.py`）写着它
存在的理由——一份大到会影响父运行上下文的报告，要在写进上下文**之前**就被挡住，因为
`agent_runtime` 要到下一轮开头才读得到 `last_input_tokens`。调大它就是把这道闸放松，
而 C-08 刚刚证明了父运行的上下文正是最紧的那处。

**真正该改的可能不是这个数（这一条后来被采纳了，见文末）**：截断发生在**结论**上，是因为结论写在最后。让子代理
**先写结论再展开**（`ANALYST` 的 system_prompt 里加一句），同样 8000 字符就能保住最
要紧的那段。这条比调大上限便宜，也不放松任何闸。

**做完的判据**：同一条 objective 重跑，三份子代理报告都带着结论回来，且父运行不需要
补发跟进。

**2026-08-29 补记：截断这件事此前在事件流里连个记号都没有。** 这一条不改变上面的判据，
但它改变了「谁看得见」。`delegate_agent` 把「[report truncated at N characters]」写在
报告正文的**末尾**（给父模型读的，它读 `content` 不读元数据）；而事件日志那一份要过
`bounded()` 的 4096 字符上限，8000 字的报告的结尾标记必然第一个掉。同时
`ToolCompleted.truncated` 这个字段**从被写下那天起就没有任何生产者**，全仓一处赋值都
没有，永远是 `false`——它的注释却一直在描述这个机制。于是控制台拿到的是一段没有任何标
记的半截报告，正是那个标记被加进来要防的读法。本批把它接上了：`ToolResult.truncated`
→ `ToolCompleted.truncated`，两条路径（成功与失败）都置位，失败那条此前连正文里的标记
都没有（它把 `clip_report` 的第二个返回值用 `[0]` 丢掉了）。

**2026-08-31 关闭记录（本次补登）。** 修法在 2026-08-28 的 `9a7011f`：`ANALYST` 与
`RESEARCHER` 的 system_prompt 各加一句「先给结论再展开」，并说明为什么（「你的报告会在
一个你看不见的长度上被截断」）。`max_report_chars` **一个字没动**——那道闸存在的理由正是
保护父运行上下文，而 C-08 刚证明父上下文是最紧的地方。

复跑（`task_593a20bc…`，见[实施状态](./status.md)「顺带：C-09 复跑」）：**截断标记 0 条**，
此前是三份报告全被截。

**这条证据到哪为止，要说准**：它证明的是**症状没复现**，不是「先写结论」这条机制被验证
了——那一跑的报告根本没写到 8000 字符，裁剪那条路没走到。真正被证明的只有「这一跑没丢
结论」。若要机制级证据，需要一份**确实超过上限**的报告，看结论是否因为写在前面而幸存。

**这一条为什么在文档里红了三天**：`9a7011f` 的提交说明写着「关闭 F-14、C-09」，代码与
`status.md` 都更新了，本文档的 C-09 却没有——F-14 那一行改了，C-09 那一行漏了。于是一个
**已关闭**的缺口在这里显示为**失败中**。方向与 E-05 惯常的那种相反（那种是把没做的说成做
了），但同属口径不实：两者都让读者据以判断的那张表与仓库事实不符。

---

### C-04 CrewAI Adapter 与对比 benchmark 未实现

**证据**：`pyproject.toml` 无 `crewai` 依赖。

### C-05 `critic` 的合法结构化输出被判成"没有可用产出"

**证据**：[agent_nodes.py:12](../src/agent_workbench/workflows/agent_nodes.py:12)
写明该模块只收"产出是一份存储 artifact"的节点，而 `plan` 与 `critic`
"need structured values decoded out of model output, so they need a decoding
contract and are deliberately not in this module yet"。缺的正是这份解码契约。

**观测**（2026-08-13，v1 图，真实 provider）：一次运行走完
`understand → plan → research_external → synthesize`，在 `critic` 终止，
`status_detail` 为 `the critic step did not produce usable output during start`。
而那一轮模型**没有出错也没有被截断**：

| 项 | 实测值 |
|---|---|
| `finish_reason` | `stop`（不是 `length`） |
| `output_tokens` | 255 |
| 文本长度 | 500 字符 |
| 整段 `json.loads` | **通过** |
| 内容 | `{"decision":"revise","reviewed_draft_ref":"art_…","revision_number":0,…}` |

也就是说模型交出了一份完整、可解析、字段齐全的裁决，节点仍然报"没有可用产出"。
失败落在 `AgentNodeFailedError` 里 `outcome.error is None` 的那一支——run 正常
完成但没有产出 artifact——这正是把一个**结构化解码节点**当成**artifact 产出节点**
来判定的后果。

**~~尚未查清~~ 2026-08-31 已查清，而结论是：上面这条"尚未查清"问错了问题。**

**先否掉本条自己的诊断。** 本条开头写着"缺的正是这份解码契约"——**它不缺**。
`plan`/`critic`/`review` 三个结构化节点自 2026-07-31（`13136e7`）起就跑在
[task_handlers.py 的 `_decoded`](../src/agent_workbench/workflows/task_handlers.py:632)
上，那正是 [ADR-034](./adr/0034-a-structured-node-asks-once-more.md) 的"再问一次"纠正轮；
[`_decode_review`](../src/agent_workbench/workflows/task_handlers.py:1183) 会把结果绑到
当前草稿与修订号上，并给出**四条各不相同**的拒绝理由（跑在合成之前 / 形状非法 /
评的是另一份草稿 / 评的是另一个修订）。契约在失败发生**之前**就已经在了。
`agent_nodes.py` 顶部那句"`plan` 与 `critic` …… 尚无解码契约，故意不在本模块"
只描述**该模块**收谁，被本条读成了"全仓没有契约"。

**也否掉第二个假设。** "`revise` 配空 `issues`"确实会被
[`ReviewResult.validate_decision`](../src/agent_workbench/domain/tasks.py:151) 拒绝，
但那条规则（"空列表只对 pass 合法"）是 **2026-08-11 的 `e808b34`** 加进契约的，早于
08-13 那次观测。所以"契约没说"解释不了这次失败。

**真正的根因是：这条链上的诊断是构造性失明的。**
[workers/task.py 的 `_status_detail`](../src/agent_workbench/workers/task.py:118)
认得 `TaskNodeRunFailedError`，却**只读 `error.outcome.error`**：

```python
info = error.outcome.error
if info is not None:
    return f"the {error.node} step failed with {info.code} ..."
return f"the {error.node} step did not produce usable output during {action}"
```

而结构化节点的解码失败，**按定义** `outcome.error is None`——模型跑完了、没报错，
只是输出不满足 schema。于是它**永远**落到那句通用兜底上。同时
`TaskNodeRunFailedError` 身上挂着两份更具体的说法：`self.reason`
（如"critic JSON did not satisfy the review schema"）与 `__cause__`（那四条之一），
**两份都没有任何读者**。

所以本条问的"是 A 还是 B"没法回答，不是因为没继续查，而是因为**A、B 以及其余每一种
解码失败都产出同一句话**。那次观测**不可能**区分它们，往后每一次也不能——除非先把
`reason` 露出来。

**做完的判据（本次重写）**：第一步不是补解码契约（它在），而是让
`_status_detail` 在 `outcome.error is None` 时读 `TaskNodeRunFailedError.reason`
（外加 `__cause__`），使下一次失败能说出四条里的哪一条。**在那之前，任何关于 C-05
根因的结论都只能是猜测**——包括本次否掉的那两个假设。此后再按真实理由决定要不要
改契约、改回边，或都不改。

**本次未改代码**，只把结论写下来：改 `_status_detail` 会改变 Task 的失败文案，属于
可观测行为，值得单独一次改动。

**顺带记下**：同一次运行里 `critic` 给出的理由是草稿"未提供任何实际内容，仅包含
任务指令的重复"——即 `synthesize` 那步的产出质量也有问题。这是另一件事，本条不
覆盖。

**做完的判据**：`plan` 与 `critic` 有一份写下来的解码契约（读什么字段、字段缺失
怎么办、解不出时的纠正轮走几次），并有一条测试：喂一份合法裁决进去，节点必须
把它变成状态而不是失败；配一条对照组，喂一份真正解不出的输出，确认它才是失败。

**后续（2026-08-16 排查）**：判据的代码半已经在了——`task_handlers.py` 的
`critic` 走 `_decoded`（ADR-034 的一轮纠正 + `decode_review_output`），
`tests/workflows/test_task_handlers.py` 正反两条都有（合法裁决 → 状态、
解不出 → 失败）。当年观测里的另一个候选原因（`revise` 在 v1 里没有可走的
回边）已由 [ADR-060](./adr/0060-an-exhausted-reviewer-annotates-not-vetoes.md)
一并移除：耗尽也有去处了。**还差的只是一次对真实 provider 的复跑**，确认
2026-08-13 那个形态不再复现——在那之前本条不标关闭。

---

### 一条不是缺口的说明：Redis

**Redis 不存在不是缺陷。** 当前以 PostgreSQL 作为唯一事实源是一个合理选择，
并且是被显式论证过的。真正缺的是 B-01 的 LISTEN 消费端，而不是换一个中间件。

---

## D. 产品与生产能力

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| D-01 | Chat turn-scoped 附件、Task 输入 Artifact 附件 | 未实现 |
| D-02 | Chat Session 服务端列表与重命名（删除已补上） | 部分关闭 |
| D-03 | 知识库重命名 / 删除 / 文档删除 / ACL UI | 未实现 |
| D-04 | Word 读取与不可变编辑 | 未实现 |
| D-05 | Langfuse、生产身份认证、S3 Artifact、远程部署 | 未实现 |

> 编号一经退休不再复用。D-06（Chat 历史 compaction，词表齐备而没有任何发射点）已于
> 2026-08-25 关闭，按本文档维护规则从正文删除，落地记录在 [status.md](./status.md)：
> `runtime/compaction.py` 决定切点、`ClaudeLikeAgentRuntime._compacted()` 用同一个
> `ModelPort` 取概括并发射 `ContextCompacted`，`runtime.context_compaction_enabled`
> 默认关。理由见 [ADR-081](./adr/0081-a-conversation-that-was-shortened-says-so.md)。
> 该条目当年说"事件类型的存在不构成能力"——现在构成能力的是那一批测试，不是这份记录。

### D-01 真正的 Chat 轮次级附件与 Task 输入 Artifact 附件

**当前事实**：`routes/uploads.py` 只有 `POST ""` 与 `POST /{upload_id}/complete`
两个端点。上传能力在，但"这一轮对话带这几个文件"和"这个 Task 以这份文件为输入"
两条产品语义没有接上。

**Code 那一条已经有了，而且走的是另一条路**（ADR-057 那次改动顺带）：
`PUT /v1/code/sessions/{id}/workspace/{name}` 把一份人给的文件直接写进会话工作区，
复用 `SessionWorkspace` 的写入与 compare-and-set 指针推进。它**没有**复用
`/v1/uploads` 三步流程，因为那条流程的终点被 `CompleteUploadRequest` 钉死在知识库
上；而一个编码会话要的不是"进知识库"，是"进这个工作区"。

允许二进制，这一点与 `WorkspaceWriteTool` 拒绝 docx/xlsx/pptx/pdf 不矛盾：那条
拒绝管的是**模型**能合成什么（模型吐出它声称是 docx 的字节，没有读者该信），
而人附一个 PDF 时，字节就是他手里的东西。

已验证（2026-08-16 本地）：传入一个 24 字节的 `rows.csv`，随后一句「读一下，第二列
加起来是多少」，回合读回文件并答 21。

### D-02 Chat Session 的服务端列表、重命名与完整历史元数据 —— **删除这一半已关闭**

**已关闭的部分**（ADR-056）：`DELETE /v1/chat/sessions/{id}` 存在，
`ConversationStore.delete_session` 在内存与 PostgreSQL 两套实现上跑同一份契约
用例，控制台侧栏每一行都有删除按钮。会话行、消息、chat_turns 与该会话的事件流
一起消失；工作区 artifact 按 ADR-056 §5 保留为不可达。

**仍然没有的**：列表与改名。[chat.py](../src/agent_workbench/apps/api/routes/chat.py)
的路由是 `POST /sessions`、`POST /sessions/{id}/messages`、
`GET /sessions/{id}/messages`、`DELETE /sessions/{id}` —— 没有 list，没有 PATCH。

**为什么删除能先做而列表不能**：删除只需要一个 id，而列表要先回答
`answerMode` 与 `knowledgeBaseId` 属不属于会话本身（见 F-06）。那是产品决定，
不是接线问题；在它之前切一半列表会得到两份互相矛盾的清单。

**当前的后果**：控制台的删除是两处一起做的 —— 服务端行删掉，浏览器本地那行也
删掉。一个从别的浏览器打开过的会话，服务端已经没有了，而那个浏览器的
localStorage 里还留着一行指向不存在会话的记录。这是 F-06 的同一笔账。

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

---

## E. 测试与发布证据

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| E-01 | Playwright 只验外壳，后端全 mock | 未实现 |
| E-02 | 缺真实 Word MCP Server 的官方 Client 闭环 | 未实现 |
| E-03 | ~~CI 不跑 E2E~~ 已进 CI；离线评测 / Compose 仍不跑 | 部分实现 |
| E-04 | ~~首个 manifest 未生成~~ 已生成过两份；缺的是没有消费者 | **口径不实 + 未接线** |
| E-05 | 文档中的数字过时 | **口径不实** |
| E-06 | 前端样式表按领域拆分，做不成一次纯搬运 | 未实现（不排期） |
| E-07 | ~~崩溃恢复 e2e 三条红~~ 已修；CI 仍不跑 `tests/e2e`（见 E-03） | **部分关闭** |
| E-08 | 能力阶梯落后于已发布的产品面，整块没有行 | **口径不实** |

### E-07 `test_worker_process_crash_recovery.py` 三条红 —— **已修**（CI 仍不跑它）

> **本条原编号 E-06，与既有的「前端样式表按领域拆分」撞号，本批让号改为 E-07。**
> 撞号是这条分支引入的：写它的时候只数了表格里的最后一行，而那一节的编号排到 E-06
> 却没有进表。认领编号要看的是**全文**，不是表格。

**分类**：失败中。不是"未实现"，也不是"口径不实"——测试写对了，被测的东西答错了。

**症状**：v1 图的 `approval` 节点一次都没跑（`approval ran 0 times`），而 Task 仍然
`succeeded`。三条都断在同一个位置：

- `test_recovery_re_runs_only_the_node_that_died`
- `test_the_two_processes_split_the_graph_at_the_node_that_died`
- `test_the_same_task_completes_in_one_process_when_nothing_kills_it`

提交用的 `run_semantics_snapshot` 是 `{"model": {"provider": "fake"}}`——里面既没有
`workflow` 段也没有 `multi_agent` 段，所以 Worker 对"要不要审批"的判断落在它自己的
配置上而不是 Task 的快照上。这是查这条时的第一个可疑点，尚未证实。

**不是哪一批引入的（有对照）**：在未经改动的基线 `414f37c` 上另开一个 worktree、
用同一个 `.venv`、同一条命令跑同样三条，**一样红**。所以它先于
`feat/multi-agent-orchestration` 存在。

**为什么没人被通知**：CI 的服务型 job 只跑
`tests/contracts`/`tests/persistence`/`tests/api`/`tests/vector`（见
`CLAUDE.md` 与 `.github/workflows/`），**`tests/e2e` 不在其中**。这与 E-03 是同一
个洞的两面：E-03 说 CI 不跑 E2E，这一条是"于是它红了也没人知道"的实例。

**2026-08-28 查实并修好了。上面那段猜测「是快照」，猜错了一半——它猜对了地方，猜错了
机制。**

真正的原因：`export_requires_approval` 是一个**部署设置**而不是提交者的选择
（`application/task_inputs.py` 写着为什么：提问的人不该决定自己的产出要不要被审阅），
它在 Task 载入时读一次、冻进检查点。而 `settings.py:683` 的出厂值是 `false`，
`config/config.test.toml` 又没有声明 `[workflow]` 段——**于是这些 Worker 是在没有审批门
的部署下起来的**，`approval` 一次都不会跑，而这个文件的每一条断言都在数它。

不是快照的问题：快照那条路（`{"model": {"provider": "fake"}}` 里没有 `workflow` 段）
是真的，但它本来就不该供这个值——这个值从来不来自快照。

修法是一行：`_child_environment` 里补
`AW_WORKFLOW__EXPORT_REQUIRES_APPROVAL=true`。**这个文件的全部主题就是 v1 图跨过它的
人工门**，所以它必须把自己要测的那个部署配出来，而不是继承一个恰好没有门的默认。

**实测**：加这一行前 `3 failed, 8 passed`，加之后 **11 passed**（对着真实
PostgreSQL）。

**为什么能猜错这么久**：没有人跑过它。`tests/e2e` 不在任何一个 CI job 里（E-03），所以
上一版那段推断写下来之后，既没有被证实也没有被证伪。

**仍未关闭的那一半**：`tests/e2e` 进入某个每 PR 都跑的 job。否则下一次变红仍然不会有人
知道——这一次它红了大概两周。

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

**2026-08-28：`tests/e2e` 进了 `stateful` job。** 上面那句「E2E 需要真后端起栈」把它
说得比实际贵——那个 job 早就起着真 PostgreSQL 和 Qdrant，而 `tests/e2e` **需要的正好
就是这些**：同一个库、同一次迁移、同样两个环境变量，不需要 provider key（这些运行是
`provider = "fake"`）。实测同一条命令：

```
pytest -q tests/contracts tests/persistence tests/api tests/vector tests/e2e
→ 1339 passed, 2 skipped, 122s
```

单独 `tests/e2e` 是 25 passed / 23s。job 超时从 15 分钟提到 20：它要起 Worker 子进程
并等真实的租约过期，墙钟由协调时序决定而不是由 runner 多快决定。

**为什么值得**：E-07 那三条崩溃恢复断言红了大约两周，而解释它们为什么红的那段笔记是
一个**没人能证实的猜测**——因为没有任何东西跑过那个文件。一个不在 CI 里的套件，缺的不
只是安全网，它会积累从未被对照过的解释。

**其中两个文件在 runner 服务不了时会自己响亮地跳过**，而不是变红：沙箱那条 `image
inspect` 不到就跳过并把 `pull` 命令写在跳过原因里，RAG 那条没有 Qdrant 就跳过。将来
runner 少了哪一样，跳过行会指名道姓，不必有人去二分。

**仍然不跑**：离线 RAG 评测（需要 embedding extra，按分层约定 CI 不装——`quality` job
还专门断言它**没有**被装上）、Compose 启动。这两项是剩下的缺口。

### E-06 前端样式表按领域拆分，做不成一次纯搬运

**证据**：[web/src/styles/app.css](../web/src/styles/app.css)（5766 行）与
[web/src/styles/minimal-theme.css](../web/src/styles/minimal-theme.css)（2233 行），
后者在 [web/src/main.tsx](../web/src/main.tsx) 里后置加载、覆盖前者。

**想做的事**：拆成 `shell.css` / `workspace-sidebar.css` / `components.css` /
`features/*.css`，让一次改动落在可预测的文件里。

**为什么做不成**：这两份样式表把层叠决定编码在**跨领域的源码顺序**里，而不是
特指度里。按领域重排文件就会翻掉这些决定。实测过一次完整的拆分，用「四个路由 ×
两套皮肤 × 两个视口，每个元素记 50 个计算属性加包围盒」的指纹逐条比对：

| 布局 | 变了的元素 |
| --- | --- |
| 按领域拆（tokens → base → shell → sidebar → components → features/*） | 74 |
| 先按层再按领域（`base/` + `theme/`） | 56 |
| 再把 `theme/workspace-sidebar` 挪到该层最后 | 12 |
| 再把两条规则改成靠特指度取胜 | 5 |

四条根因，每一条都是同一个形状——一条本该赢的规则原本只靠「排在文件后面」取胜：

1. `minimal-theme.css` 的 `.aw-chat-page, .aw-code-page, .aw-work-page, …
   { grid-template-columns: minmax(0, 1fr) }` 是 workspace-first 改版追加在文件
   末尾的整页重置。它跨四个 feature，拆分后排到了 `.aw-chat-page
   { grid-template-columns: 236px … }` 前面——移动端因此退回桌面两列。
2. `app.css` 的 `@media (max-width: 760px) { .aw-chat-page, .aw-work-page
   { display: flex } }` 同理，被各页面自己的 `display: grid` 压过。
3. `@media (max-width: 760px) { .aw-button, .aw-icon-button { min-height: 44px } }`
   ——触屏最小可点高度，被 `.aw-chat-send { min-height: 38px }` 压过，也就是说
   拆分会**破坏一条无障碍约束**。
4. 把 2、3 改成 `.aw-app-content :is(…)` / `.aw-app-shell :is(…)` 能解决它们，
   但 0-2-0 的 base 规则会跨过层边界压住 0-1-0 的 theme 规则，翻出新的两处。

**结论**：这是一件要逐条决定「谁应该赢，以及凭什么赢」的工作，不是搬文件。它需要
先把顺序依赖改写成特指度依赖，每改一条对着上面那把尺子验一次。拆分本身可以留到
那之后，也可能到那时已经不必要了。**不排期**——排期会让它看起来像一次机械工作。

### E-04 evidence manifest 有工具、有产物，但没有消费者

**这一条要更正一个常见误解：工具已经存在。**
[bootstrap/evidence.py](../src/agent_workbench/bootstrap/evidence.py) 实现了
`write` 与 `verify` 两个子命令，有配套测试
[test_evidence_manifest.py](../tests/bootstrap/test_evidence_manifest.py)，
默认输出 `artifacts/evidence/<gate>/manifest.json`。它区分**派生事实**
（配置 revision、policy 指纹、图版本、模型与索引身份、commit——全部自动取，
"nothing here can be wishful"）与**附件**（按 SHA-256 与大小记录），并且有两条
拒绝：附件不存在就不写；工作树脏就不写，除非显式 `--allow-dirty`。

**2026-08-31 更正：「从没跑过一次」是假的，而这一条此前就是这么写的。**
`artifacts/evidence/` 下有**两份**已生成的 manifest：

| gate | 时间 | commit | 脏否 | 附件 |
|---|---|---|:---:|---|
| `gap-closure-2026-08-11` | 08-11 08:26 | `bf31815` | 是（`--allow-dirty`） | 1 |
| `batch3-2026-08-11` | 08-11 21:50 | `cc7d146` | **否** | 5（4 份评测报告 + 真实服务测试报告）|

第二份是一份**干净树上、带附件**的 manifest，而且
`agent-evidence verify artifacts/evidence/batch3-2026-08-11/manifest.json`
今天（2026-08-31，二十天之后）仍返回 `status: ok`、`problems: []`——附件的
SHA-256 逐个对得上。**这个机制不但跑过，还往返验证过。**

**所以真正缺的是别的东西，而且比"跑一次"难：没有任何消费者。**

- **CI 一次都不碰它**：`.github/workflows/` 里 `agent-evidence` 零命中，`write` 与
  `verify` 都不在任何 job 里。
- **manifest 不随仓库走**：[.gitignore:44](../.gitignore) 忽略整个
  `artifacts/evidence/`，`git ls-files | grep manifest.json` 命中 **0**。也就是说
  它只存在于**跑过它那台机器**上，读仓库的人看不到。
- **于是它不会过期，因为没人指望它新**：最后一份停在 2026-08-11、schema `1.14`，
  而当前是 `1.19`。

**这解释了 E-05 为什么治不好。** 上一版把 E-04 叫作"E-05 唯一的根治手段"，方向对，
但机制没接上：manifest 生成之后不进仓库、不进 CI、不被任何文档引用，于是它无法让
任何一句过期的散文失败。**跑一次不是修复，被消费才是。**

于是仓库里所有数字仍以散文形式存在——状态文档里的测试计数、README 里的评测数字——
每一条都为真，每一条都不可核查。

**做完的判据（2026-08-31 重写，并已定下方向）**：不再是"生成首份 manifest"（早已完成）。

**已决定：manifest 保持本地产物，不进版本控制**，只在发布时手动生成。这排除了
"提交 manifest + CI verify"那条路——`artifacts/evidence/` 继续被 [.gitignore](../.gitignore) 忽略，
CI 也就没有可 verify 的对象。**这是一个取舍而不是遗漏**：它换来的是仓库里不堆积
per-gate 的 JSON，代价是"过期在 CI 里失败"这条机制在本仓库不成立。

于是判据收敛成**唯一一条**：**门禁数字由 manifest 生成，而不是手抄。**
[HIGHLIGHTS §2](./HIGHLIGHTS.md#2-门禁与规模) 那四行与规模行，应当来自一次
`agent-evidence write` 的产物，而不是有人跑完测试再把数字誊进 Markdown。

**这条路更难，要说清楚。** 上面被排除的那条是"让机器发现过期"；剩下这条是"让人不再
有机会抄错"。前者能在 CI 里失败，后者不能——所以只要数字还靠人往文档里搬，E-05 就仍会
复发，只是复发面积从"任何数字"缩小到"生成与粘贴之间"。本文档 2026-08-31 这一版已经
第三次记录 E-05 复发；把这条判据做完之前，应当默认它还会有第四次。

### E-05 文档中的数字过时 —— 口径不实

**这一条在 2026-08-12 的文档重写里又前进了一步，但没有消失。** 做了三件事：

1. **门禁数字收敛成单一来源**。它们此前在 README、`docs/README.md`、
   [HIGHLIGHTS](./HIGHLIGHTS.md) 与本文档各存一份，改一处必漏三处。现在只有
   [十分钟版本的门禁与规模一节](./HIGHLIGHTS.md#2-门禁与规模)维护数值，其余文档一律链接过去。
   四份复述降到一份，是把复发面积缩小，不是把机制补上。
2. **数字重新实测**。两组后端计数在 `main@921dda5` 上重跑确认：真实
   PostgreSQL + Qdrant `2758 / 11`，不起外部服务 `2065 / 704`，跳过构成逐条核对。
3. **锚点的措辞改了**。hash 现在明说自己记的是"测量时那棵树"而不是"当前基线"——
   同一个 hash，不同的承诺；后者一往前走就变成假话，前者不会。

**上一版点名的落后处已经处理**：[架构基线](./architecture-baseline.md) 第 17 节
那张门禁表已删除，改为链接 README。删而不是刷新，是因为刷新只推迟下一次过时。
**删的时候抓到一个此前没人发现的错**：该表写"无外部服务那一行多出的 676 项跳过"，
实测是 693（634 项 DSN 未设 + 59 项 Qdrant URL 未设）。它在那里错了不知道多久，
而同一份数据在 README 里一直是对的——这正是"同一组数字存在两处，一定有一处先烂掉，
而两处看起来一样可信"的实例。

**同一次重写还暴露了这一类的反向形态**：本文档 B 组的四条曾**声称仓库里没有
某项能力，而它已经落地**——B-02 写着"全仓无 `watchdog` / `loop_lag` 符号"，
而 `EventLoopLagWatchdog` 已装进 API 进程；B-04、B-05 同类；B-01 说"两条消费路径
都在轮询"，而 Task Worker 那条已接上。它们已按核对结果改写或退休。
**这个方向同样是口径不实**：正向形态把缺口伪装成能力，反向形态把能力伪装成缺口，
两者都让读者无法用文档判断代码。而反向形态更难被发现——没有人会去质疑一份
自称"还没做"的清单。

**为什么这一条不会"修完就消失"**：数字过时是持续现象，不是一次性缺陷。真正
消除它的是 E-04 的 evidence manifest——把数字从散文变成可校验的引用，让"过时"
在 CI 里失败，而不是靠人记得来改。在那之前，这一条每次基线变动都会复发一遍。

**为什么归入口径不实而不是"文档没更新"**：这些数字是读者用来判断"我读的这份
文档描述的是不是我手上这份代码"的锚点。锚点错了，整份文档的可信度都要打折。

---

### E-08 能力阶梯落后于已发布的产品面 —— 口径不实

**分类**：口径不实（2026-08-31 本次扫描新登记）。

**为什么是"口径不实"而不是"文档旧了"**：[CLAUDE.md](../CLAUDE.md) 指定
[架构基线](./architecture-baseline.md) 第 17 节为"能力**处在哪一级**"的权威，并规定
能力只能凭可链接的证据往上走。当那张表漏掉整块已发布的产品面时，按它去判断"这个能力
做到哪一步了"会得到错的答案——和 E-05 是同一类危害，只是载体从数字换成了行。

**方向与常见的那种相反，但危害同类**。E-05 那种是把没做的说成做了；这一条是把
**做了并且测过的**记成 Planned 或干脆没有行。前者骗读者高估，后者让一份用来对外说明
"我们做到哪"的文档**低估自己**，同时让"阶梯"这个机制失去意义：如果落地不必登记，那
登记就不再是能力状态的事实来源。

**证据**：见[架构基线 §17](./architecture-baseline.md) 开头本次补的过时声明，那里列了
五块**表里连一行都没有**的产品面（computer use、项目、用量面板、工具投影、计划模式），
每块都给了 `src/` 与 `tests/` 的命中文件数；以及一行低估（「动态 supervisor / agent
spawn / 持久 mailbox」整行只勾 Planned，而 spawn 已落地）。

**本次只补了声明，没有重排阶梯**：往上挪一格要逐条给证据，那是另一件工作，而做成
"看起来重排过了"比留着一条明确的过时声明更坏。

**做完的判据**：§17 的表覆盖当前所有已发布产品面，每一格的位置都能指向一条测试或一次
演示；且这件事有一条防复发的机制（同 E-04：让"阶梯落后于代码"在 CI 里可检出），否则它
会以 E-05 的节奏复发。

---

## F. Code 模式

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| F-01 | 一轮不可恢复：进程没了，那一轮就没了 | **拒绝** |
| F-02 | 部署必然砍断在跑的回合 | **拒绝** |
| F-03 | Code 没有持久幂等 | **拒绝** |
| F-04 | 同一 principal 跨会话的工作区不隔离 | **拒绝** |
| F-05 | ~~没有工具会触发审批~~ `sandbox_run` 接上了，闸门在用 | **已关闭** |
| F-06 | Chat 的侧栏仍是本地列表（Code 那半已关闭） | 部分关闭 |
| F-07 | 步骤最快也要等一个轮询周期才出现（默认 10s） | 已知代价 |
| F-08 | 重新开启导出闸门的部署没有跨任务收件箱 | 已知代价 |
| F-09 | 评测和 Code 抢同一块内存，没有跨子系统准入控制 | 已知代价 |
| F-10 | 从界面发起一次评测会覆盖已提交的报告文件 | 已知代价 |
| F-11 | Code 工作区的 .docx 没有版面预览 | 已知代价 |
| F-12 | HTML 预览的出网封锁是尽力而为，不是硬边界 | 已知代价 |
| F-13 | 产出文件卡片预览的是该文件名**此刻**的字节，不是那一轮的 | 已知代价 |
| F-14 | ~~Task 工作集里的文件在界面上完全不可见~~ | **已关闭**（ADR-088） |
| F-15 | ~~运行产出卡片对 listing 时序有可见的不一致~~ | **已关闭** |
| F-16 | 刷新之后，历史里的答案不带引用标记 | 已知代价 |
| F-17 | 工具进度的预览有损：单条 2 KB、整次 64 KB，到顶静默停止 | 已知代价 |
| F-19 | computer use 的批准是进程级的，不是 MCP 会话级的 | 已知代价 |
| F-20 | 跨产品归属的三处数据没人再读写（ADR-074 之后） | 已知代价 |
| F-21 | 不可重试的 MCP 工具（点击、截图）进不了 Task | **拒绝** |
| F-24 | 项目目录的回合没有容器可用 | **拒绝** |
| F-25 | 读写回执喂不满：三条路径绕过工具改动目录 | 已知代价 |
| F-26 | `policy.write_tools_require_approval` 读起来像保证，src/ 里没有读者 | **口径不实** |
| F-29 | computer use 只能激活已经开着的应用，不能启动应用 | **拒绝** |

> 编号一经退休不再复用。F-23（项目目录的回合被告知自己在一个扁平的、有版本的工作区
> 里）已于 2026-08-25 关闭，按本文档维护规则从正文删除，落地记录在
> [status.md](./status.md) §1：`CODER_SYSTEM_PROMPT_PROJECT` 由具名替换从基底派生，
> 由 `_system_prompt_for` 按本回合被提供的工具选中，锚点缺失时在 import 时抛错。
> `src/` 与 `tests/` 里若干注释仍以过去时引用这个编号——它们讲的是这段散文为什么是
> 现在这个样子，读者顺着编号找到的应该是这里。
>
> F-22（截图按显示器给坐标、点击按全局坐标发事件）已于 2026-08-28 关闭，同样按
> 维护规则从正文删除，落地记录在 [status.md](./status.md) §1 与
> [ADR-090](./adr/0090-a-coordinate-carries-the-screen-it-was-measured-on.md)：
> `Display` 带上自己的原点，换算落在 `domain/computer.py` 的 `DisplayFrame`，
> 多屏时省略 `display_id` 由「当作主屏」改为拒绝。**它写下的判据只兑现了算术那一半**
> ——「两块显示器上各点一次」这台机器仍然做不到，那一半的证据停在
> `SECOND_DISPLAY` 这个假显示器上，见 ADR-090 §5 末段。
>
> F-30（`activate_application` 在 macOS 上改变不了前台应用）已于 2026-08-29 关闭，
> 同样按维护规则从正文删除，落地记录在 [status.md](./status.md) 第四十七批与
> [ADR-092](./adr/0092-a-server-that-changes-the-front-of-the-screen-is-an-application.md)：
> 原因不是 ADR-091 的门禁写错了，是**进程形态**——macOS 要求四个条件同时成立
> （bundle 身份、代码签名、辅助功能授权、**主线程活着的 run loop**），缺任何一条
> 都是**静默**失败。服务器改成签名的 `.app`、主线程交给 `NSApplication`、uvicorn 挪到
> 后台线程之后实测 15/15，端到端（真 MCP 客户端 → 真服务器 → 真屏幕）通过。
> 该条目当年写「两条出路各自带着一次要推翻的决定」是对的：推翻的是 ADR-076 §2，
> 而推翻的是它的结论，不是它的理由。
>
> F-31（JSX 里折行的中文段落渲染出来多一个空格）于 2026-08-29 **当天登记、当天关闭**，
> 按维护规则不在正文留条目，落地记录在 [status.md](./status.md) 第四十九批。它被登记时
> 写的是「186 处、21 个文件」——那个数是正则扫出来的，**虚报了 3.4 倍**；改用 TypeScript
> 的 parser 只看 `JsxText` 节点内部的换行之后是 **54 处、11 个文件**，同批清完，并留下
> `web/src/test/jsxChineseWrap.test.ts` 让它长不回来。

### F-20 跨产品归属的三处数据没人再读写 —— 已知代价

**证据**：[models.py](../src/agent_workbench/adapters/persistence/models.py) 里
`conversation_sessions.project_id`、`task_runs.project_id` 与
`project_knowledge_bases` 三处仍在；
[routes/projects.py](../src/agent_workbench/apps/api/routes/projects.py) 的
`PATCH /v1/chat/sessions/{id}/project` 与 `PATCH /v1/tasks/{id}/project` 仍在。
但 [ADR-074](./adr/0074-a-project-is-where-code-happens.md) 之后没有任何界面读写
它们——Chat 头部那个归属选择器已经下线，独立的项目页已删除。

**为什么保留**：那些行里存着人做过的判断。ADR-071 §2.2 立下的规矩是「删掉标注不该
删掉被标注的东西」，而一次产品形状的改变顺手删一列数据，是同一种破坏换了个名义。
下线一个端点和删一列数据，也是同一种破坏的两种形式。

**代价**：库里有一些没有任何界面在用的列，读 schema 的人会以为它们还在起作用。
这就是它写在这里而不是假装不存在的原因。

**做完了算什么样**：要么有一次明确的、单独确认过的数据迁移把它们清掉，要么有一个
新的产品形状重新用起它们。两者都需要先写 ADR，不是顺手做掉的事。

### F-01 一轮不可恢复 —— 拒绝

**证据**：[code_session.py](../src/agent_workbench/application/code_session.py)
模块 docstring；`code.execution_locality` 与 `code.coordination` 是单值
`Literal`（[settings.py](../src/agent_workbench/bootstrap/settings.py)），架构测试
`tests/architecture/test_code_premises_are_frozen.py` 钉住它们。

**这是拒绝，不是遗漏。** 可恢复性需要一个能在崩溃后**释放**半成品状态的写者——租约
加 reaper。要它就得要一整套：活跃槽、过期回收、检查点。Code 用放弃可恢复性换掉了
整个协调面，换来的是"回答审批的人正对着那个停着的协程说话"，而那是等待唯一诚实的
一种安排。

崩溃后没有任何东西需要回收：没有租约、没有 `release_pending`、没有数据库里的活跃槽。
工作区停在**最后一次成功写入**上（指针是每次写成功就推进的），用户把那句话再说一遍。

**做完的判据**：不适用。要改，先改
[更正文档](../var/plans/2026-08-14-code-turns-are-not-chat-turns.md) 的结论。

### F-02 每次部署必然砍断在跑的回合 —— 拒绝

**证据**：`code.turn_timeout_seconds` 默认 600，`api.shutdown_grace_seconds` 默认远
小于它；`ApiDependencies.dispose()` 在关引擎之前调
`CodeSessionService.drain_cleanup`，但只等 grace 那么久。

**算术要写出来**：一轮最长 600 秒，优雅关停最多等 grace 秒，超出的部分被砍断。被砍断
的那一轮不留任何需要回收的行（见 F-01），工作区停在最后一次成功写入上。

**为什么不做交叉校验**：把 `turn_timeout ≤ shutdown_grace` 写成启动期校验，等于要求
每次部署等待最长的一轮跑完，那是把一条运维约束伪装成配置错误。

### F-03 Code 没有持久幂等 —— 拒绝

**证据**：[routes/code.py](../src/agent_workbench/apps/api/routes/code.py) `ask`
的 docstring。`Idempotency-Key` 必填，但它只用来派生稳定的 run id。

Chat 的幂等住在 `chat_turns` 那本账本里，而 Code 一行都不写（这是 F-01 的同一个
决定）。所以：打到一个正在忙的会话的重试是 409；**进程死后的重试是新的一轮**。

### F-04 同一 principal 跨会话的工作区不隔离 —— 拒绝

**证据**：[workspace.py](../src/agent_workbench/application/workspace.py) 的
get/put 只带 `(tenant_id, principal_id)`；
[artifact_store.py](../src/agent_workbench/ports/artifact_store.py) 自己写着
"Hard to guess is not an authorization rule"。

**今天不可达**：没有任何入口收 workspace version——它经由服务进入的 `ContextVar`
到达工具，经由比较并交换到达数据库。守卫是架构测试
`tests/architecture/test_a_workspace_version_is_never_asked_for.py`，它扫路由的输入与
工具 schema 的 properties。

### F-05 审批闸门第一次被真实工具触发 —— **已关闭**（[ADR-057](./adr/0057-a-pure-function-is-not-a-shell.md)）

这一条原来写的是「闸门接好了，但今天没有工具会触发它」，理由是 `CODE_TOOLS` 只有五个
工作区工具（risk 是 read/write），而信封的 `approval_required_risks` 是
`("external", "destructive")`；`code.shell_enabled` 冻结为 `False`。

**当时的预判是对的**：那条说「C4 把 `sandbox_run` 给 Code 的那天，需要的是改这个元组
和风险上限，而不是改闸门底下的机器」。闸门、registry、决定端点与它们的测试确实一个字
没改。**预判漏掉的一件事**：API 进程从来没有持有过任何 MCP client（MCP 至今只在 Task
Worker 里），所以还需要给它接一条连接的生命周期 —— 见 ADR-057 §3 与
`apps/api/dependencies.py` 的 `SandboxSlot`。

**做完的判据（已满足）**：2026-08-16 本地实跑，一次「写 primes.py 并运行它」的回合在
`sandbox_run` 上停下来等人，答 `approve_once` 之后才执行，报告里是真实的
`[2, 3, 5, ..., 47]`；对照组答 `deny` 时工具返回 `policy_denied`、没有执行任何代码，
回合继续并明说「这次我没有实际运行它」。

**尚未有的**：一条自动化的端到端测试。这条路径需要真实容器运行时，而 CI 的 `quality`
job 离线运行，所以它只能是本地证据（见 E-03）。

**后续（2026-08-16，[ADR-058](./adr/0058-the-sandbox-gate-moves-from-the-human-to-the-envelope.md)）**：
门本身的处置变了。上面实测的每次停下来等人，暴露的是这道门买不到同意（卡片上
只有摘要，ADR-054）却买得到延迟（两次 120s 批准耗光 240s 回合）。现在
`code.external_requires_approval` 默认 `false`——`external` 放行、`destructive`
继续上膛，要旧行为的部署一行配置拿回去。闸门机器仍然一个字没改。

### F-06 Chat 的侧栏仍是本地列表 —— 部分关闭

**Code 那一半已经关闭**（[ADR-047](./adr/0047-a-session-is-named-by-its-first-sentence.md)）：
`ConversationStore.list_sessions(tenant_id, principal_id, mode)` 存在，第一条指令
给会话命名，`PATCH /v1/code/sessions/{id}` 可以改名，`GET /v1/code/sessions` 返回
这份列表。清掉浏览器存储、换一台机器，列表都还在。
`web/src/features/code/storage.ts` 随之删除。

**Chat 那一半没关**：`web/src/features/chat/storage.ts` 仍然在 localStorage 里存
`LocalChatSession`，而那条记录带着 `answerMode` 和 `knowledgeBaseId`——服务端不建模
这两样。

**为什么不顺手做掉**：那是合并问题不是接线问题。切一半会得到两份互相矛盾的列表
（服务端有标题没有 answerMode，本地有 answerMode 但可能少了在别处开的会话），而
两份列表里总有一份是旧的。先要决定「answer mode 和知识库选择属不属于会话本身」，
那是产品决定。

**做完的判据**：Chat 的侧栏也来自 `list_sessions`，且 `answerMode` /
`knowledgeBaseId` 要么进了会话行、要么明确定为「每次打开重选」。

### F-07 步骤的延迟下限是一个轮询周期 —— 已知代价

**证据**：[sse.py](../src/agent_workbench/apps/api/sse.py) 的 `observe` 明确丢弃
`durability != "transient"` 的信封——持久事件**只**从重放路径出去，因为同一条事件
若两条路都走，会一次带位置、一次不带，客户端无法调和。于是持久事件到达订阅者的时间
下限就是 `event_stream.catchup_poll_seconds`，出厂默认 10。

**实测**：2026-08-15 在本机用真模型跑，一轮 5.4 秒的编码回合，浏览器里步骤面板
从头到尾是空的——不是没接上（`GET /events` 返回 200，live 的 ModelDelta 帧收到了），
是那一轮结束时第一次轮询还没到。同一个端点用 curl 订阅、跨过一个轮询周期就能拿到
RunStarted 及其后全部。

**为什么不是缺陷**：面板存在的理由是"一轮要跑几分钟，一个转圈和卡死分不开"。几分钟的
回合在 10 秒粒度下有十几次更新，那个问题是答得上的。5 秒的回合本来也不需要步骤反馈。

**已做的**：`config.code-local.toml` 把它降到 2 秒，代价是每个在线订阅者每秒多一次
事件日志查询，而该 profile 的 `max_concurrent_turns` 是 1。

**留着的口子**：任何打开 Code 的部署继承的仍是 10 秒默认值，得自己做这个权衡。
真正的修法是持久事件也能即时推送且仍带位置——那要动 `LiveEventChannel` 的契约，
需要先写 ADR。

### F-08 重新开启导出闸门的部署没有跨任务收件箱 —— 已知代价

**证据**：[ADR-048](./adr/0048-the-export-gate-is-off-by-default.md) 把
`workflow.export_requires_approval` 的仓库默认改成 `false` 并删掉了控制台的
「待我确认」页。`GET /v1/approvals` 仍然在服务、仍然有测试
（`tests/api/test_approval_api.py`），审批仍然可以在 Work 的任务详情里回答
（`WorkPage.tsx` 的 `ApprovalSection`）。

**代价**：一个把这个开关改回 `true` 的部署，得逐个 Task 去看谁在等，或者直接用
HTTP。没有一个「所有待我处理的事」的地方。

**为什么可以接受**：今天只有一种确认——允许生成并导出任务报告——而它天然长在
那个 Task 上。一个只有一种条目的收件箱，是一份和任务列表一一对应的列表。

**做完的判据**：出现第二种需要人回答的东西时（比如 Code 的 `sandbox_run`，见
F-05），收件箱重新长出来，而且那时它该是「所有待我处理的事」而不是「所有审批」
——Code 的审批走的是另一套注册表，两者的并集才是那个页面该显示的东西。

### F-09 评测和 Code 抢同一块内存 —— 已知代价

**证据**：[ADR-049](./adr/0049-an-evaluation-is-a-process-not-a-task.md) 让控制台
可以发起评测，`evaluation.max_concurrent_runs` 是冻结的 1。但那个 1 只约束评测：
一个正在跑的 RAG 消融（加载 BGE-M3）和一个正在跑的 Code 回合（模型循环 + 工作区）
之间**没有任何准入控制**，两者都会去拿同一台机器的内存。

**实测背景**：开发这个仓库的机器是 8 GB。一整轮消融 30–70 分钟，期间干别的重活
会双双被杀。

**为什么不修**：跨子系统的准入控制意味着一个进程级的信号量，横跨 Code、评测和
（将来的）任何重活。那是一套协调机制，而这个仓库对协调机制的态度写在 F-01 和
ADR-049 §2 里：只在它守着的东西比它本身更贵时才引入。今天它守的是"别在跑评测的
时候点发送"，那句话一个人就能记住。

**做完的判据**：出现第三种重活，或者部署到一台多人同时用的机器上——那时"记住别
同时点"不再是一个人能保证的事，信号量才开始比它的代价便宜。

### F-10 从界面发起一次评测会覆盖已提交的报告 —— 已知代价

**证据**：runner 直接往 `evals/*/reports/*.json` 写，那些文件是提交进仓库的证据，
`docs/` 里多处引用它们的数字。[ADR-049](./adr/0049-an-evaluation-is-a-process-not-a-task.md)
让控制台可以发起运行，于是点一下按钮就会改写它们。

**实测**：2026-08-16 验证「发起」这个功能时跑了一次 triage，
`evals/triage/reports/report.json` 的 accuracy 从 0.8333 变成 0.875——`unsure` 那 4 例
里多对了 1 个，同一个 gold digest、同一个模型、同样 24 个用例。那是运行间噪声，
不是改进。**那次改动没有提交**：让仓库记录的数字取决于「谁最后点过按钮」是错的。

**为什么现在不修**：三种修法各有代价。写到 `reports_root` 之外要改每个 runner 的
输出路径，而 runner 的输出路径也是它 docstring 里那条手动命令的一部分；写成带时间戳
的新文件要决定页面显示哪一份，而"最新"和"被引用过"经常不是同一份；只读挂载会让
手动运行也失败。都不是一行能改完的。

**眼下的做法**：跑完之后 `git diff evals/` 看一眼，是想留的就提交，是副产物就
`git checkout --` 掉。这句话写在这里，就是为了下一个人不用重新发现它。

**做完的判据**：一次从界面发起的运行不再修改被 git 跟踪的文件——要么写到别处，
要么每次运行有自己的目录，且页面明确说它显示的是哪一次。

### F-11 Code 工作区的 .docx 没有版面预览 —— 已知代价

**现状**：Work 页的 .docx 能看版面（`/v1/artifacts/{id}/pdf` 转换）和文字
（`/preview` 抽取），Code 工作区的 .docx 只能下载。图片、PDF、文本在两边都能
点开直接看（统一走 `previewKind`，见 `web/src/components/media.ts`）。

**为什么**：那两个转换端点按 artifact id 寻址，而 Code 的工作区列表故意不给
id（见 `client.ts` 里的注释：不让浏览器叫得出一个已被会话翻过去的版本）。给
Code 加 .docx 版面就要开一条按 session + name 寻址的第二条转换路，而
[ADR-0045](./adr/0045-a-layout-is-a-conversion-not-a-third-parser.md) 刻意把
转换收在 artifact 寻址一条路上。第二条路是边界变更，该有自己的 ADR，等真有人
需要再开。

**ADR-066 之后**：这条缺口的措辞变了，缺口本身没变。它现在是 `checkCost` 表里
Code 那一行的 `canConvert: false`——与 Work 的 `canRun: false`（`.py` 在那边跑不了）
对称的一格，而不再是两个界面里两句互不相识的注释。读者侧的变化只有一句话：
点开一个 .docx 不再和点开一个 .zip 得到同一句「这个类型只能下载」，而是被告知
版面预览在任务产出里有、以及为什么这里没有。**能看到的东西一点没多。**

**做完的判据**：Code 工作区里点开一个 .docx 能看到版面或文字，且新端点有 ADR
记录它为什么可以存在。

### F-12 HTML 预览的出网封锁是尽力而为，不是硬边界 —— 已知代价

**现状**：HTML 产物在 `HtmlPreview` 的沙箱帧里真实渲染
（[ADR-0062](./adr/0062-a-produced-page-runs-in-an-empty-origin.md)）。
**平台数据不可达是硬保证**：`sandbox="allow-scripts"` 不含
`allow-same-origin`，文档是 opaque origin，无 cookie 无 storage，对平台 API
的请求带不上身份头。出网（页面把自己的内容发往公网）只由注入的 meta CSP
封堵（`connect-src 'none'` 等），而 meta CSP 从解析点生效——恶意文档把脚本
放在注入点之前可以先行外联。

**为什么**：能把出网也变成硬边界的方案（服务端预览端点发真 CSP 响应头）
需要给 iframe 开一条新鉴权通道，并推翻 `routes/code.py` 与
`routes/artifacts.py` 两处在案的「单路径单行为」论证；ADR-0062 §3 记录了
这次权衡。威胁模型里页面能带走的只有它自己的内容——生成它的 Agent 本就
持有这些内容。

**ADR-066 新增的一条残余，写在这里而不是留给下一个人发现**：HTML 产物在轮次
卡片里**自动展开**（它是 `free`——展示即验收），所以上面这份「尽力而为」的出网
风险是在读者滚到那一轮时**自动付掉的**，不是点出来的。ADR-066 把那句安全提示从
iframe 下面移到了上面，让它在页面加载之前被读到；那改变的是读者何时知道，不是
这次加载是否发生。保留自动展开是有意的：一个网页不渲染就等于没产出。

**做完的判据**：预览页面发起的任意外联请求被拦截（无论脚本写在哪个位置），
且拦截层有自己的 ADR 说明鉴权通道怎么开。

### F-13 产出文件卡片预览的是那个名字此刻的字节 —— 已知代价

**现状**：一次工具调用写进工作区的文件名，由
`ToolCompleted.workspace_writes` 如实记着，**归属是准的、可持久重建的**
（[ADR-063](./adr/0063-a-produced-name-is-a-fact-not-a-sentence.md)）。但卡片
点开后取的字节走 `GET /v1/code/sessions/{id}/workspace/{name}`——按**名字**
寻址，拿到的是这个名字**当前**指向的字节。第三轮把 `report.md` 改写过，第一轮
那张卡片点开看到的就是第三轮的内容。

**为什么**：工作区是「名字可变、字节不变」（ADR-028），所以「那一轮当时的字节」
等价于「那一轮进入时的工作区版本」，而那是一个 artifact id。
`tests/architecture/test_a_workspace_version_is_never_asked_for.py` 专门扫描
路由参数、请求体字段与工具 schema 属性名，禁止任何入口接受
`workspace_version` / `manifest_id` / `workspace_manifest`：读写只按租户与
principal 划界，能点名版本的 principal 就能点到自己另一个会话正在中途改的
工作集。开一条按轮次寻址的读取路，等于先要回答那个授权问题——那是一份 ADR，
不是一个参数。

**ADR-066 之后更显眼，而不是更好**：产出卡片现在出现在更多地方（读者自己发起的
一次运行，它写出的文件就在运行结果下面），所以「预览的是这个名字此刻的字节」这条
性质被更多人看见。卡片上那句「第 N 轮又改过，预览的是最新内容」仍然是全部解法。
ADR-066 §2.8 顺带记下了这条缺口为什么挡着「记录已验收状态」这个想法：要记录就得
回答验收的是哪一份字节，而那等价于一个工作区版本 id。

**做完的判据**：从某一轮的产出卡片点进去，看到的是那一轮写下的字节；且新的
寻址方式有 ADR 说明它凭什么不违反上面那条架构测试的理由。

### F-14 Task 工作集里的文件 —— **已关闭**（ADR-088）

**现状**：[ADR-063](./adr/0063-a-produced-name-is-a-fact-not-a-sentence.md) 让一次
工具调用写进工作区的文件名成为结构化事实，**无条件**发布在
`ToolCompleted.workspace_writes` 上（在 `runtime.record_step_inputs` 门之外）。
Code 的控制台读它——轮次里的产出卡片就是它——而 **Work 一侧一行都没读**：
`web/src/features/work/` 三个文件里没有 workspace 字样。所以一个 Task 的节点往
工作集里写了什么，读者连「有这些文件」都不知道。侧栏列的是 artifact（按 id 可打开），
工作集里的文件不在其中。

**为什么不修**：把它们做成**可打开的**要给 Task 开一条工作区读取面，而工作区的
列举/读取/运行三条路全挂在 `/v1/code/sessions` 下并做 `mode="code"` 检查
（`application/code_session.py`）。复制一份等于第二套授权与第二条寻址，撞
`routes/artifacts.py` 那段「两个视图不能是两次鉴权」的论证；而 Code 模块被
`tests/architecture/test_code_has_no_coordination_plane.py` 与
`test_code_premises_are_frozen.py` 钉在「无协调面、在 API 进程内执行」上，复用它
等于把 Task 的产物塞进 Code 的前提里。

**已关闭的那一半（2026-08-18）**：`collectWorkspaceWrites`（`workTimeline.ts`）
从 `ToolCompleted.workspace_writes` 收名字、按 `graph_node_id` 归组，`ArtifactRail`
把它们作为第二组列出来——**刻意不是按钮**，并在标题下用一句话说明控制台打不开它们。
同一个名字被同一个 stage 写两次算一次，被两个 stage 各写一次算两条（后者覆盖了
前者，合并会把这件事藏掉）。空产物时那句「这个任务还没有产生文件」相应收窄成
「没有产生可以下载的产物」——工作集里躺着三个文件时，原来那句话是假的。

**另一半也关了（2026-08-28，[ADR-088](./adr/0088-a-working-set-entry-is-already-an-artifact.md)），
而关掉它的办法说明上面那段「为什么不修」的前提比它需要的强。**

上面写着「要给 Task 开一条工作区读取面」。那句话对**按名字取字节**的路成立，并且仍然
成立——所以那条路**没有造**。但工作集条目**存下来就是一件 artifact**：
`WorkspaceManifest.entries` 的类型是 `dict[WorkspaceName, ArtifactRef]`，而
`GET /v1/artifacts/{id}` 与它的预览早就存在、按 owner 鉴权，而且 owner 正是写入时记下的
那个 principal。

**缺的从来不是一条路，是「名字绑到哪个 artifact」这件事没有离开服务端。** ADR-088 让
`workspace_write` / `workspace_edit` 在写完之后把那条绑定发布在
`ToolCompleted.workspace_write_refs` 上（与 ADR-063 的名字同标准、同门外），控制台按
`filename` 配对，点开走的是产物那一栏同一个查看器、同一条鉴权。

**授权面的数量没变（一个），寻址方案的数量也没变（artifact id）。** 变的只是控制台知不
知道那个 id。

**关闭证据**：`task_319ceb4c…`，Work 页里 `redis-risks.md` 是一颗按钮，点开抽屉渲染出
全文，带下载与关闭。没有引用的行（ADR-088 之前写下的事件）仍旧只是名字、不可点——
向后兼容那一半单独有测试钉着。

### F-15 运行产出卡片的时序不一致 —— **已关闭**（2026-08-18）

**现状**：[ADR-066](./adr/0066-showing-is-not-checking.md) 把读者发起的一次运行写出的
文件渲染成卡片。构造卡片需要 listing 里的条目（媒体类型与字节数），而运行的响应与
listing 刷新是两条独立的异步路径：响应先到，listing 后到。所以在响应到达与列表刷新
之间的那一次渲染里，产出显示为今天那句纯文本「写回工作区：plot.png」，随后才变成
卡片。

**为什么是这个形状**：退化是全有或全无——只要有一个名字解析不出条目，整组都退回
句子。三个名字里两个是卡片、一个是文字，读者会读成第三个失败了。而按名字画一个
「已不在工作区」的死按钮，是对一秒前刚写出的文件说谎。

**怎么关的，以及为什么原来的判断只对了一半**：ADR-066 §7 判它「代价倒挂」，那条
判断针对的是**一种**修法——把 `written` 从名字列表改成结构化条目。那确实倒挂：
`SandboxOutcome.written` 是工具与路由共用的那一半（ADR-065 §3 明说必须共用），
工具侧直接把它喂给 `ToolResult.workspace_writes`（`tuple[str, ...]`），动它要连着
改领域类型。

还有一种更便宜的修法，而 ADR-066 没有考虑到：**让响应把跑完之后的整个工作区一起
带回来**。`RunFileResponse` 新增一个带默认值的 `files` 字段，路由在返回前对它已经
持有的 session 多做一次 `list`。这不碰 `written`，不碰 `SandboxOutcome`，不碰
`ToolResult`，也不碰任何领域类型——纯增量字段，旧客户端忽略它就是原来的行为。

**而且「整个工作区」比「written 那几个的条目」更对**，理由是 `PUT /workspace/{name}`
早就写下的那条：调用方的下一个问题永远是「现在里面有什么」。它还覆盖一种逐文件
列表覆盖不了的情形——脚本改写了一个它没报告的文件，那只会表现为这里的一处大小变化。

前端因此优先用响应自带的 listing，页面那份退化成 fallback（一个早于这个字段的服务端
不发它）。全有或全无的退化保留着，所以旧服务端上的行为一个字没变。

**证据**：`tests/api/test_code_api.py::test_a_file_the_script_wrote_lands_in_the_working_set`
钉住 `files` 里 `out.csv` 的 media_type 与 size_bytes；
`web/src/features/code/CodePage.test.tsx` 两条——页面 listing **永不刷新**时卡片
照样画得出来，以及服务端不发 `files` 时退回纯文本且不画死按钮。

### F-16 刷新之后历史里的答案不带引用标记 —— 已知代价

**现状**：[ADR-067](./adr/0067-a-cited-passage-is-a-new-read.md) 让一条引用可以点开
看原文，但只在**这次会话里发生过的那些轮次**上——`GET /v1/chat/sessions/{id}/messages`
返回 `StoredMessage`（role + text），引用躺在 `chat_turns.result` 这个 JSONB 列里，
两者之间没有路。所以刷新页面之后引用 chip 连同它新得到的展开能力一起消失，
`ChatPage` 里那句「历史记录只保存对话文本，不含引用与证据标记」仍然诚实。

**为什么不在 ADR-067 里一起做**：它不是接线。`ChatTurnStore`（`ports/conversation_store.py`）
在 ADR-067 之后有了一个按 id 的读；按**会话**列出轮次是第二个读方法，要两个适配器
实现加两套参数化契约测试，再加一次带真库的本地跑。而且它改变**一次历史读取披露的
内容**——今天读历史拿到的是文本，改完之后拿到的是文本加证据指针——那是一个该被
论证一次的决定，不是顺手加的字段。

**眼下的影响有限但真实**：读者在当前会话里能核对刚拿到的答案，隔天回来不能。

**做完的判据**：重新打开一个旧会话，历史里的答案带着引用标记，点开走的仍然是
ADR-067 那条新鲜鉴权的路（标记可以重放，原文不可以）。

---

### F-17 工具进度的预览有损 —— 已知代价

**证据**：[_bootstrap.py](../src/agent_workbench/apps/sandbox_mcp/_bootstrap.py)
（`MAX_PROGRESS_RECORD_BYTES` / `MAX_PROGRESS_TOTAL_BYTES`）、
[useCodeStream.ts](../web/src/features/code/useCodeStream.ts)（`KEPT_PROGRESS_LINES`）

ADR-069 把脚本自己 `print` 的东西接到了控制台上，但那是一份**预览**，三处有损：

| 有损 | 数字 | 到顶之后 |
| --- | --- | --- |
| 单条记录 | 2 KB | 拆成下一条 |
| 整次调用 | 64 KB | **静默停止**，不插标记 |
| 控制台窗口 | 8 行 | 更早的行滚出去 |
| UTF-8 解码 | 按字节块读 | 半个字符变 `�` |

一个打印 100 KB 的脚本，控制台会在某一刻**不再更新**，而界面上没有任何东西说它
停了——读者看到的是一个还在跳的秒数和一块不再变的输出。

**为什么算已知代价**：信封没有被截断。完整的两条流照旧按各自大得多的上限回到调用
方，落进 `ToolCompleted` 与工具结果里，读者点开那一步就能看到全部。丢掉的只是实时
预览的尾巴，而到那个体量时它早已不是任何人在读的东西（ADR-069 §4）。

**真要修的话**：静默停止那一处最值得先修——加一条「预览到此为止，完整输出见结果」
的记录，代价是一个常量和一行。另外两处是设计上的窗口，不是缺陷。

### F-19 computer use 的批准是进程级的 —— 已知代价

**证据**：[server.py](../src/agent_workbench/apps/computer_mcp/server.py)
（`create_server` 里 `gate` 被闭包捕获，一个进程一份）

`request_access` 批准的名单挂在 `ScreenGate` 上，而 `ScreenGate` 是
`create_server()` 建的——**一个服务端进程一份，不是一个 MCP 会话一份**。两个客户端
连同一个 `agent-computer-mcp`，第二个直接继承第一个批过的名单。

**为什么算已知代价**：这台服务端绑在回环上、由这台机器上的一个人使用，而屏幕本来
就只有一块——「两个互不信任的会话共用一块屏幕」这个场景在本部署形态下不存在。进程
重启即清空，所以它至少不会跨重启留下授权。

**真要修**：把 grant 挂到 MCP 会话上（`stateless_http=False` 已经开着，会话是存在
的），代价是 `create_server` 要从「一个 gate」变成「按会话取 gate」。

**2026-08-29 收窄一档（ADR-095 §4）**：这条缺口没有被修，但它现在**在界面上被说出来
了**。`GET /session` 答的是 `scope: "process"`，控制台那块面板照抄，写的是「批准挂在
这个进程上，进程一关就清空」。此前这一页的散文里到处是「这次会话批准了哪些应用」——
一块写着「会话」的面板会是第一个把会话级 grant 读进存在的地方，而那正是这条缺口还没
做的事。

### F-21 不可重试的 MCP 工具进不了 Task —— 拒绝

**证据**：[config.computer-local.toml:109](../config/config.computer-local.toml:109)
`retryable_effects = false`；两处拒绝各自独立生效——
[projections.py:155](../src/agent_workbench/bootstrap/projections.py:155)
把这类服务器的工具名排除在新 Task 的授权信封之外，
[composition.py:871](../src/agent_workbench/apps/task_worker/composition.py:871)
连绑定都不建，只留一行 `mcp_server_skipped_nonretryable`。

**`false` 的含义没变，变的是它的身份。** 此前它只是一句配置注释加两处 `continue`：
后果散在两个文件里，没有 ADR，也没有测试。本次把它记成决定（ADR-075），补了护栏，
补了测试。

**ADR-025 §2.7 给自己留的重开条件不成立。** 它写的是「真正的 exactly-once MCP 需要
远端幂等键，或让账本持久化并回放完整 ToolResult，另开工作包实现」——那两样补齐了也
不解锁任何东西，因为**挡路的是键，不是载荷**。`ToolBinding.operation_key` 是
`(ToolCall, ExecutionContext) -> str`，账本按 `(task_id, operation_key)` 找行；而一次
运行里没有任何东西能把「同一个意图被重放」和「一个新意图恰好长得一样」分开：
`graph_node_id` 被节点内所有调用共用，`agent_run_id` 每次恢复重铸，`lease_epoch` 每次
回收改变，`tool_call_id` 每轮重铸——仓库里唯一那把上账键为此写明自己**不能**由
`tool_call_id` 派生（[export_artifact.py:104](../src/agent_workbench/adapters/tools/export_artifact.py:104)）。
两种派生法各有反例：

1. **按参数派生**会把合法的第二次相同点击折叠进第一次的存档结果，而
   [agent_runtime.py:249](../src/agent_workbench/runtime/agent_runtime.py:249)
   的 `MAX_IDENTICAL_CALLS = 3` 是**故意**允许一次运行里出现三次相同调用的。
2. **按位置派生**（节点内第 n 次上账的调用）设计过，因正确性被否：它毁掉账本的重试
   身份。一次在位置 5 记为 `intended` 的点击，其 Worker 死掉后由重放的模型在位置 6
   重新提出，拿到**新键**，于是被做第二遍——正是账本存在的理由。

**只读的那一半也不成立，理由与幂等无关**：模型没有视觉通路。
[messages.py:83](../src/agent_workbench/domain/messages.py:83) 的 `ContentBlock` 是
`TextBlock | ToolUseBlock | ToolResultBlock`，没有图像成员，而 `map_remote_result` 把
每个 `RemoteBinaryBlock` 都送去 artifact。放进来的 `screenshot` 交到模型手里的只是一句
分辨率——那是让 Agent 蒙着眼睛开界面。

**本次落地的护栏**：[tool_gateway.py:296](../src/agent_workbench/runtime/tool_gateway.py:296)
的 `advertise` 对任何带 `operation_key` 的绑定抛 `PolicyDeniedError`——「这个工具记录
外部副作用，由图节点发起，永远不摆到模型面前」（`unknown_tool` 留给进程根本没注册的
名字，两个码分得清「没有这个工具」和「有但不给模型」）。同一条规则在装配期还有一道：
[composition.py](../src/agent_workbench/apps/task_worker/composition.py) 的
`_assert_no_profile_offers_a_ledgered_tool` 让一个把上账工具写进 profile 的部署起不来，
而不是每个 Task 挂一次。它今天**拒不到任何东西**（没有 profile
声明上账工具），而这正是预期形状：它替下的是一条**意外**的护栏——在 trace 带上 lease
epoch 之前，模型提出的上账工具都会因为拿不出栅栏而在更深处被拒，看起来像决定，其实
是遗漏。`export_artifact` 不受影响，它从不过 `advertise`。测试在
[test_tool_gateway_ledger.py:640](../tests/runtime/test_tool_gateway_ledger.py:640)
三条，以及
[test_local_computer_profile.py:75](../tests/config/test_local_computer_profile.py:75)
的「没有任何屏幕工具进得了 Task 授权信封」。

**替代的重开条件**（ADR-075 用它换掉 ADR-025 §2.7 那条）：一个不可重试的 MCP 工具
进入 Task 的唯一方式，是**由一个确定性节点自己发起这次调用**，像
[task_export.py](../src/agent_workbench/adapters/tools/task_export.py) 那样，而不是由
模型提出。要把它摆到模型面前，还额外需要一条今天不存在的视觉通路。

**做完的判据**：不适用。要改，先改 ADR-075。

## 优先级建议

按"单位工作量能消除多少不可核查性"排序，而不是按功能大小。

1. **E-04 给 manifest 接一个消费者**。~~生成首份 manifest~~ 已经生成过两份，其中一份
   干净、带附件、今天仍能 `verify` 通过——**上一版说"从没跑过一次"是不实的**。缺的是
   它不进版本控制（`artifacts/evidence/` 被 gitignore）、不进 CI、不被任何文档引用，
   所以它无法让任何一句过期的话失败。**它仍然是 E-05 唯一的根治手段**，但修法是
   "被消费"，不是"跑一次"。**方向已定：manifest 不进版本控制**，因此 CI verify 那条路
   被排除，剩下的唯一判据是让 HIGHLIGHTS §2 的数字**由 manifest 生成而不是手抄**。
2. **A-03 重跑等价评测**。它同时是 A-01 能否打开的前置条件。约 30–70 分钟机器时间。
3. **B-05 第一条真实 upcaster**。升级链至今没在真实数据上走过一次，机制是否真的
   接得上仍未被证明。
4. **A-07 决定候选漏斗的去留**（2026-08-31 新登记）。`[rag.retrieval]` 的四个 `top_k`
   没有读者，而[配置说明](./configuration.md) §8 把它们写成了生效的上限。口径那一半
   本次已修，剩下的是二选一：接线，或从 schema 里删掉。两条都要先有 ADR——它们把
   "配置是契约"往相反方向解释。排在这里是因为它便宜，且**每天都在误导读配置的人**。
5. **B-02 watchdog 的 abort 半**。warn 半已在 API 进程里跑着，剩下的是判定与停止
   claim；同时把它装进 Task Worker。
6. **B-01 SSE 那半消费端**。边界清楚，可对真 PostgreSQL 验证，且不改变正确性论证。
7. **E-08 重排能力阶梯**（2026-08-31 新登记）。排在末位不是因为不重要，而是因为它
   **应当在 E-04 之后做**：阶梯要的是逐格可链接的证据，而 evidence manifest 正是产生
   那种证据的机制。反过来做，等于再手抄一遍会过期的表。

**上一版这份排序自己过期了，登记在此。** 它的第 4 项是"C-01 调用账本，它是 C-02 三项的
共同前置"，而 C-01 在表里已是**已关闭**，C-02 也已收窄成"仅 partial failure 未做"——
也就是说，**一份用来决定"下一步做什么"的清单，推荐了一件已经做完的事**。这比数字过期更贵：
数字过期误导的是判断，排序过期误导的是行动。它与 E-05 同因，也同样只能靠 E-04 治。

其余条目（D 组大部分、C-03、C-04、B-03）需要新的 Adapter 或新的产品决策，
不适合在证据链补齐之前动。

**C-05 不在上面这个排序里，但它挡着一件别的事**：v1 图目前跑不到终点——
2026-08-13 的实测里，`understand → plan → research_external → synthesize` 全部
通过，停在 `critic`。上面这份排序问的是"哪一步最能消除不可核查性"，而 C-05 问
的是"这条链能不能走完一次"。两者不冲突，但如果需要一次 v1 的端到端演示，它是
唯一的拦路条目；Chat 那条链路不受影响。（此处此前还有一句"B-06 则决定同一条链遇到
抖动时是不是必然失败"——**B-06 已关闭**，那句连同它描述的风险一起过期了，本次删去
而不是留着。）

**一条排序上的更正**：上一版把"E-05 刷新过时数字"排在第 2 位，当作一件可以做完
的机械工作。它不是——本文档自己写着数字过时是持续现象。把它当任务排期，等于每次
基线变动都重排一次同一件事；真正该排期的是 E-04。

---

## 维护规则

- 一条缺口关闭时，**从本文档删除**并在 [status.md](./status.md) 记录，不要在这里
  留"已完成"的条目——那会让本文档退化成第二份状态文档。
- 新增缺口必须带仓库位置。没有位置的条目不许写进来。
- "口径不实"类不排期。发现即修，修完在本次提交里连带更正本文档。


### F-24 项目目录的回合没有容器可用 —— 拒绝

**证据**：[code_session.py](../src/agent_workbench/application/code_session.py)
`CODE_PROJECT_TOOLS_WITH_RUN` 是项目侧唯一带可选工具的元组，里面没有
`sandbox_run`；[dependencies.py](../src/agent_workbench/apps/api/dependencies.py)
`_code_project_tools` 不再读 `code.sandbox_enabled`；不变量由
`_assert_project_tuples_enter_their_own_scope`（import 时）与
`tests/adapters/test_project_tools.py::TestExclusivity::test_no_project_tuple_offers_a_tool_bound_to_the_flat_workspace`
两处钉住。

**为什么**：在此之前项目回合是**被提供**了 `sandbox_run` 的，而它一次也不可能成功。
`SandboxRunTool` 持有的是扁平的 `WorkspaceScope`，从 ContextVar 里取会话；而
`CodeSessionService.run` 的 `ExitStack` 只进入一个 scope，项目回合进的是
`ProjectFileScope`（ADR-073「只进入一个」）。所以每一次调用都在碰到沙箱之前就抛
`SandboxUnavailableError`，模型收到的是 `unhandled SandboxUnavailableError`。在
`config.demo-local.toml` 下每个会话都有项目，也就是**每一次**调用。

删掉这个提供，是把"不能用"从一次浪费掉的回合改成一句在装配期就成立的话。让容器真正
看得见项目目录是另一件事：它要动 [ADR-029](./adr/0029-ephemeral-sandbox.md) 逐行论证过的
`ISOLATION_FLAGS`，那是能力变更，要自己的 ADR 和自己的证据。

**做完的判据**：一份 ADR 回答「容器挂载用户真实目录之后，`--network=none` 之外还剩
哪些隔离保证」，并给出一次真实运行的证据。在那之前，项目回合就是没有容器——
`code.sandbox_enabled` 只对扁平工作区的会话有意义。

### F-25 读写回执喂不满 —— 已知代价

**证据**：[ADR-078](./adr/0078-a-file-you-have-not-read-is-not-yours-to-overwrite.md)
§3。[file_read_receipts.py](../src/agent_workbench/application/file_read_receipts.py)
只在 `ProjectReadTool`／两个写工具里被写入，而目录有三条不经过这些工具的写入路径：
`PUT /v1/projects/{project_id}/file`（[projects.py](../src/agent_workbench/apps/api/routes/projects.py)，
走同一个 `store.write` 且**不带**前置条件）、用户自己的编辑器与 `git`、以及
`project_run`（能改根下任何东西）。

**为什么不修**：三条里只有 `project_run` 是可归因的，它已经被
`ReadReceipts.note_command_ran()` 记下并换一句拒绝措辞。另外两条是**用户在动自己的
文件**——给控制台的 `PUT` 加前置条件是让用户跟自己赛跑，没有意义。

后果是回执会看见它没造成的合法 mtime 移动：模型被拒一次、重读一次、再写。
**这是选定的代价**，不是没做完。多一次读换掉一次静默的数据丢失；反过来的错误——闸
放行了一次真该拦的写——没有第二次机会，因为被覆盖的字节没有版本可退。

**这一条不会有"做完"的一天。** 它不是待办：任何声称"回执覆盖了所有写入路径"的说法
都是假的，除非目录变成一个只能经由本进程写入的东西——那是另一个产品。

**2026-08-27：同一条边界现在还管着目录树。**
[ADR-086](./adr/0086-a-produced-file-is-not-answerable-to-which-store-it-landed-in.md)
让 `project_write`／`project_edit` 发布 `project_writes`，控制台据此让文件树在回合中
自己刷新、并把这段会话写过的行标出来。它跟的是**记账过的**写入，所以上面那三条路径
同样不在其中。界面上的措辞因此是"这段会话写过它"——一句它答得出的话——而不是"这是目录
当前的样子"。**不要**把这个标记或这次刷新说成"树是活的"。

### F-26 `policy.write_tools_require_approval` 无人读取 —— 口径不实

**证据**：[settings.py:927](../src/agent_workbench/bootstrap/settings.py) 是
`write_tools_require_approval: Literal[True] = True`，
[config.default.toml:407](../config/config.default.toml) 写着 `true`，而
`rg write_tools_require_approval src/` 在这两处之外**零命中**。同时
[code_session.py](../src/agent_workbench/application/code_session.py) 的
`approval_required_risks` 只有 `("destructive",)`，`project_write` 与
`workspace_write` 都是 `write` 风险——**按构造，写工具不停在任何人面前**。

**为什么算口径不实而不是未实现**：这个名字是一句读起来像保证的话。一个读配置的人
会得出"写文件要人批准"，而事实相反。这正是
[ADR-077](./adr/0077-a-command-on-this-machine-is-shown-before-it-is-run.md) 的
settings 注释点名的"最贵的一种不变量：读起来是保证，实际是注释"，也是
[ADR-059](./adr/) 删掉 `node_retry_max_attempts` 的同一种形状。

**为什么 ADR-079 没有顺手接上它**：plan mode 不是"写入停在人面前"，它是"这一轮没有
写工具"。把这个字段接到 plan mode 上，只是让一个名字在新位置上继续承诺一件它不做的
事。见 [ADR-079](./adr/0079-a-plan-is-not-an-authorization.md) §6。

**做完的判据**：二选一，都要一次决定。要么接上一道真的 `write` 审批闸——那要回答
"每一次写都停下来的回合还能不能干完活"，以及它与 ADR-078 的读写回执如何分工；要么
像 ADR-059 那样把字段删掉，那是一次 `config_schema_version` 变更，应当与下一次
schema 变更合并，不单独 bump。

**2026-08-27 更新，收窄但不关闭。**
[ADR-087](./adr/0087-a-session-may-be-stricter-than-its-deployment.md) 把闸接上了：
`CodeApprovals = "before_write"` 让这一轮的每一次写入停在人面前，`code_approval_risks`
只加不减，界面上是发送框旁边三档里的中间那一档。上面那个悬而未决的问题——"每一次写
都停下来还能不能干完活"——的答案是**不该由部署替所有人回答**，它是一次一回合的选择。

**但这一条不关。** 缺口是这个**字段没有读者**，而 ADR-087 加的是一条**请求体上的**
轴。`policy.write_tools_require_approval` 读起来仍然是"这个部署要求写入审批"，而它仍然
不影响任何一次调用。收窄后的判据只剩一件事：把它接成 `code_approval_risks` 的**地板**
（`base` 在它为真时含 `"write"`，会话仍然只能在其上加），这需要值域从
`Literal[True]` 变成 `bool`——一次 `config_schema_version` 变更，仍然应当与下一次
schema 变更合并。

### F-27 项目目录一侧只看得了文本 —— **已关闭**（2026-08-27，同批）

**曾经的证据**：`GET /v1/projects/{project_id}/file`
（[projects.py](../src/agent_workbench/apps/api/routes/projects.py)）返回的是
`ProjectFileContent`：`{path, text|null, size_bytes, is_text, modified_at}`
（[ports/project_files.py](../src/agent_workbench/ports/project_files.py)）。整个
projects 路由里没有 `StreamingResponse`——**项目目录里的文件取不到字节**。所以
[ADR-086](./adr/0086-a-produced-file-is-not-answerable-to-which-store-it-landed-in.md)
让项目侧用上了工作区那套查看器之后，`html` 与文本能看，`image` 与 `pdf` 仍然停在
「这是一个二进制文件（N 字节），不显示内容。」

**做完的样子**：`ProjectFileStore.open_bytes(path)` 返回 `(entry, AsyncIterator)`，
新路由 `GET /v1/projects/{project_id}/file/bytes` 把它接成 `StreamingResponse`，
前端 `getProjectFileBlob` 喂给已有的 `BlobPreview`。

三件值得单独记下的：

1. **流式，因此没有上限**。整份读进内存就得有一个数，而任何为它挑的数在别人真实的
   目录上都是错的。流式不需要：字节从不同时全部存在。控制台确实会在请求之前拒绝大
   文件（`BlobPreview` 按目录列表里的字节数判断），但那是一次交互上的客气，**不是**
   边界——一个只因为客户端有礼貌才活着的服务端根本没有边界。
2. **`open_bytes` 不是 `async def`**，和 `ArtifactStore.iter_chunks` 一样。所有拒绝
   （路径检查、符号链接叶子、文件不存在、是个目录）必须在**调用时**发生，那时路由还
   能改状态码；只在第一个 chunk 才失败的写法是一个中途停下的 200，客户端分不清它和
   断线。
3. **分岔发生在读之前**。图片按名字直接走字节那条路线，不先走一遍文本读——后者会把整
   份字节读进来解 UTF-8，对一张 PNG 只为了得到 `is_text: false`，而超过
   `MAX_READ_BYTES` 的图片会直接撞成一条错误。

**没有重开 ADR-062 §3**。那条被拒的是一个**当 `iframe src` 用**的服务端预览端点，
沉掉它的是鉴权：嵌入元素不发身份头，所以要另开一次性 token 或同源 cookie——为一层纵深
新增一条进入这个 API 的路。这条路由由 `BlobPreview` 用普通方式取，带着其它调用一样的
身份头，字节在页内变成 object URL。没有新鉴权通道，授权和其它项目路由完全相同；
`attachment` + `nosniff` 无条件带上（ADR-062 §4）。

**服务端不猜 media type**，一律 `application/octet-stream`。项目文件就是磁盘上的一个
文件，没人给它标过类型，这里编一个是这套 API 站不住的断言——而在 `nosniff` 之下，它
恰好也是浏览器唯一不会再质疑的那个断言。显示成什么由控制台按名字决定
（`effectiveMediaType`），那是一个显示决定，没有任何授权读它。

**一处此前写错的成本估计**：这条缺口原先写着「要过一遍 `tests/contracts/` 的参数化
套件」。不对——`tests/contracts/test_projects.py` 是 `ProjectStore`（归属与成员关系）
的，`ProjectFileStore` 只有一个实现，测试在 `tests/adapters/test_project_file_store.py`。
真实成本比原先记的小，[ADR-086](./adr/0086-a-produced-file-is-not-answerable-to-which-store-it-landed-in.md)
§4 里同一句话也一并更正。

### F-28 Code 的文件预览不渲染 Markdown —— **已关闭**（2026-08-27，同批）

**曾经的证据**：`text/markdown` 以 `text/` 开头 → `isReadableMedia` 为真 →
`previewKind` 返回 `"text"` → `FilePreview` 落进 `TextPreview` 的 `<pre>`。而
`MarkdownContent` 是存在的，chat、Code 的**报告**、Work 的产物面板都在用它——唯独没有
接进文件预览。项目侧走同一张分派表，所以两侧一致地都不渲染。

**做完的样子**：新增 [MarkdownPreview](../web/src/components/MarkdownPreview.tsx)，
props 与 `TextPreview` 完全相同（`load` + `queryKey`），所以两个调用方都是一行替换，
两个查看器也不可能在取数和缓存上分叉。默认渲染，源码在一个与 `HtmlPreview` 同形的
`aw-segmented` 切换后面；被截断的文件不渲染（理由同 `HtmlPreview`：半份文档画出来是
它从来不是的样子，却被当成产物）。

**没有加第六个 `PreviewKind`**，这是 ADR-065 §4 拒绝过的形状：`previewKind` 是每一个
展示文件的界面共用的词表，而「怎么画」是只有其中一部分能回答的问题。Markdown 因此是
text 臂里的第二问——`isMarkdown(...)`，紧挨着 `isRunnablePython(...)`，同一个形状同一
个理由。`isMarkdown` 从 `features/work/preview.tsx` 提到了
[components/media.ts](../web/src/components/media.ts)：一份谓词，因为答案不该取决于
是哪一页在问。

**Work 那一侧保持只渲染、不给切换**，而这不是漏做。Code 多出来的那个 源码 档，与
ADR-065 §4 记下的「运行按钮只在 Code 有」是同一种不对称：**编码**控制台里一份 `.md`
既可能是要读的文档、也可能是刚被写出来正要被检查的文件；Task 的产物面板里没有要编辑
的东西，也就没有要切过去的东西。
### F-29 只能激活已经开着的应用 —— **拒绝**

**证据**：[screen.py](../src/agent_workbench/ports/screen.py) 的 `activate` 契约写明
「An implementation **launches nothing**」，返回 `None` 表示「没有任何在运行的应用带这个
bundle id」；[darwin.py](../src/agent_workbench/adapters/screen/darwin.py) 只调
`NSRunningApplication.runningApplicationsWithBundleIdentifier_`，全仓没有
`launchApplication` / `openApplicationAtURL` 的调用；
[computer.py](../src/agent_workbench/domain/computer.py) 的
`application_is_not_running` 是那条拒绝的文案，
`tests/apps/test_computer_gate.py::test_an_approved_application_that_is_not_running_is_not_launched`
是它的回归。

**这是拒绝，不是遗漏。**
[ADR-091](./adr/0091-choosing-a-window-is-choosing-within-a-set-somebody-approved.md) §4
是它被论证的地方：Claude Desktop 的 `open_application` 会启动应用，这里的
`activate_application` 不会，工具名也因此不一样。启动一个进程和把窗口重排不是一个量级
的行为——它会执行那个应用启动时做的任何事（同步邮箱、恢复上次的文档、连服务器），而且
撤不回；更要紧的是**人批准的那份名单不是这个意思**：对话框问的是「可以在这次会话里
控制下列应用」，一个人看着「Notes、Word」点同意，同意的是控制它们。

**代价是真的**：两个应用必须都已经开着，任务才跨得过去。开着一个、另一个没开的时候，
模型收到的是一句「approved but not running，请人打开它」，而不是一次启动。

**做完的判据**：不是「实现一次启动」，是**先回答同意怎么给**。要么对话框上多一档人能
读懂的授权（「并允许在需要时打开它们」——那是一次 `consent.py` 的改动加一条它自己的
ADR 段落），要么维持现状。在人没有被问过这件事之前，实现它就是拿一份为别的问题收来的
同意去付一件更贵的事。

### F-31 第一轮没法收窄工具 —— 已知代价

**证据**：[ComposerMenu.tsx](../web/src/features/code/ComposerMenu.tsx) 里「工具」那一
栏挂在 `sessionId === undefined ? [] : …` 后面；目录的路由是
`GET /v1/code/sessions/{session_id}/tools`（[code.py](../src/agent_workbench/apps/api/routes/code.py)），
按会话寻址；`CodePage` 的 `send()` 在没有会话时先 `createCodeSession` 再 `askCode`，
而那两步之间没有人可以读一次目录再表态。

**为什么是这个形状。** offer 里有一件事是**关于这段会话**的——它的项目有没有登记目录
（ADR-073 的两套文件语言不相交）。起始屏上项目已经选好了，所以那个答案原则上算得出来，
但它此刻没有一个 id 可以被问。三条路都不划算：给路由加一个「按项目问」的分支（同一个
问题两种寻址，而其中一种答的是一个还不存在的东西）；起始屏先 POST 一个空会话（
ADR-047：第一句话才是给会话命名的东西，一个凭空点出来的会话会永远无名地躺在列表里）；
或者前端按 profile 猜一份（这正是 ADR-096 §8 拒绝的那条）。

**代价是真的，而且小**：第一轮跑在这个部署的完整 offer 上。第二轮起菜单就在，勾选
按会话记在 `localStorage` 里。计划模式与写入前批准在第一轮**就**可用——它们不需要
目录，所以「第一轮完全没法收窄」并不成立。

**做完的判据**：不是「让起始屏也能勾」，是先回答**它在问谁**。一条按项目寻址的
`GET /v1/code/projects/{id}/tools` 是可以成立的，但它得先说清自己答的是「一个还不
存在的会话的下一轮」——那是 ADR-096 §2 那条时态线的第三次划，值一段自己的 ADR。
