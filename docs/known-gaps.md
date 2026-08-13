# 已知缺口

截至 **2026-08-12**，配置 schema `1.14`，Alembic 迁移 25 个
（head `0025_agent_invocation_count`）。本文档各条的代码位置核对于 `main@921dda5`。
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
| B-01 | SSE 回放仍在轮询（Task Worker 那半已接上） | 未接线 |
| B-02 | Watchdog 只有 warn 一半，且未装进 Task Worker | 未实现 |
| B-03 | 七点故障矩阵只覆盖四点 | 未实现 |
| B-05 | 生产 upcaster 注册表为空，Chat 侧不披露隔离 | 未接线 |
| B-06 | 失败标着 `retryable` 却没有任何重试路径 | 未接线 |
| B-07 | tool 参数读不出来时，说不出是被截断还是真的坏 | 未实现 |

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

### B-06 失败标着 `retryable` 却没有任何重试路径

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

---

## C. Multi-Agent

| 编号 | 缺口 | 分类 |
|---|---|:---:|
| C-01 | 调用次数上限只有配置，无持久账本 | 未接线 |
| C-02 | 跨 retry 预算、partial failure、父子取消 | 未实现 |
| C-03 | 动态 supervisor / spawn / mailbox | 未实现 |
| C-04 | CrewAI Adapter 与对比 benchmark | 未实现 |
| C-05 | `critic` 的合法结构化输出被判成"没有可用产出" | 未实现 |

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

**尚未查清**：是解码契约缺失本身，还是 `decision: "revise"` 在 `revision_number: 0`
时没有可走的修订回边。两者都会以同一条消息收场，本次没有继续区分。

**顺带记下**：同一次运行里 `critic` 给出的理由是草稿"未提供任何实际内容，仅包含
任务指令的重复"——即 `synthesize` 那步的产出质量也有问题。这是另一件事，本条不
覆盖。

**做完的判据**：`plan` 与 `critic` 有一份写下来的解码契约（读什么字段、字段缺失
怎么办、解不出时的纠正轮走几次），并有一条测试：喂一份合法裁决进去，节点必须
把它变成状态而不是失败；配一条对照组，喂一份真正解不出的输出，确认它才是失败。

---

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

## 优先级建议

按"单位工作量能消除多少不可核查性"排序，而不是按功能大小。

1. **E-04 生成首个 evidence manifest**。工具已在，成本最低，收益是把此后所有
   数字从散文变成可校验的引用。**它同时是 E-05 唯一的根治手段**——数字收敛成
   单一来源只是缩小复发面积，让"过时"在 CI 里失败才是修复。
2. **A-03 重跑等价评测**。它同时是 A-01 能否打开的前置条件。约 30–70 分钟机器时间。
3. **B-05 第一条真实 upcaster**。升级链至今没在真实数据上走过一次，机制是否真的
   接得上仍未被证明。
4. **C-01 调用账本**。它是 C-02 三项的共同前置。
5. **B-02 watchdog 的 abort 半**。warn 半已在 API 进程里跑着，剩下的是判定与停止
   claim；同时把它装进 Task Worker。
6. **B-01 SSE 那半消费端**。边界清楚，可对真 PostgreSQL 验证，且不改变正确性论证。

其余条目（D 组大部分、C-03、C-04、B-03）需要新的 Adapter 或新的产品决策，
不适合在证据链补齐之前动。

**C-05 不在上面这个排序里，但它挡着一件别的事**：v1 图目前跑不到终点——
2026-08-13 的实测里，`understand → plan → research_external → synthesize` 全部
通过，停在 `critic`。上面这份排序问的是"哪一步最能消除不可核查性"，而 C-05 问
的是"这条链能不能走完一次"。两者不冲突，但如果需要一次 v1 的端到端演示，它是
唯一的拦路条目；Chat 那条链路不受影响。B-06 则决定同一条链**遇到抖动时**是不是
必然失败——那三次真实失败全部是可重试的网络错。

**一条排序上的更正**：上一版把"E-05 刷新过时数字"排在第 2 位，当作一件可以做完
的机械工作。它不是——本文档自己写着数字过时是持续现象。把它当任务排期，等于每次
基线变动都重排一次同一件事；真正该排期的是 E-04。

---

## 维护规则

- 一条缺口关闭时，**从本文档删除**并在 [status.md](./status.md) 记录，不要在这里
  留"已完成"的条目——那会让本文档退化成第二份状态文档。
- 新增缺口必须带仓库位置。没有位置的条目不许写进来。
- "口径不实"类不排期。发现即修，修完在本次提交里连带更正本文档。
