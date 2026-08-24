# 已知缺口

截至 **2026-08-15**，配置 schema `1.15`，Alembic 迁移 28 个
（head `0027_session_workspace_version`）。本文档各条的代码位置核对于 `main@921dda5`。
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
| D-06 | Chat 历史 compaction | 未接线 |

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
| F-14 | ~~Task 工作集里的文件在界面上完全不可见~~ 名字已列出，仍打不开 | **部分关闭** |
| F-15 | ~~运行产出卡片对 listing 时序有可见的不一致~~ | **已关闭** |
| F-16 | 刷新之后，历史里的答案不带引用标记 | 已知代价 |
| F-17 | 工具进度的预览有损：单条 2 KB、整次 64 KB，到顶静默停止 | 已知代价 |
| F-18 | computer use 的截图不排除未批准的窗口，且抓屏是遮盖不是合成器过滤 | **未实现** |
| F-19 | computer use 的批准是进程级的，不是 MCP 会话级的 | 已知代价 |
| F-20 | 跨产品归属的三处数据没人再读写（ADR-074 之后） | 已知代价 |
| F-21 | 不可重试的 MCP 工具（点击、截图）进不了 Task | **拒绝** |

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
`code.sandbox_requires_approval` 默认 `false`——`external` 放行、`destructive`
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

### F-14 Task 工作集里的文件 —— **部分关闭**（列出来了，仍打不开）

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

**仍未关闭的那一半**：打不开。理由不变（上一段），需要一份 ADR 回答 Task 的工作区
读取面凭什么可以存在。

**做完的判据**：Work 页里点开一个工作集文件能看到它的内容，且新端点有 ADR 记录它
为什么不构成第二套授权与第二条寻址。

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

### F-18 computer use 的截图不排除未批准的窗口 —— **未实现**

**证据**：[gate.py](../src/agent_workbench/apps/computer_mcp/gate.py)
（`ScreenGate._to_exclude` 恒返回空）、
[darwin.py](../src/agent_workbench/adapters/screen/darwin.py)
（`capabilities()` 返回 `exclude_mask`）

ADR-070 的门禁在**输入**侧是完整的：没批准的应用点不了、打不了字。**输出侧不是。**
一次 `screenshot` 抓的是整块屏幕，未批准应用的窗口如果在上面，它就在图里。

两处独立的欠缺：

1. `_to_exclude()` 恒返回空元组。按 bundle id 排除需要「当前有哪些应用在跑」这份
   清单，而 `ScreenPort` 没有暴露它——**故意没有**：给模型一个枚举所有运行中应用的
   能力，比它被拒绝的那个能力有意思得多。要补的是一条更窄的路：从 grant 列表反推
   「不在名单里的窗口」，而不是先列举一切。
2. 就算列表有了，本机适配器给的也只是 `exclude_mask`——抓完整帧再把矩形涂黑。
   ScreenCaptureKit 的 `SCContentFilter` 能在**合成器层**把窗口挡在帧外（像素从来
   不存在），pyobjc 对它的覆盖不完整，本批没做。两者是不同的承诺，`capabilities()`
   把它们分开命名就是为了让调用方能区分，而不是让它们看起来一样。

门禁里那条「平台排不掉就拒绝截图」的分支已经接好了（`test_a_platform_that_cannot_
exclude_is_refused_rather_than_trusted`），今天走不到——因为要排除的列表是空的。

**为什么算未实现而不是已知代价**：这是 ADR-070 自己的模型没有兑现的一半。输入侧
说「没批准就碰不到」，输出侧现在是「没批准也看得到」。

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

### F-21 不可重试的 MCP 工具进不了 Task —— 拒绝

**证据**：[config.computer-local.toml:95](../config/config.computer-local.toml:95)
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
