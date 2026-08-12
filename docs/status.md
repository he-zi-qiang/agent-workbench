# 实施状态

这份文档记录**做成了什么**，逐次增量累积，**倒序排列**——最新的在最上面。
每一节保留它写下时的时点与当时的门禁数字，**不随主线刷新**：一次增量的证据属于
它自己的那棵树，倒写会让证据链失去意义。

**因此本文档里的数字不是当前值。** 当前门禁只在
[十分钟版本的门禁与规模一节](./HIGHLIGHTS.md#2-门禁与规模)维护一份。

## 怎么用这份文档

| 你要找 | 去哪 |
|---|---|
| **某项能力现在存不存在** | 先看[已知缺口](./known-gaps.md)——它按"拒绝／未接线／未实现／口径不实"给出当前判定；本文档给的是历史证据 |
| **某项能力是怎么做成的** | 在本文档搜该能力的名字，读它那一节的"证据"与"对照组" |
| **当前门禁数字** | [十分钟版本 §2](./HIGHLIGHTS.md#2-门禁与规模)，不在本文档 |
| **某个决定为什么这样做** | [ADR 索引](./adr/) |
| **更早的审计快照** | [历史归档](./archive/) |

**阅读约定**：标注"未合并"的小节记录的是当时工作分支上的状态，其中一部分此后已
合入主线；判断是否已在主线，以该能力在[已知缺口](./known-gaps.md)中的分类为准。

---

## 2026-08-11 第三批（进行中）：停摆的 Worker 不再替自己作证

第二批清的是"服务端知道、界面不说"。这一批是前两批**明确排除**的那条——
durable Agent 预算、event-loop watchdog、阻塞 Adapter 隔离——排除理由是"架构改动，
要先有 ADR"。所以这一批先产出了三份 ADR（[ADR-040](./adr/0040-a-task-pays-before-it-calls.md)、
[ADR-041](./adr/0041-a-late-heartbeat-may-not-renew.md)、
[ADR-042](./adr/0042-blocking-belongs-to-the-adapter.md)），再按 ADR 逐刀实现。

**这一节还没写完**，下面只记已经落地的部分。

### 一、迟到的心跳没有资格续租（ADR-041）

对着代码核完 WP08-12 之后，结论和计划文档写的不一样：**坏掉的不是"缺一个
watchdog"。**

`_heartbeat_loop`（`workers/task.py`）此前是 `sleep` 完就无条件续租，
**一个字都不检查自己迟到了多久**。而续租那条写走的 fence
（`task_registry.py:775` 的 `_live_lease_conditions`）对时间的唯一要求是
`lease_until > now()`。于是这条路径可达（默认 `lease_seconds=90`、
`heartbeat_seconds=20`）：

| 时刻 | 发生了什么 | `lease_until` |
|---|---|---|
| t=0 | `claim_next` 设租约 | 90 |
| t=20 | 第一次心跳，续租 | 110 |
| t=20…80 | **事件循环冻住 60 秒** | 110 |
| t=100 | 心跳照常续租；`100 < 110`，fence 通过 | **170** |

一个死了 60 秒的进程就这样保住独占权，而且期间没有任何别的 Worker 能 reclaim
它——`reclaim_expired` 找的是已过期的行，而这一行的过期时间一直被推着走。
`implementation-plan.md:925` 写着"Watchdog 绝不能替 Worker 续租"，
**今天违反这条的不是 watchdog，是 heartbeat 自己。**

改法是心跳在续租前自查迟到，超过 `heartbeat + abort_lag`（默认 20+20=40 秒）
就不续租，抛 `StaleExecutionError`。它落进 `_execute` 里那条**既有的**
`except StaleExecutionError`，复用 `_GuardLostError` 已经走通的纪律——不再写
checkpoint、heartbeat、lifecycle——而不是新造一条停机路径。**零配置字段、
零迁移、零线程、零新端口方法。**

**处置权本来就该在心跳手里**：它是唯一那个"停摆期间还在等、循环恢复时第一个知道
自己迟到了多久"的东西。daemon thread 能更早**检测**，但它无法在不持数据库句柄的
前提下**阻止**那次续租，而让线程持句柄距离"线程自己替 Worker 续租"只差一次编辑。

**一处实现缺陷是被测试找出来的，记在这里。**第一版把计时锚点取在协程的第一行，
测试直接超时——因为 `time.sleep` 发生在心跳协程**第一次被调度之前**，它的首次读数
落在停摆之后，于是正常睡、正常续租。换成真实场景就是：循环在 claim 之后、心跳
首次运行之前冻住，这个自查完全看不见，**同一个洞没堵上**。锚点因此改成心跳任务
**被创建**的时刻（`since=loop.time()` 在 `create_task` 处求值），第一个窗口——
也就是紧接 `claim_next` 之后、冷模型加载与大文档解析真正落在的那一段——才算数。

**刻意不做的两件事**，都写在 ADR-041 里：不 release 租约（让它自然到期，才能由
健康 Worker 用新 epoch 接管；一个刚承认自己不可信的进程发出的 release 不该被接受），
以及**不给 ingestion worker 装**——它循环上还有本批不挪走的合法阻塞源
（`TextDocumentParser` 的同步 `extract_text`），装了会把一次正常的大文档摄取判成
失去 lease。这是本批内部的顺序依赖，写进 ADR 而不是留在谁的脑子里。

`abort_lag` 由装配处从 `heartbeat_seconds` 派生，**不是配置字段**：一个能被调到
大于 `lease_seconds` 的旋钮等于让自查永不触发，而配置校验挡不住它（两个数在不同
的 section，失败形态是"缺少一次拒绝"而不是"值不合法"）。

**验证。**新增 `tests/workers/test_task_worker_heartbeat_lateness.py`，两条用例是
一对：停摆那条用同步 `time.sleep` **真的冻住事件循环**（不是 patch 时钟——手动
搬时钟的测试会对着测错东西的实现通过），对照那条同配置、同代码路径、只是不冻循环。
两层修复逐层撤销实测：

```text
撤掉迟到判据            1 failed / 1 passed
锚点改回协程第一行      1 failed / 1 passed
```

对照组两次都保持绿，这正是它存在的理由——证明红不是"总是红"。
真 PostgreSQL 下 `tests/workers/` + `tests/persistence/test_task_worker*.py`
25 passed / 0 skipped，ruff、pyright 全绿。

### 二、阻塞调用有了界（ADR-042）

计划 `implementation-plan.md:900` 要的是「`AdapterCallRunner` 是所有 Model/Tool/外部
SDK 调用的**唯一入口**」。ADR-042 **收窄了这句话**，理由是边界而不是工程量：把工具
派发改成物理穿过一个 runner 对象，等于在自研 Runtime 与它的工具之间插进第二个持有者，
直接撞上"Tool Loop 只有一个所有者"这条冻结 `Literal`。所以 runner 是**注入给会阻塞的
Adapter 的一个协作者**，不是所有调用的必经之路。30+ 个调用点一处不动。

**真正的问题比"没有界"更具体。**`asyncio.to_thread` 用的是解释器的**默认** executor，
而这个 executor 是**全进程共享**的——`getaddrinfo`（每一次对外连接背后那一下 DNS）
排的是同一个队。所以一串嵌入批次不只是让嵌入变慢，它把 DNS 排到了嵌入后面，而队列
长度没有任何人看得见。

`adapters/concurrency/call_runner.py`：专用 `ThreadPoolExecutor` + **等大**
`asyncio.Semaphore` + 排队超时。两个数相等但都不能删，这一点写死在模块 docstring 里
（否则后人会看见"两个相等的数"删掉一个）：`max_workers` 真正限制并发，
`Semaphore` 负责让等待**可观测且有时限**——`ThreadPoolExecutor` 的 work queue 是无界
`SimpleQueue`，给不了超时。**排队上限只管等一个 slot，不管调用本身**，所以一次合法的
慢调用不会被它杀掉；饱和的后果是排队 + 超时，**不是拒绝**（三条路径全是只读幂等的，
拒绝会把一次慢检索变成一次失败检索）。

搬了 5 条路径：dense/sparse/reranker 三个模型适配器（原本 `to_thread`），
加上 `LocalArtifactStore` 的 `get` 与 `_read`（原本**根本没进任何线程池**）。
**`put` / `put_stream` 明确不搬**——计划自己写着"不可取消的非幂等写调用不能放进普通
线程池"，而它的 quarantine → replace 语义被取消后会在盘上留下半个文件。

两个新配置字段 `coordination.blocking_call_slots`（默认 2）与
`blocking_call_queue_timeout_seconds`（默认 30）。**放 `[coordination]` 不是因为找
不到别的地方**：`runtime` / `multi_agent` / `rag` 三个前缀都在 `task_snapshot_allowlist`
里，放进去会让"这台机器给阻塞调用开几个线程"变成**每个 Task 的语义**并改掉全体
`run_semantics_revision`——与否掉 `rag.enabled` 开关是同一条推理，**不要把部署状态
伪装成语义**。而且它们的作用是让循环保持响应、以致心跳和租约仍然诚实，**它们是存活性
参数**。配置 schema 保持 `1.14`，ADR-042 §13.2 明说两条先例（ADR-036 抬版、ADR-038
不抬）在这个判据下不可调和，本批跟随更窄的那条。

**一处真回归是被既有测试抓住的，记下来。**`test_the_download_leaves_the_app_in_more_
than_one_piece` 变红，body 为空。原因不是流坏了：那个用例在 `receive()` 里**立刻**投递
`http.disconnect`，而 `StreamingResponse` 让 body 与 `receive()` 赛跑——ADR-042 把
每块读挪上线程后，body 生成器在第一次 `yield` 之前多了一个 `await`，于是断连稳赢。
**改的是用例不是实现**：一个已断连的客户端拿不到字节，比拿到两块写进死 socket 更对；
用例要证的是"分多块下发"，不该顺带测调度顺序。现在它等响应真正结束再投递断连，
并带 10 秒上限，免得把挂起伪装成通过。

其余 16 处红都是同一类：测试用 lambda / 函数替身顶掉三个工厂，签名跟不上新增的
`runner=` 关键字。属于 fixture 跟进，不是行为回归。

**验证。**新增 `tests/adapters/test_blocking_call_runner.py`，四条用例是**两对**：
界与「更宽的池确实到得了更高峰值」，超时与「愿意多等的调用仍然拿得到 slot」。
第二对正是把"池饱和就拒绝"（ADR 否掉的）和"池排队、队列有上限"区分开的东西。
逐层撤销实测：

```text
撤掉排队上限                    1 failed / 3 passed
把界整个拿掉（池放宽+不再守门）  2 failed / 2 passed
```

两条对照组两次都保持绿。全量 `tests/`（真 PostgreSQL + Qdrant，不含 e2e）
**2693 passed / 11 skipped**，ruff、pyright 全绿。

**已知未闭合**：`blocking.close()` 目前只在 task worker 的 `AsyncExitStack` 里注册，
API 与摄取 Worker 没有注册——它们是进程生命周期对象，且 `ThreadPoolExecutor` 在提交
第一份工作前不建线程，所以没有泄漏，但"谁负责关"这件事在三个进程里不一致。

### 三、调用额度有了一个持久的计数器（ADR-040 第一刀，共三刀）

`multi_agent.max_agent_invocation_attempts_per_task` 从有 settings 那天起就声明着，
**至今没有第二个读者**。没投影的理由写在 `projections.py` 里：它要跨 retry 与 reclaim
计数，需要持久的 per-Task 计数器，投影它只会让它离"看起来被执行了"更近一步。

这一刀是那个计数器，**只有它**。迁移 `0025_agent_invocation_count` 给 `task_runs` 加
`agent_invocation_count`（not null，server_default 0），并把既有的
`task_runs_lease_counters` check **替换**成含三个计数器的版本（不是并排加第二条——
三个都是"计数不能倒退"这同一类主张，两条约束说同一件事会漂移）。**不回填**：现存每一行
都花掉了未知的额度，0 是唯一诚实的起点，它少算历史而不是编造历史。

**不建台账表**，理由写成结论而不是偏好：`_context_for` 每次重放都现铸新的 `agent_run_id`，
所以 `(task_id, agent_run_id)` 唯一键换不来它名字暗示的幂等，它换来的是可诊断性。

**ADR §2.9 把这一刀写成"纯 schema、零行为变化"，这个前提在本仓库不成立，如实记下来。**
`_to_run` 用行映射构造 `TaskRun`，而它是 `extra="forbid"` 的严格模型，于是"多一列"直接让
**201 条既有测试变红**（`Extra inputs are not permitted`）。所以这一刀必须同时给
`ports/task_registry.py` 的 `TaskRun` 加上这个字段。仍然没有任何代码**读**这个数——
加字段是为了让表读得出来，不是为了让谁用它。

**验证。**双向迁移对着真 PostgreSQL 实测（注意约束的真名带 `ck_task_runs_` 前缀，
用裸名查会查出空来、把"没查到"误当成"没问题"）：

```text
head            列在，check = lease_epoch>=0 AND attempt_count>=0 AND agent_invocation_count>=0
downgrade -1    列没了，check 回到两项
upgrade head    列回来，check 回到三项
```

有牙验证用的是既有的 `test_the_migrated_schema_matches_the_model_metadata`：把列从
`models.py` 拿掉、迁移不动，**2 failed / 3 passed**，失败信息直接点名
`Detected removed column 'task_runs.agent_invocation_count'`。另外那 201 条红本身就是
这一刀的第二重牙——模型不认这列时它们立刻全红。

全量 `tests/`（真 PG + Qdrant，不含 e2e）**2697 passed / 11 skipped**，ruff、pyright 全绿。

### 三之二、这个计数器开始动了，但**不拒绝任何东西**（ADR-040 第二刀）

`TaskRegistry.reserve_agent_invocation(lease) -> int`：一条 fenced UPDATE，
`agent_invocation_count + 1` 由 PostgreSQL 在**同一把行锁里**算出来——不是先读后写，
否则两个 Worker 会读到同一个旧值。走的是 `_live_lease_conditions`，不发明第二个 fencing
token。

**先扣后花**：在调用之前记账，不是之后。每次都恰好在调用中途崩溃的循环永远走不到"事后
记账"那一步，而那个循环正是这条闸存在的理由。代价写在明处：崩在记账与调用之间会多算一次，
所以真实上界比配置值**小**一点而不是大一点。

`BudgetedAgentExecutor` 包在 `BoundedParallelExecutor` **外**：记账的那次 Registry 往返
发生在拿并发槽**之前**，而不是握着槽等数据库。作用在 executor 而不是 node 上，理由和
`BoundedParallelExecutor` 一样——花钱的是一次 invocation，以后加扇出不用回来改这个文件。

**这一层不拒绝任何东西**，这是刻意的，也是 ADR-040 三刀里中间那刀的全部意义：一条闸如果
第一次被人看见就是"某个 Task 突然变成终态"，那在值班的人眼里跟 bug 没有区别。所以数字先
可见，再致命。`TaskView` 上因此多了一个只读的 `agent_invocation_count`。

没有 lease 在 scope 里时**既不记账也不拒绝**：那种组合只出现在没人 claim 过 Task 的地方
（窄测试、demo handlers），凭空造一个"付款方"比不记账更糟。

**验证。**新增 `tests/persistence/test_agent_invocation_budget.py` 六条，对着真 PostgreSQL。
三层撤销各自命中不同的用例、互不重叠：

```text
计数器不再自增        4 failed / 2 passed   （剩下 2 条正是两条"没花钱"的对照组）
装饰器不再记账        1 failed / 5 passed
retry 时清零计数器    1 failed / 5 passed
```

"跨 retry 不归零"那条特意**同时断言 `attempt_count`**：它本来就跨 retry 存活，所以一个
只会往上加的计数器会看起来正确却在量错东西；用例最后让两个数字**分叉**（3 对 2），
把"这是两个计数器"这件事放在看得见的地方说。

全量 `tests/`（真 PG + Qdrant，不含 e2e）**2703 passed / 11 skipped**，ruff、pyright 全绿。

**第三刀没做**：读上限、超限抛 `AgentInvocationBudgetExhaustedError`、Worker 写
`dead_letter`（含一条与 `reclaim_expired` 可区分的 `status_detail`），以及 ADR §2.7 那
三种拒绝的区分（失去 lease / 额度用尽 / 快照里没有这个键）。**所以
`max_agent_invocation_attempts_per_task` 至今仍然没有被执行**——它现在有了一个会动的、
看得见的计数器，仅此而已。

**动手前先查清楚了一件 ADR 没说对的事，记在这里免得下一个人重踩。**ADR-040 §2.8 写
"dead-letter 基础设施**已全部就位**，不需要新设计"，并逐条列了 `TaskStatus`、check 约束、
`ALLOWED_TRANSITIONS`、`TaskDeadLettered` 事件、CLI 终态。逐条核过都对，**但漏了一条**：

```python
class TaskDeadLettered(TaskLifecycleEvent):        # domain/events.py:152
    reason_code: Literal["lease_expired"] = "lease_expired"
```

`reason_code` 是**单值** `Literal` 且带默认值，因为它至今只有 `reclaim_expired` 一个写入方。
额度用尽照原样写进去，会发出一条 `reason_code="lease_expired"` 的事件——那是假话，而且正是
ADR §2.8 自己要求"两个作者必须可区分"想避免的东西。所以第三刀**必须**同时改这个事件。

好消息是这条路通，而且有仓库自己的先例：同一个文件里
`TaskRetryScheduled.reason_code` 已经是 `Literal["lease_expired", "retry_requested"]`，
**双值且不带默认**——不带默认强迫每个写入方自己说清楚。全仓 grep 过 `reason_code`，
没有任何读取方 switch 在它上面（只有写入方），前端也不读；旧事件继续合法，因为
`lease_expired` 留在取值集里。所以第三刀的第一步应该是：把 `TaskDeadLettered.reason_code`
按 `TaskRetryScheduled` 的形状加宽成双值并**去掉默认**（`reclaim_expired` 那处已经显式
传值，去默认不破坏它）。

另一处连带：`_transition_event(task, to)` 只按目标状态选事件，拿不到原因，且它对
`dead_letter` 目前直接 `raise AssertionError`。所以 `mark_dead_lettered` 要么不走 `_move`，
要么给这两个函数加一个原因参数——这是第三刀里唯一需要拿主意的地方。

### 三之三、闸门开始拒绝（ADR-040 第三刀，完成）

上面那两处都按查明的方案改了：`TaskDeadLettered.reason_code` 加宽成
`Literal["lease_expired", "invocation_budget_exhausted"]` 并**去掉默认值**（`reclaim_expired`
那处随之显式写出自己是哪一种）；`_move` 与 `_transition_event` 多带一个 `reason_code`，
且 `dead_letter` 分支在拿不到原因时**直接 assert 失败**而不是挑一个填进去。

**上限从 Task 自己的快照读，不从进程配置读。**`reserve_agent_invocation` 在**同一条**
fenced UPDATE 里从行自己的 `run_semantics_snapshot` 取
`multi_agent.max_agent_invocation_attempts_per_task`，`+1` 与比较都在一把行锁下发生——
两个 Worker 因此不会各自读到同一个"最后一格"。

**三种拒绝必须可区分**（ADR §2.7），零行结果的那条路多读一次行来判读：

| 情形 | 抛什么 | Worker 写成 |
|---|---|---|
| 租约已经不是当前的 | `StaleExecutionError` | 什么都不写，交给接手的 Worker |
| 快照里没有上限 | `AgentInvocationCeilingMissingError` | `failed` |
| 计数已达上限 | `AgentInvocationBudgetExhaustedError` | `dead_letter` |

**判读顺序是有意的，并且被单独测了**：既丢了租约又用完了额度时，报的必须是租约——
反过来就会给一个已经属于别人的 Task 写终态，那正是围栏要挡的事。

缺上限判成 `failed` 而不是 `dead_letter`：那是**这个部署的缺陷**，不是一个毒任务；
打成 `dead_letter` 会把一次配置事故变成一批不可复活的 Task。

**验证。**用例从 6 条加到 11 条。四层撤销，每层命中不同的子集：

```text
UPDATE 不再比上限              3 failed / 8 passed
先判额度、后判租约             2 failed / 9 passed
两个写入方发同一个 reason_code  1 failed / 10 passed
缺上限被当成额度用尽           1 failed / 10 passed
```

其中第三层第一次没撤成功——锚点字符串在两处都匹配，脚本 assert 失败后跳过了修改，
那次的 "11 passed" 是未撤销状态下的结果、不能算数。换唯一锚点重做才拿到上表那一行。
**记在这里是因为它正是"绿灯可能只是探针指错了地方"的现场版本。**

对照组同样是成对的：`test_the_ceiling_comes_from_the_task_not_from_this_process` 在同一个
进程里跑两个上限不同的 Task——若上限来自配置，两个会在同一个数字上停下，那条会红而
"用完了就拒绝"那条仍然绿。

全量 `tests/`（真 PG + Qdrant，不含 e2e）**2708 passed / 11 skipped**，ruff、pyright 全绿。

**至此 `max_agent_invocation_attempts_per_task` 第一次被真正执行**——从"声明了很久、
无人读取"，到有持久计数器、可见、并且会拒绝。

### 六、第 7 条四件新建能力的分诊（只出 ADR，不写实现）

四件（RAGAS runner / 通用 Tool 审批 / Word 读+编辑 / 生产身份+S3）逐件勘察后分诊。
结论是**只有两件现在该落笔**，而且其中一件写的是"不做"：

| 排序 | 能力 | 判定 | 一句话理由 |
|---|---|---|---|
| 1 | Word 读+编辑 | 写 ADR | 撞**零**条冻结边界、零配置叶子、零迁移，`python-docx` 已是主依赖 |
| 2 | 生产身份 + S3 | 写 ADR（**明确不做**） | 撞的冻结边界最多；且没有 remote 部署就没有消费者 |
| 3 | RAGAS runner | 需用户拍板 | 规格最清楚，但"judge 用谁"只能由用户定 |
| 4 | 通用 Tool 审批 | 推迟 | 要正面回答"是不是在反转 ADR-038"，且第一件缺的是**人做的分类清单**而非代码 |

[ADR-043](./adr/0043-docx-reading-is-a-native-tool.md)：读取器是 native 工具，不是第二个
MCP server。这一条**不是**用户的取舍——`adapters/tools/workspace.py:11-15` 已经为同一个
问题判过一次，而且 MCP 那条路被物理条件否掉（参数只能来自模型输出，
`MAX_MCP_REQUEST_BYTES=262144`）。写下来是因为下一个只读代码的人会先想到抄 ADR-027 §3.4
的渲染器模板，那是事后很难改的错。

[ADR-044](./adr/0044-no-remote-no-production-identity.md)：先有远端部署，才谈得上生产身份与
远程对象存储。形式沿用 ADR-041 刚立的先例——**把"明确不做"写成一份 ADR**，而不是留白。

**分诊查出一处真缺口，已独立核实：**`grep -rn "s3" tests/` **零命中**。三处
`backend != "local"` 的拒绝装配（`dependencies.py:304`、`task_worker/composition.py:243`、
`ingestion_worker/composition.py:110`）**一条测试都没有**，而 README、
architecture-baseline 与 status.md 三处都把这个行为当成"这是 fail closed，不是能力"
在引用。按本仓库纪律，**一个被文档引用、却没有覆盖触发条件的回归测试的行为，不算完成**。
补三条拒绝 + 一条对照（`backend=local` 装得起来）是半天的活，且它让"我们明确不做"这句话
变成有牙的，而不是三行没人验证过的代码。

**十个问题留给用户拍板**，逐条写进了两份 ADR 的未决一节，agent 没有替他给答案。
最要紧的一条是本轮的性质本身：你要的是"截止前多出一件能演示的新能力"，还是"把该关的门
关严、把形状定死"？两份 ADR 偏后者。

### 七、Word 进摄取路径（A1，用户选了"要一件能演示的能力"）

用户在上面那个问题上选了 (a)。所以做的是 ADR-043 §14 明确留给用户的**入口**问题里的 A1：
上传的 Word 能被切块、索引、检索、被答案引用。

**先搬家，否则会违反 ADR-043 §5。**唯一那份 docx 解析实现原本在 `apps/api/docx_preview.py`
——摄取 Worker 够不到它，直接在 parser 里写第二个 `Document(BytesIO(...))` 等于新开一条
解析路径、也就新开一个绕过三道 zip 炸弹闸的洞。所以整份搬到
`adapters/documents/docx.py`（`adapters` 是 outer boundary，Worker 与 API 都到得了），
`tests/api/test_docx_preview.py` 跟着搬到 `tests/adapters/test_docx.py`，都用 `git mv`。

**上限变成调用方的参数，而不是模块的常量。**`MAX_PREVIEW_CHARS = 40_000` 是**面板**的数
（它自己的注释就写着 "this is one panel beside a run"）。摄取照抄会**静默索引半份文档**，
而且每一层都报成功。所以 `extract_docx_preview(content, *, max_chars=MAX_PREVIEW_CHARS)`：
预览路径逐字节不变，摄取传 `None`。**默认值刻意保持预览那个数**——要整篇的调用方必须自己
说出来。

**没有 page_starts，这是格式的性质不是遗漏。**docx 不存分页：页在哪断由排版的渲染器连同
字体和纸张一起决定，两个阅读器对同一份文件合法地不一致。所以引用退回字符偏移，
`ParsedDocument` 用"空 `page_starts`"表达这件事（它的注释已经写明空数组是正面声明）。

**一处我先写错、被实测纠正的事，留痕。**我原本断言"法语 Word 的 `Titre1`、德语的
`Überschrift 2` 不会被识别成标题，所以本地化 Word 会丢结构"，还写了测试断言它——**测试
还通过了**。实测推翻了这个说法：OOXML 里 styleId 是本地化的，但 `w:name` 是语言中立的
`heading 1`，python-docx 解析的是后者，所以两种都被正确识别成 `#` / `##`。之前"看起来
丢了"，只是因为我那个最小包**没有 `word/styles.xml`**，什么样式都解析不出来。

这正是本仓库反复防的那件事：**一条会通过的测试，把一个不存在的限制写进文档。**现在
测试包带上了 styles part（真实 Word 文件总是有的），并断言实测行为：本地化标题保留，
真正的自定义模板样式（`Report Title`）保持纯文本——后者是前者的对照组。

**验证。**新增 `tests/ingestion/test_docx_parsing.py` 七条，**全部 fixture 是手工拼的
OOXML 字节**，不经过本项目的 `render_document`——这正是 ADR-043 §8 要的新证据，因为现有
docx 证据是自产自读的闭环（那份测试的 docstring 自己写着）。三层撤销：

```text
不再派发到 docx 读取器     6 failed / 1 passed
摄取沿用预览的 40k 上限     1 failed / 6 passed
空文档不再被拒             1 failed / 6 passed
```

前端三处（`SERVER_READABLE_MEDIA_TYPES`、`MEDIA_TYPE_BY_EXTENSION`、两个 `accept` 与
`ACCEPTED_EXTENSIONS`）加 `.docx`，并补了三条 Vitest：浏览器给出的类型要保留、浏览器没给
时按扩展名补、而 `.doc` **仍然不猜**——那是这个 build 读不了的旧二进制格式，猜一个可读类型
只会让文档永远停在"正在建立索引"。

`tests/ingestion/test_docx_parsing.py` 进了 `per-file-ignores` 的 `E501`，理由写在
`pyproject.toml` 里：那些 OOXML 命名空间是必须逐字精确的单行 URI。

全量 `tests/`（真 PG + Qdrant，不含 e2e）**2715 passed / 11 skipped**；前端 `tsc -b` 干净、
Vitest **158 passed**；ruff、pyright 全绿。

**没做的**：`.doc`（旧二进制格式）、编辑、以及让 agent 在 Task 里**主动读**一份 Word——
那是 ADR-043 §13 的 A2/A3，仍然未决。本刀之后，Word 只是**又一种能被上传和检索的资料**。

### 四、52 题 gold set 重跑，ADR-017 第 2 步的证据到齐

见 `evals/rag/reports/`。四份报告出自同一个进程、同一个 collection、同一份 gold set
（digest `55ec24c7d2b86062`）、52 题。此前 llama_index 那两份锚定的是更早的一轮，
与 reference 不是同一次运行，**因此根本不构成对照**。

两个臂的四个排序指标**逐位相同**。不止聚合分数：`--dump-outcomes` 的逐题结果里，两条
路径唯一的差别是 `index_identity` 这个标签本身，52 题的检索结果**逐字节相同**——这说明
top_k 没有被应用两次、没有在进程内二次融合、node 往返没丢 page 或 revision。

顺带确认了可复现性：reference 两份与 8-10 那轮相比除延迟外逐位未变，ADR-033 之前那种
"重建索引就换一组分数"的抖动没有再出现。

**延迟变化很大，写在明处**：hybrid 的 `retrieval_latency_ms` 从 171.94 跳到 38608.08。
两条路径同向变化、排序指标一个没动；本轮是在物理内存只剩 20% 空闲、swap 几乎耗尽的机器上
跑的，而 38.6 s/题落在这台机器历史上 hybrid 臂 7.2–27.9 s/题的量级里——反倒是 8-10 那次的
171.94 ms 是异常值。**我没有查明它当时为什么那么快**，所以只说两轮延迟不可比，不声称
哪一个是对的。同批的 ADR-042 已核对过不在这条路径上（`run_rag_eval.py` 直接 `.load()`
不传 runner，`offload` 落回 `asyncio.to_thread`，与改动前逐字节相同）。

**没有翻 `rag.llama_index.enabled`。**这一轮补上的是"证据还没有"里的那个证据，不是那个
开关该翻的决定——脚本自己的 docstring 就写着等价性是 weak claim by construction。翻开关
会改动 Task 语义指纹与一条冻结边界，该由一份单独的 ADR 决定。

### 五、evidence manifest 重新生成（gate `batch3-2026-08-11`）

上一份（`gap-closure-2026-08-11`）有三处问题，这次逐条改掉：

| | 上一份 | 这一份 |
|---|---|---|
| 锚定的 commit | `bf31815`（早已过期） | `52c4bd1`（当前 HEAD） |
| 工作树 | `git_dirty: true`（`--allow-dirty`） | `git_dirty: false` |
| `evaluation_report` | 在 `missing` 里 | 4 份实际报告，带 SHA-256 |

`verify` 零问题。`missing` 现在只剩 `otel_trace_sample`、`demo` 与
`task_run_semantics_revision`——前者取不到的原因没变：compose 里从来没有
`otel-collector` 这个服务。

**它仍然是本机产物、仍在 `.gitignore` 里**（`artifacts/evidence/`）。所以这条记录说的是
"本机重新生成过、内容如上"，仓库里不会出现这份文件；要复核得在本机重跑。生成它需要
`AW_DATABASE__*` 三个 DSN 与两个 model id 环境变量，否则配置加载阶段就失败。

七条 `known_limitation` 逐条写进了 manifest，包括本批**没做完**的部分：ADR-040 三刀只
落了第一刀、ADR-041 明确不做 watchdog、ADR-042 收窄了"唯一入口"那句话、以及那个不可比的
延迟数字。

## 2026-08-11 缺口清单第二批：界面开始承认自己不知道什么，文档不再说假话

第一批（`5363d7a`）清的是"配置说了假话、唤醒没有消费端、毒行挡死回放"。这一批是同一
条线往前走一段：**服务端早就知道的事，界面上没人说。** 五处，加上最后一遍把文档里已经
过期的数字改准。下面第一节那条（无 `embedding` extra 的 Worker）单独记在紧接着的一条
里，不在这里重复。

### 一、知识库：写权限提前说，失败别装成正在索引

两件事，都是"服务端知道、使用者不知道"。

**写权限。** `KnowledgeBaseSummary` 加 `can_write`，在 `_summary_query` 里用
`owner_id == principal_id` 算出来，与 `KnowledgeBaseService.require_writable` 的
owner-only 规则同源。前端据此**整块不渲染**上传入口，而不是渲染成禁用按钮——这个知识库
是别人分享进来的，要消掉的体验正是"传完整份文件再吃 404"。**服务端的 `require_writable`
一行没动**，隐藏只是提前告知；测试把两者钉成一对：同一条用例既断言只读主体拿到
`can_write=false`，也断言它上传仍吃 404，撤掉任意一半都能让它变红。

**失败状态。** 失败原因此前**根本没有持久化过**，而 `KnowledgeDocumentStatus` 只有
`processing`/`ready` 两档、由 `last_applied_revision == source_revision` 推出，于是
**任何摄取失败都表现为永久"正在建立索引"**。现在 `documents` 表加 `failed_revision` +
`failure_code` 两列（迁移 `0024_document_ingestion_failure`），配
`(failed_revision IS NULL) = (failure_code IS NULL)` 的 check 约束——半个失败不可表示。

三个设计决定值得写下来：

- **按 revision 划范围而不是布尔标记。**重新上传不会一出生就是失败态；成功路径用同一条
  UPDATE 把标记清掉。
- **存 `ErrorCode` 而不是解析器的异常文本。**那段文本会把文档自己的字节回显给每一个能读
  这个知识库的人。
- **状态是投影推导的**（先 ready、再 failed、否则 processing），`failed_document_count`
  从 `processing_document_count` 里减出去——否则"3 个处理中"会在上一层继续撒同一个谎。
  `_document_refused` 用 `IS NOT DISTINCT FROM` 而非 `=`：processing 分支要对它取反，
  而三值逻辑下 `NOT (x = NULL)` 是 NULL，会把所有健康文档漏掉。

迁移**不回填**：失败是某次摄取做出的观测，凭空回填等于给没人拒绝过的文档扣帽子。
`downgrade -1` → `upgrade head` 双向实测都过，列数 2 → 0 → 2。

**两处代价，写在明处。**（1）瞬时故障（比如 Qdrant 短暂不可达）同样会被记成 `failed`，
等下次重试成功再清掉——这是 worker 模块 docstring 里已经写明的权衡，但意味着一次外部
依赖抖动会让界面短暂显示"索引失败"。（2）`_record_refusal` 自己开新事务；若此时 PG 不
可达，它抛出的异常会顶掉原始异常（原始的还在 `__context__` 里）。重试行为不受影响
（事件照样不 ack），只影响日志里看到哪个错。

### 二、Work 页承认自己拿到的是残缺时间线

后端从毒行隔离落地起就在发 `skipped_sequences`（`routes/tasks.py`），**前端一直没接**。
现在从类型一路接到界面，**只加披露，不动游标**。

`TaskTimelineResponse.skipped_sequences` 定成**必填**是故意的：服务端永远发它，而必填
让所有过期 fixture 在 `tsc -b` 当场炸出来——第一次跑 typecheck 就是靠这个把 4 个测试
文件里的构造点一次找齐的。类型注释点明"空数组是『这一页完整』的**正面声明**，不是
『服务端没查过』"。

**没有退化成一句"有些事件丢了"。**位点和已交付事件的 `sequence` 同处一个命名空间，所以
纯函数 `locateTimelineGaps` 把每个缺口**锚定在它确实收到的前后两条事件之间**，渲染成
"#2：在「工具调用已开始：external_search」与「任务成功完成」之间"。运维拿位点能去翻那
一行，读者能看出是这段运行的哪一截不完整。措辞是"这些事件仍在日志里，只是这次没能解码、
没有交给这个页面"，不是"丢了"——**行还在，坏的是解码**。告警落在步骤流**下方**：它是关于
上面那些步骤的陈述，读者得先看见步骤。

跨页语义：`mergeSkippedSequences` 做并集去重升序。并集而不是覆盖，因为每个 response 只
为自己那一页说话；去重是因为重叠重读会把同一位点再报一遍——**这正是后端给位点不给计数
的理由**（重发的计数分不清是旧伤还是新伤）。另外 `locateTimelineGaps` 会**丢掉已被后来
某次读取真正交付的位点**：重读解码成功后客户端手里已经有那条事件了，继续声称有洞是同一
个谎反着说。

**没动的东西**：cursor 照常推进（后端明写毒行也要推过去，不推就永远撞同一行）。

**Chat 那一半仍然沉默**，这里如实记下来：`sessionStream.ts` 的 `acceptQuarantine` 同样
只推游标、不给用户看。逻辑上 Work 显示了 Chat 也该显示，但那是第二块 UI 设计，没塞进来。

### 三、Markdown 上传的 MIME 兜底（只改前端）

真实失败路径是：浏览器对 `.md` 常常给空 `type` 或 `application/octet-stream`，上传三步
**全部返回成功**（`routes/uploads.py` 与 `application/uploads.py` 全程不看 media_type），
然后在**异步摄取 worker** 里被解析器拒绝，`last_applied_revision` 永不推进，文档永远停在
"正在建立索引"。**不要写成"上传被拒"，也不要去 uploads 路由找 422。**

改法是前端新增纯函数 `declaredMediaType(file)`：`file.type` 按 `;` 截断、trim、小写后
落在允许集里就**原样用**（浏览器给了服务端读得懂的类型就不覆盖，那是更具体的观察，
改掉是丢信息）；否则按 `.md/.markdown/.pdf` 查表；再否则才 `application/octet-stream`。

**为什么不改后端。**把 `application/octet-stream` 塞进 `TEXT_MEDIA_TYPES` 等于取消允许集，
凑巧能 UTF-8 解码的二进制会被当正文索引——那正是 `parser.py` 那段"without guessing"注释
唯一明确禁止的改法。而 `apps/cli/upload.py` 早就在用 `mimetypes.guess_type(source.name)`：
按扩展名读**名字**不是断言**字节**，前端与 CLI 同侧。

带 charset 的用例（`text/plain; charset=utf-8`）是比计划多加的：`parser.py` 就是按 `;`
截断再比对的，前端的匹配口径必须跟它一致，否则带 charset 的声明会被误判成"服务端读不懂"
而被扩展名覆盖。

**这只拆掉了最常见的触发器，没修好那一类。**"摄取失败表现为永久处理中"这件事本身由上面
第一节的 `failed` 状态解决；两件事是配套的，但 MIME 这条只是让最常撞的那扇门不再关上。

### 四、Playwright 用例查的是一个不存在的按钮

`AttachmentTray` 的 aria-label 改成"上传文件到知识库"之后，`web/e2e/shell.spec.ts` 还在
查"添加附件"，于是 e2e 红着落了地。**改用例而不是改回按钮名**：旧标签描述的是这套系统
并不存在的"逐条消息附件"，新标签说的是文件去哪儿。

顺带更正一条流传的说法：`pnpm --dir web check` 确实不含 e2e，但 `ci.yml` 有独立的
`pnpm --dir web test:e2e` 步骤——**CI 并非看不见这个失败**，是这次改名带着红的 e2e 落了地。

e2e 目录只有这一个文件，27 个选择器全部对照现网源码核过。重点排查了绿测覆盖不到的一类
隐患：`toBeHidden()` 在元素**根本不存在**时也会通过，所以藏在它后面的过期选择器不会让
测试变红。这类断言有两处，背后三个名字都还活着，且每个都在别处被正面断言过可见。

另记一条本地陷阱（本次没修）：`playwright.config.ts` 的 `webServer` 命令在 corepack 环境
下派生的 `/bin/sh` 里没有 pnpm，必然 127；本地跑 e2e 要先手工 `build` + `preview` 起 4173。

### 五、把文档里已经过期的地方改准

三处点名的漂移，逐条处理：

1. **tied-score 不确定性**：README 把它列为"已知的可复现性缺口"，而
   [ADR-033](./adr/0033-fusion-ranks-are-ours.md)（2026-08-10）已经修掉了，连带更正了
   诊断——不稳的不是并列分数的**次序**，是分数**本身**。README 里"CI 那条 tie-break 会
   偶发失败"的说法同属修复前状态，已改为"现在是确定性通过的"，并保留了这次更正本身，
   因为一条被改过口径的结论比一条悄悄消失的结论有用。**同时守住了一个界**：噪声底没了
   不等于评测做完了——ADR-017 第 3 步要的等价性评测**还没有在可复现的检索器上重跑**，
   所以 `rag.llama_index.enabled` 继续 `false`，理由从"路被堵着"改成"证据还没有"。
2. **工作树脏 + Ruff/Pyright 红**：那两处红（`docx_preview.py` 的格式、
   `composition.py` 的两个 `EmbeddingUnavailable` 类型错）现已不存在，门禁全绿；
   README 里那段"此刻各有一处红"连同"工作树含未提交改动"的口径一起换成了当前实测值。
3. **旧测试数与旧迁移 head**：README、README.en.md 与本基线文档里的
   `1821/1264/2711/2629/1996/920`、`135 passed`、`114 passed`、`45 passed`、
   `2 passed`、`441/483 files`，以及 `architecture-baseline.md` 里的
   `alembic head = 0019_tool_executions`，全部按下面这轮实测重写。

另外三件顺手改准的：

- **evidence manifest**：README 此前读起来像"仓库里有一份新证据包"。事实是它是**本机产物**，
  `artifacts/evidence/` 被 `.gitignore` 忽略（clone 下来没有），而现存那份锚定的 commit
  停在生成它的那一次、`git_dirty` 记的是 `true`，已经过期。**本批没有重新生成它**——那需要
  真跑一轮评测，不在范围内。文档改的是描述，不是产物。
- **README.en.md 说 WP15 阶段四、五"have not started"**，而中文版写着两个都已落地。这是
  **低报**不是夸大，但两版口径必须一致，已按中文版改准；同时补上中文版有而英文版没有的
  `config.word-local.toml`。
- **架构状态表补了一串只有 Planned 的行**。此前它们根本不在表里，而"不在表里"和"写着未实现"
  读起来差不多、实际差很多：一张只列做过的事的表，读者读到的是"这就是全部范围"。补进去的
  包括知识库管理、Chat 会话服务端管理、逐条消息临时附件、Chat 历史压缩、通用 Tool 级动态
  审批、benchmark runner、动态 supervisor / agent spawn / mailbox、Word 读取与编辑、
  远程对象存储与远程部署、LlamaIndex ingestion 迁移。

**多改了两份，说明理由**：本批点名的是四份文档，但另外两份带着**完全相同的**三处漂移，
而且都是从 README 第一屏点进去的：

- `docs/HIGHLIGHTS.md`（README 第一行推荐的"十分钟版本"，也就是最先被读到的那份）：
  过期数字（`1996/2629/920/441 files/114 passed`）与同一句已经不成立的"CI 那个真服务
  job 不是每次都全绿"。已改：门禁数字换成同一轮实测值，tie-break 那段改成**留痕的更正**
  而不是删除，边界一节补上红线清单与"控制台管不了的事"，迁移数 23 → 24、ADR 数
  38 → 39（编号 0012–0039）。**代码行数那三个值重新数过**（`git ls-files` 计，与原文
  声明的方法一致），因为原值与当前树对不上。
- `docs/README.md`（文档索引，README"设计依据"一节的第一条）：钉着 `main@a4dea2b`、
  一组更早工作树的数字，以及同一句 tie-break 偶发失败。已改成同一轮实测值、去掉 commit
  锚点、把那句话标注为**已不成立**，并把 LlamaIndex 开关的理由更新成"缺证据而不是缺路"。

让这两份继续与 README 相反，等于把这次改动的目的整个抵消掉，所以一并改了。除这两份外
没有再动别的文件；源码、测试与前端一行未碰（`git diff` 统计与接手时逐字节一致：30 个
非文档文件、1689 insertions / 131 deletions）。

### 证据

门禁全部本机实测（2026-08-11，真实 PostgreSQL 5433 + Qdrant 6333，`PYTHONDONTWRITEBYTECODE=1`，
跑前清过 `__pycache__`）：

| 环境 | 结果 |
|---|---|
| 后端，真实 PostgreSQL + Qdrant | `2716 passed / 11 skipped`（144.92s） |
| 后端，无外部服务 | `2040 passed / 687 skipped`（40.00s） |
| 后端，CI 那组服务型目录 | `1009 passed / 2 skipped`（88.14s） |
| 前端 Vitest | `155 passed`（22 个文件） |
| 前端 Playwright | `4 passed`（chromium + mobile 各两条） |

`ruff format --check .` 485 files、`ruff check src tests` 全过、Pyright
`0 errors, 0 warnings, 0 informations`、ESLint `--max-warnings 0`、`tsc -b`、
production build 均通过。Alembic 唯一 head `0024_document_ingestion_failure`。
11 项跳过中 10 项要 `embedding` extra 与本地 BGE 权重、1 项是 PostgreSQL-only 契约。

牙口：本批四项改动一共 25 条断言做过破坏验证，每一条都真跑出过红、还原后复跑绿。其中
两处值得单记，因为它们是"绿测覆盖不到"的那一类：

- **e2e 那条做了两次破坏，第二次更强**：不是把选择器改回旧名（那只证明字符串对不上），
  而是改掉 `AttachmentTray` 的 `aria-label` 后重新 build——同样红，证明这条用例咬的是
  真实 DOM。
- **`markdown-mime` 有 3 条在"还原成旧实现"下按设计保持绿**（它们守的是"别过度猜测"而
  不是"补足猜测"），所以另做了两次定向破坏（让扩展名压过浏览器类型、把兜底改成
  `text/markdown`）才证明它们有牙。

### 一条关于来源的说明

本批的知识库改动（`can_write` 与 `failed` 状态）**在被接手时已经完整存在于工作树中**，
是并行会话留下的——这是本项目已知的协作形态，第一批的提交信息里也记过同一件事。这一条
里做的是逐条核实、跑通迁移双向、并对每处修复做"撤掉→确认变红→还原"的验证，不是原创
编写。写在这里是因为**这份文档的用途之一是当作品集给人看**，工时与归属如果被据此判断，
应当以此为准。

## 2026-08-11 没有 embedding extra 的机器终于能跑 Task 了：一个只落地了一半的形态

`composition.py` 里那段 docstring 从 v2 落地起就论证过一个形态：**没有检索能力时只注册
v2，一个不指名知识库的普通 Task 照跑**。代码也写好了——图注册表会收窄、`_RealHandlers`
的注释解释了为什么收窄注册表而不是收窄认领。**但它是一段死代码，一次都没有被走到过。**

原因在投影层。`grounds_tasks` 判的是"配置里有没有检索"：

```python
grounds_tasks = (
    config.qdrant is not None
    and config.embedding is not None
    and config.retrieval is not None
)
```

而 `project_task_worker` 把这三样**无条件**填满：`_project_qdrant` 与 `_project_embedding`
的返回类型本身就不可为空，`RetrievalConfig(...)` 连 `if` 都没有。上游更堵死——`settings.rag`
与 `settings.qdrant` 都是**必填**段，没有 `default_factory`，**任何配置文件都写不出
"这个部署不检索"**。所以对每一个正式部署，`grounds_tasks` 恒为真。

后果比"一段死代码"严重一档：拒绝发生在 `build_embedder` **之后**，且必然命中——

```python
if grounds_tasks and isinstance(embedder, EmbeddingUnavailable):
    raise RealTaskHandlersUnavailableError(...)   # → EXIT_CONFIGURATION_ERROR
```

而 embedding 是 optional extra、CI 不装、`docs/deployment.md` 明说默认栈"intentionally
absent"。**于是：没装 extra ⇒ 标准 Worker 起不来 ⇒ 一个连知识库都不指名、一行都不检索的
普通 Work 也跑不了。**

### 半成品是怎么进来的

`5363d7a` 的提交信息自己写了：那次并入了"同一工作树上另一个会话未提交的在制品"，清单里
明确有**"Worker 装配的无检索形态"**。作者当时只做了两处机械修改让门禁变绿，其中一处正是
"把 `grounds_tasks` 隐含的两个条件显式重述一遍……**行为未变**"。也就是说，这个形态是带着
只落地一半的状态被合进来的，而缺的恰好是让它可达的那一半。

### 改法：让 `grounds_tasks` 由"实际装出了什么"决定

那条 raise 换成 WARNING 日志加一个 `TaskWorkerDependencies.grounding_unavailable` 字段。
**它的论点没有被推翻**——"这个部署要求接地，悄悄降级不诚实"是对的，错的是前提，因为投影层
让"这个部署要求接地"恒为真。所以降级照做，被拿掉的是"悄悄"：日志与那个字段是它的两个出口。

**这不是新决定，是兑现一个已经被接受的决定。** 同一件事 API 侧早就做完了：
`apps/api/dependencies.py` 里 "**A missing embedder costs retrieval, not Chat**"，配
`ApiDependencies.rag_unavailable` 把降级说出口。`grounding_unavailable` 是刻意取的同名
形状——两个进程为同一个原因掉同一半能力，一套词汇。

### 为什么没有写 ADR

项目规矩管的是**突破冻结约束**和**新增配置字段**，这次两样都没有：零 Settings 叶子、零
`Literal` 变动、零 `ownership.yaml` 变动、`config_schema_version` 保持 `1.14`、零迁移。
API 侧那次同形状的改动也是当缺陷修的，没有配 ADR。

**考虑过并否掉了新增 `rag.enabled` 开关。** `ownership.yaml` 的 `task_snapshot_allowlist`
含 `rag.*`，架构测试会**强制**把它登记成 `task_snapshot` 生命周期，于是"这台机器装没装
3 GB 权重"会进入每个 Task 的 `run_semantics_snapshot`、改变全体 `run_semantics_revision`。
那是把**部署状态**伪装成 Task 语义——同一份 `settings.py` 正是因为"where a model is
reachable is deployment state"才主动把 `model.base_url` 从快照里 pop 掉的。而且它解决不了
真问题：开关拨到 `true` 而权重装不上时，"raise 还是降级"原封不动地还在。

### 一个连带的部署耦合，CI 抓不到

`config.default.toml` 的 `workflow.graph_version` 是 `v1`，它经投影交给 **API** 的
`TaskService`，是**提交**时不指名 shape 的默认值。所以在没有 extra 的部署上，只修装配还
不够：绝大多数提交仍会 park 成 `waiting_migration`。这条链跨 4 个文件、跨 API 与 Worker
两个进程，`settings.py` 又只对 `graph_version` 做字符集校验、不校验是不是已知版本，CI 抓
不到。

**没有改任何配置文件的默认值**——仓库里那几份 local profile 都是有完整检索能力的机器
（本地跑真 BGE-M3 与重排），把它们指向 `v2_general` 是替有能力的部署做决定。改成两件事：
`deployment.md` 与 `running-locally.md` 写死"无 extra ⇒ Worker 只跑 v2 ⇒ 必须把
`workflow.graph_version` 设成 `v2_general`"，以及 Worker 在启动时若发现配置的默认版本自己
装不出来，打一条 `task_worker_default_graph_not_buildable`。日志是提示不是拦截：真正用这
个值的是 API，Worker 只是恰好也拿到了同一个投影字段。

### 证据

3 条测试，**三次牙口自检都真跑过**（还原 raise → 只留日志不设字段 → 放开图注册表限制，
每次都确认对应断言变红，再还原）：

- `test_a_worker_without_an_embedding_runtime_serves_v2_only`：装得起来、只挂
  `v2_general`、没开 Qdrant 连接、`startup()` 不抛、两条 warning 都说出口了。
- `test_real_worker_wires_model_retrieval_and_policy_gated_evidence` 加了对照断言：同一份
  投影配置，只差 `build_embedder` 的返回值，能接地时两张图都在、`grounding_unavailable`
  为 `None`。没有这一对，"只挂 v2"在一个悄悄不再注册 v1 的装配下也会通过。
- `test_an_ungrounded_worker_completes_a_v2_task_that_names_no_knowledge_base`（真
  PostgreSQL）：这条才是缺陷的用户可见后果。一个不指名知识库的 v2 Task 走完
  `understand → work → review → export` 到 `succeeded`，`export_ref` 非空，
  **`evidence_refs` 为空**（证明没有走 v1 那三个 `research is None` 的 fallback、没有把模型
  自述当检索证据写进去），且同一个 Worker 上的 v1 提交 park 成 `waiting_migration` 而不是
  拿着 fallback 跑完——docstring 里最要紧的那句，此前同样零覆盖。

## 2026-08-11 Word 任务第一次端到端跑通，代价是修掉四个各自独立的缺陷

起点是两个使用者报告的问题：Chat 搜不到东西，Word 文件只能下载不能看。查下去，
两条线各自牵出了在它们后面的缺陷——其中两个会**静默地**让正确的行为失败。

最后 `task_3ae4d5a0…` 走完了 `understand → work → review → export → succeeded`，
控制台里点开附件栏的 `.docx`，正文与表格直接渲染出来。这是这条链路第一次通。

### 一、搜索没坏，是它说了假话

DeepSeek 的 `web_search` 一次返回 **19 个真实源**，`_fetch` 把 19 个全部读成空，
最后返回 `()`，工具对模型说 "No results"，模型于是告诉使用者"搜索没有返回结果"。

真实原因是这台机器的 fake-IP 代理把所有域名解析进 `198.18.0.0/15`，
`address_guard` 按设计（ADR-027 §3.2）拒绝一切非公网可路由地址。**代码没有一处
出错**，但"搜索一无所获"和"找到了源却一个都读不回来"被折叠成同一个空返回值，
于是一个网络配置问题伪装成了"你的问题没人写过"。

修法是让这两件事可区分：`ports/research.py` 新增 `SourcesUnreadableError`
（放在 port 而不是 adapter，因为这是每个调用方都要做、又都做不了的区分），
按 `refused` / `unreachable` / `http_error` / `no_text` 分类计数。Chat 与 Task
两条路径都改了措辞。修复前后模型的原话：

| | 模型说的话 |
|---|---|
| 修复前 | 搜索没有返回结果，因此我无法提供… |
| 修复后 | 我尝试搜索，但**搜索结果页面无法正常获取内容** |

失败详情里直接写着 `refused=5`，一眼能看出是地址闸门拒的。**根因仍在使用者那边**
（代理要切规则模式），这里修的是可诊断性。

### 二、reviewer 的结构化输出：一个正确的判断被三件事联手废掉

`review` 节点反复以 `critic output has an invalid shape` 失败。那条输出**本身完全
正确**——决定对、字段齐、issues 非空——挂在一条 **376 字符**的 issue 上，而
`issues` 的元素类型是 `ShortText`，上限 **256**。

`ShortText` 是这个领域给**标识符**用的（`model_id`、`reason_code`、`profile_name`），
`issues` 是唯一拿它装自由散文的地方，而那散文要交给下一个 agent 当工作指令。256
不是"一条 issue 该多长"的判断，是顺手复用。

放大它的两件事都在提示词里：

1. **prompt 说了假话**：它写着 "the next attempt sees your issues and nothing else
   about this review"。投影代码同时传 `summary`（4096）和 issues。模型信了，把完整
   解释塞进那个只有 256 字符的字段。
2. **示例模板演示了一个非法形状**：`{"decision":"pass|revise", …, "issues":[]}`，
   而校验器规定 `revise` 时 issues 必须非空——照抄模板填 revise 必然失败。

而这类失败**不可重试**：ADR-034 只让 framing 错误走纠正轮。那条边界没有动——它的
分界线"是不是一个 JSON 对象"是形式的、可判定的，改成按值的错法分类就软了；何况
前三条修对之后模型不会再超限。

新增 `ReviewIssue`（512，覆盖实测最长 376 有余量，仍是 `ReviewSummary` 的 1/8），
删掉假话，模板改合法。实测：

| | 修复前 | 修复后 |
|---|---|---|
| issue 长度（最短/中位/最长） | 65 / 181 / 376 | 66 / 104 / 104 |
| 超 256 的比例 | 1/10 → 硬失败 | 0 |
| summary 使用 | 几乎不用 | 190–280 字符 |

中位数降 43%，且**是写作分配变了**——模型开始把解释放进 summary。不是"上限放宽了
所以不撞"。

### 三、一个中文文件名会永久毒化工作区

修完 reviewer 再跑，它还在打回，说"所有工作区工具均返回验证错误"。
`workspace_write name='季度总结.docx'` **成功了**，此后**每一个**工作区操作都失败，
错误永远指向那个名字——包括写一个完全合法的 `quarterly_summary.md`。

根因是 `WorkspaceManifest.with_entry` 结尾的 `model_copy`，它**不跑校验器**。而
`workspace.write` 的 docstring 声称"the manifest constructor is what enforces the
name"——那句话是错的。非法名字进了持久化 manifest，之后每次 `load` 都炸，而 `write`
第一步就是 load，所以工作区**不可恢复**，错误还指向一个早已返回的调用。

改成 `model_validate`，并给工具 schema 补上 pattern 和一句人话（"写
`quarterly-summary.docx`，不要写 `季度总结.docx`"）。模型下一次就用了 ASCII 名字。

**这是"写入端放行、读取端才炸"的典型**：既有测试测的是 `WorkspaceManifest(...)`
构造函数，而生产代码走的是 `with_entry`，缺陷正是从这条缝里漏出去的。

### 四、我自己引入的：路由能返回的目标，边表里没有

让导出审批可配置（ADR-038）之后，第一个真实 Task 死在 `KeyError: 'export'`。
`route_review` 能返回 `"export"` 了，而 `add_conditional_edges` 的目标表只列了
`work` 和 `approval`。**所有路由单测照过**——它们直接调路由函数，而 LangGraph 是
拿返回值去查那张表的。

和第三条同源：测了一条路径，生产走另一条。所以新测试**编译并真的跑一遍图**，那是
唯一能暴露它的地方。接着又暴露两处同源遗漏：`TaskState` 的 `export_ref` 不变量、
export 节点自己的前置检查，两处都改成按 Task 冻结的 `export_requires_approval`
判断（见 ADR-038 §2.4）。

### 顺带：docx 可以看了，附件栏可以点开了

- 服务端提取 `.docx` 文本（`GET /v1/artifacts/{id}/preview`，仅 docx，415 拒其它）。
  放服务端是因为 docx 是 zip+XML，放浏览器意味着给每次页面加载塞一个 zip 库和 XML
  解析器，去重新推导 API 用同一个库（`python-docx`，Word MCP 的渲染依赖）已能产出的
  文本。走 body 的 XML 子元素而不是 `document.paragraphs` + `document.tables`，
  否则段落与表格的相对顺序会全错。
- Task 产出的 `.docx` 的 kind 是 `tool_result` 而非 `report`（`report` 是 export
  节点导出的 markdown 草稿），所以它只在右侧附件栏。附件栏现在按类型分流：能显示的
  在阅读列打开，其余下载。
- Chat 的工具列表一直在 `RunStarted.tool_names` 里，但 `RunStarted` 与 `RunCompleted`
  共用 activity key，**完成事件把开始事件整个覆盖**，`tool_names` 随之消失——恰好在
  使用者会去看的时候（turn 结束后）列表是空的。`ToolFailed` 同理（它的 payload 里
  没有 `tool_name`）。把 `carryPrompt` 泛化成 `carryForward` 并在补齐后重算标签。

### 口径

- `workflow.export_requires_approval` **默认 `true`**，只有 `config.local.toml` 与
  `config.word-local.toml` 关着。改默认值要动 ADR-038 §2.1，不是改一行配置。
- 能力表可以声称 **.docx 内联预览**（服务端提取，有测试与实测）。**不得**声称把
  docx 渲染成排版——它是文字预览，界面上也这么写。
- `config.word-local.toml` 补了两处预算（token 16000 → 120000、`max_steps` 12 → 40），
  都有实测依据写在配置注释里。**这不是调参**：v2 的 `work` 节点一次调用要读工具、
  写文档、渲染，默认值是给"读输入然后回答"的节点定的。
- 搜索能不能真的搜到，仍取决于使用者的网络（fake-IP 代理下永远读不到页面）。这轮
  修的是**说实话**，不是修好搜索。

### 证据

后端 **2629 项** passed（真 PostgreSQL 5433 + 真 Qdrant 6333，11 项因缺 embedding
extra 跳过），前端 **114 项** passed；ruff / pyright strict / tsc / eslint 全绿。
CI 四项全过，含对着真服务那条。

每处修复都做了破坏验证。其中一次值得记：docx 的顺序测试第一次破坏**没有变红**，
因为断言用 `index("指标")` 命中了表格单元格里的段落副本——改成按渲染出的表格行
定位才真正有牙。**破坏验证没红，先怀疑测试而不是庆幸。**

## 2026-08-10 图谱轨收束：建成了，四轮消融说不要开

ADR-037 的决定被完整实现——三张表、抽取第二遍、种子扩展臂、消融装置，都有测试和
实测证据。**然后测量说它让检索变差，所以它没有被启用。**

| 机制 | 结果 |
|---|---|
| 查询 → 实体名（§2.1） | 桥接实体在查询里出现 **0/7**，一道也修不了 |
| 种子扩展，共现计数 | `full_coverage@3` **−4 道**（43 → 39） |
| 种子扩展 + 特异性加权（IDF） | **−2 道**（43 → 41），判据未达成 |
| 查询 → 关系描述 | 匹配话题而非实体链，会高置信度提名**错误**文档 |

第四条最能说明问题：问「Retrieval Service 读的集群有没有备份」，最高分关系是
`scratch cluster → aw-core`（0.6363）——指向**错误的集群**，因为查询里的显著词是
"backups"，而 `aw-core` 那篇正好在讲备份。

### 两个根因，都有测量

1. **合并键不匹配**：`marlin` vs `team marlin`、`osprey` vs `team osprey`。需要桥接的
   文档对里实体根本没合并——7 道题只有 2 道的桥接实体真跨到了两侧。
2. **没有任何路径携带查询相关性**。共现、特异性、关系描述相似度都不知道"这条连接
   是不是问题问的那件事"。用 dense 相似度补这个洞是**自我否定**的：桥接文档之所以
   需要图谱，正因为它离查询远（实测想要与噪声的分布完全重叠）。

### 两处我在实现中被自己抓到的错

- 初版把提名过滤成"只保留其它臂已返回的 chunk"——**那会废掉整条臂**
- `fetch` 用 `scroll` 返回 `Record` 没有 `.score`，复用 `_scored` 抛
  `AttributeError`，而被调用处的 `except Exception` **当成"图谱不可用"静默降级**。
  测试抓到了，但诊断被拖慢；已把索引移出 try（它不是可选依赖）

### 口径

- **`rag.graph.enabled` 保持 false**，能力表**不得**声称图谱检索可用
- 基础设施建成且有测试，缺的不是代码**是证据**
- 重开的前置条件是**先修抽取质量**（让 `Team Marlin` 与 `Marlin` 归一），再谈打分。
  在根因未动的情况下试第 5 个变体是调参不是工程
- 顺带产出的可复用装置：`--dump-outcomes` 逐题归因、`--collection` 索引复用、
  `--arms` 窄跑、抽取产量闸门（mentions 为 0 时**拒绝测量**，今天真的救了一次）

## 2026-08-10 图谱臂消融：它让检索变差，闸门拦住了它（B4）

一份索引、一份金集，两臂只差图谱。抽取产量健康（49 chunk 全读成功、138 个提及、
78 条关系），所以这不是"图谱不在"。

| 指标 | hybrid | hybrid+graph | 变化 |
|---|---|---|---|
| recall@1 | 0.8846 | 0.8846 | ±0 |
| recall@3 | 0.9615 | 0.9423 | −0.019 |
| **full_coverage@3** | **0.8269 (43/52)** | **0.7500 (39/52)** | **−0.077（−4 道）** |
| MRR | 0.9199 | 0.9103 | −0.010 |

对照臂与已提交基线**逐位相同**，差异只能归因于图谱臂。

### 我的预测错在方向

跑之前基于实体表预测 **+2**（7 道里恰好 2 道桥接实体两侧都在）。**实际 −4。**
逐题 diff：1 道变好，4 道变差。

### 根因：枢纽实体拿到了满权重

4 道变差形状一致——`component-event-log`、`component-retrieval-service` 反复挤进
top-3 顶掉正确答案。`aw-core` 横跨 5 篇文档，`postgresql`/`qdrant` 各 3 篇；种子
提到 `aw-core`，扩展就提名 5 篇组件文档。而 RRF 给图谱臂 rank-0 的贡献是
`1/(2+0)=0.5`，**与 dense 臂最佳命中等权**。

**这条臂没有查询相关性概念**：`expand_from_seeds` 按"几个种子实体到达该 chunk"
排序，那是共现计数不是相关度，而共现在枢纽实体上极其廉价。

### 但机制本身能work

唯一变好那道：hybrid 的 top-3 里**根本没有** `runbook-index-rebuild`，+graph 把它
提到第 1。所以错的不是"种子扩展"这个想法，是**"按共现计数排序 + 满权重进 RRF"
这个实现**。

### 口径

- `rag.graph.enabled` 保持 **false**；能力表**不得**声称图谱检索可用
- B4 这道闸门就是为这种情况设的：先量再开，不是建完就开
- 合并键问题（`marlin` vs `team marlin`）仍在，但**不是瓶颈**——即使 5 道全可达
  也抵不过枢纽实体的 −4
- 候选修法见 ABLATION.md，首选按实体特异性（IDF）加权，直接打在根因上，未验证

## 2026-08-10 种子扩展臂落地（B3，ADR-037 §2.7）

按 §2.7 而不是被推翻的 §2.1 实现。方向是这轮的全部要点：桥接实体在 doc A 里
（7/7），不在查询里（0/7），所以臂是「dense/sparse 的 top-N → 反查它们提到的
实体 → 提名该实体的**其它** mention」。

- `expand_from_seeds`：一条自连接完成跳转；**种子在查询里就被排除**，不是事后
  过滤——把已命中的东西再提名一次，等于在按名次计分的融合里给它第二票，而
  `limit` 还会被这些重复占满
- `VectorIndexPort.fetch`：提名只有 id（图谱存溯源不存正文），必须从索引取回，
  **不能**从其它臂的输出里筛——那部分恰恰是这条臂唯一不关心的，按那样实现它
  只会重排、永远够不着。带 ACL 收窄，否则图谱臂就是绕过索引收窄的口子
- 迁移 0023：`kg_mentions` 加 `(tenant, kb, chunk_id)` 索引（0022 的两个索引
  都是另一个方向）
- 三条**原始**臂进同一次 RRF；种子用于提名候选，不是第二次融合

### 两个自己踩的坑，都记在代码里

1. 第一版把提名过滤成"只保留其它臂已返回的 chunk"——**那会废掉整条臂**。
2. `fetch` 用 `scroll`，返回 `Record` 没有 `.score`，复用 `_scored` 直接抛
   `AttributeError`；而调用处包了 `except Exception`，把这个 bug 当成"图谱
   不可用"**静默降级**了。核心测试抓到了它，但诊断被拖慢。现在索引**不再被
   try 包住**——dense/sparse 刚用同一个客户端成功过，那里失败是缺陷或已波及
   其它臂的故障。顺带删了写了从没被读过的 `_degraded`，并改掉模块文档里
   "mode 会说明实际跑了哪种形态"这句与 `mode` 文档自相矛盾的话。

### 证据

对真 Qdrant + 真 PostgreSQL：核心测试复现实测失败的形状（锚文档带查询词、
桥接文档与查询正交、干扰占满其它臂名额），配对照组（图谱身份不匹配 → 桥接
够不着）。破坏验证：去掉种子排除、去掉 ACL 收窄，各自对应测试变红，还原后
5 项全绿。无服务 1965 passed；实服务 947 passed；ruff + pyright strict 全绿。

### 口径

`rag.graph.enabled` **仍为 false**。还差：ingestion worker 的第二遍分支接线、
抽取产量的 go/no-go 报告、以及 B4 消融。消融时必须先确认图谱**有行可贡献**
——否则"图谱没用"和"图谱不在"会产出同样的数字，这一点已写进检索器的模块文档。

## 2026-08-10 逐题归因推翻了图谱臂的设计，并更正了一个数量级

B1+B2 合并后（#104），趁 B3 动工前先收了 ADR-037 里那个标着"未验证"的机制假设。
收对了：**它确认了假设，同时推翻了修法。**

### 7 道题的形状完全一致

`--dump-outcomes` 写出逐题明细（**独立文件**；score 报告一个字节不动——那是跨
改动逐位比对的依据，让它长出题目载荷就等于废掉这种比对）。

- doc A（带查询表面词的那篇）**每次都在第 1 位命中**；
- 缺的**永远是桥接目标**（team-kestrel、team-marlin、store-aw-vectors…）；
- top-3 剩下两位被**和 doc A 词汇相近的文档**占满。

### 此前没测过的那一列

| | |
|---|---|
| **查询**里含桥接实体名字 | **0 / 7** |
| **已命中的 doc A** 里含它 | **7 / 7** |

查询里没有 `Kestrel`、`Marlin`、`aw-vectors`——它描述 doc A 的特征，问的是
doc A 指向的那个东西的属性。所以 ADR-037 §2.1 画的"查询→实体名→mention→chunk"
**一道题也修不了**；关系臂按"提名边被读出来的那个 chunk"实现同样打偏，那个
chunk 就是 doc A。

§2.1 已标注为被推翻，新增 §2.7：图谱臂是**种子扩展**——从 dense/sparse 的 top-N
反查种子提到的实体，提名该实体的**其它** mention。**融合仍只有一次**：种子用于
提名候选，不是第二次融合；ADR-033 禁的是把已经融合过的结果再融合。代价是扩展
臂依赖前两条臂的输出，不是可并行发出的对等臂，而是第二阶段。

### 顺带更正一个数量级

机器空闲时重跑：hybrid 延迟 **21 193 ms → 172 ms**（dense 48 ms）。123 倍差别
全部来自 swap 压力。质量指标两轮**逐位相同**，只有延迟行变——既是可复现性证据，
也说明质量指标确实不读时钟。此前 ABLATION 里"hybrid 比 dense 高两到三个数量级"
是当时机器状态的真实观测，**但不是这个检索器的属性**：真实代价约 3.5 倍。

### 口径

- B3 尚未动工，且**必须按 §2.7 而不是 §2.1 实现**；`rag.graph.enabled` 仍关闭
- 这一环先收是对的：不收它，B3 会照着 §2.1 建两条臂，跑完消融发现没涨再从头查

## 2026-08-10 图谱轨 B1+B2：融合下沉、只提名 chunk 的存储与抽取（ADR-037）

B0 造出了失败，这一轮把能修它的机器搭起来——但**没有接线，开关仍关**。

### B1：融合下沉（行为等价）

图谱臂要和 dense/sparse 进**同一次** RRF，而融合逻辑锁在 Qdrant 适配器里，
且 `search_hybrid` 返回的是**已经融合过**的列表——从它出发再融合就是融合两次。
`ranked`/`fused`/`RRF_K` 下沉到 `adapters/vector/fusion.py`（只有纯逻辑走，
Qdrant payload 映射留在适配器），新增裸的 `search_sparse`。

**`search_hybrid` 的守卫语义一字不动**：它刻意不带 `search` 的空 principals
短路与 `limit<1` 拒绝，用公开方法组合它会把行为变化混进一次重构。共享落在
两者真正相同的那一层（查询 + 定序）。

等价证据：`test_tied_score_order` 对着真实 Qdrant 把融合分数钉到精确值
（`1/2+1/2`、`1/3+1/2`）并穿过重构后路径通过；dense 臂评测报告四个质量指标
**逐位相同**（0.8462/0.8558/0.7885/0.9423），只有延迟行变。

### B2：存储与抽取

与港大 LightRAG / RAG-Anything 的分界（ADR-037 §2.2）：它们把跨文档同名实体
**合并成同一个节点**，那正是图能做全局摘要的原因，也是不能照抄的原因——合并
之后"这条知识来自哪份文档"就没有了，而整条授权链建立在按 `document_id` 复核
之上。本项目实体在 KB 内合并以获得跨文档连接，但每条知识经 `kg_mentions`
逐条锚回具体 chunk。**合并的是索引入口，不是证据。**

- 迁移 0022 三张表 + 拓宽 `outbox_events.kind`（第二遍免费获得现有
  claim/lease/heartbeat/重试）；downgrade 先删未 ack 的抽取请求再收窄约束
- 读永远按 `graph_identity` **在查询里**收窄，不是取出来再判
- 一个 chunk 只投一票：RRF 数名次，提到三个命中实体的 chunk 若出现三次会压过
  匹配更好但只出现一次的 chunk
- 第二遍的 chunk **重新派生**而非随事件搬运——chunk id 是
  `index_identity|document_version|ordinal` 的哈希，重切同一份字节必然落在
  已索引的那批 id 上，于是 outbox 行只需带文档版本
- `unreadable_chunks` 与"什么都没提到"分开计数：一份合并两者的报告会让坏掉的
  供应商看起来像一份平淡的语料
- `rag.graph` 默认关闭，两个冻结 Literal；schema 1.12 → 1.13

### 证据与口径

- 无服务 1965 passed；实服务 942 passed（含图谱 8 项对真库、迁移往返、
  metadata 一致性）；ruff + pyright strict 全绿
- 三处破坏验证均先红后绿（`search_sparse` 词项移位、`graph_identity` 收窄移除、
  每实体投一票），还原时清了字节码缓存
- 过程中修掉我自己一条**名不副实**的测试：Qdrant 对不匹配的稀疏查询照样返回
  带稀疏向量的点（打 0 分），所以"只找词面重叠"其实只证明了"查的是稀疏向量"
- **hybrid 端到端等价评测未跑完**：这台机器上 hybrid 每题约 21 秒，2.5 小时
  未完且把 swap 顶到 7 GB，主动停掉。它要确认的事已由上面两条更精确的证据
  覆盖；未完成不记为已完成
- **尚未做**：worker 的第二遍分支接线、两条提名臂与四臂融合、抽取产量的
  go/no-go 报告。图谱开关因此保持关闭，能力表不得写 Implemented

## 2026-08-10 跨文档证伪成功：hybrid 一半的多跳题只返回半个答案（B0）

图谱臂（ADR-037）此前没有测量依据——38 题金集上 hybrid 已经 1.000，量不出
任何改进。B0 的任务是**先造出失败**，造不出就不建图。造出来了：

| 指标 | dense | hybrid |
|---|---|---|
| recall@1 | 0.7885 | **0.8846** |
| recall@3 | 0.9423 | **0.9615** |
| MRR | 0.8558 | **0.9199** |
| **full_coverage@3** | **0.8462** | 0.8269 |

**hybrid 三个单命中指标全面优于 dense，唯独"答案到齐"更差。** 精确归因：
单文档题的 `coverage_rank` 恒等于 `rank`，所以「coverage 失败 − recall 失败」
就是只捞回一半答案的题数——dense 5 道，**hybrid 7 道，占 14 道跨文档题的一半**。
机制假设（sparse 把 top-3 都推向含题面词的文档、挤掉桥接文档）**未验证**，
逐题归因属于 ADR-037 的前置工作。

### 装置

- `full_coverage@k` 进指标注册表：跨文档题必须**每一篇**期望文档都进 top-k
  才得分，拒绝部分命中；单文档题上退化为 recall@k，所以两个数字的差**只可能**
  是跨文档题在失败。`RetrievalOutcome` 的 `expected_document_id` 改为
  `expected_document_ids`，新增 `coverage_rank`。
- 金集加 `document_ids` 多文档形式，loader 双形式兼容并带校验（两种形式同时
  出现、空列表、重复文档各自报错并指到行号）。
- 语料 10 → 49 篇：组件/团队/存储/runbook/配置/SLO/迁移，外加**只共享表面词、
  不含桥接实体**的干扰文档。top-3 从占语料 30% 降到 6%。
- `run_rag_eval.py --paths`：单路径把墙钟减半；关键是单路径下 ADR-017 的等价
  检查会**静默通过**（`quality[1:]` 天然为空），所以改成按数量显式守卫并在
  任何数字之前打一行 `NOT COMPARING`。

### 作废的中间版本，记下来

同日更早一版跨文档题测得 `full_coverage@3` 与 `recall@3` **完全相等**（0.9808），
缺口为零：题目是"拼接"（同时点名两个文档的关键词，两半各自可检索），语料又只有
10 篇、top-3 即 30% 的语料。那版金集未提交、无法复现，其报告已从工作区还原。

### 口径

- **延迟不作数**：hybrid 中位 21.2 秒，落在历史 7.2–27.9 秒区间但偏高；整场
  会话机器处于内存压力（一度 swap 14.28/15.36 GB），这是机器状态的测量不是
  检索器的测量。质量指标不读时钟，不受影响。
- 本轮只跑 `reference` 生产路径，**没有**产生 ADR-017 第 2 步的等价证据，
  运行自己在 stderr 声明了这一点。
- 门禁：无服务 1916 passed；评测装置 29 项；ruff + pyright strict 全绿。

## 2026-08-10 提交预判（triage）服务端落地：模型判形态、判不准问人、失败回落默认（ADR-036）

上一条记录里"界面不猜"的立场被正面推翻，且是有据推翻：那个决定其实一直在被猜——
web 的 `REPORT_WORDS` 正则、CLI 的另一份关键词表、API 的 `false` 默认，同一句目标
三个入口三种行为，猜完不留痕。ADR-036 取代 ADR-031 §2.3 的"不做自动路由"，论证的
不是"模型不会猜错"，而是猜错的前提变了：**判定发生在 Task 存在之前**（独立无副作用
端点，判不准返回问题而不是硬猜）、**可见**（理由进 TaskSubmitted）、**可兜底**（超时/
失败/未启用回落部署默认）、**可覆盖**（显式选择跳过判定）。冻结与幂等语义一字不改：
提交端点永远收显式值或缺省，"auto" 不存在于提交语义里。

### 落了什么

- **`POST /v1/tasks/triage`**：入参 objective + 是否选了知识库 + 附件名；出参
  `decided`（graph、wants_report、一句理由）／`ask`（仅 graph 判不准：一个问题 +
  两个选项）／`default`（一切失败路径，客户端按原样提交）。wants_report 判不准取
  false 不问——两张图的审批拒绝终态都是 failed，拿不准就强开审批等于逼人"批准或
  失败"。
- **`application/task_triage.py`**：toolless 单步结构化调用，deny 信封；解码复用
  ADR-034 边界——`workflows/structured_output.py` 从 task_handlers **提升**了
  `json_object` 与 `restatement_messages`（行为逐字不变，两处共用），framing 失败
  补问一次，说错话（能解析但不合契约）直接 default 不追问。
- **intent 溯源**：`TaskSubmission.intent`（user|model|default × 2 + reason）——
  不进列、不进幂等身份，只进 `TaskSubmitted` 事件。发现并绕开了两个真实陷阱：
  ①给 `TaskInput` 加任何可选字段都会让存量任务的 fingerprint 复核全体失败
  （canonical_bytes 不排除默认值）；②事件按 key 去重时比对 payload，重试带着
  不同 intent 会把幂等重试变成 `EventKeyConflictError`——为此
  `append_durable_in_transaction` 增加 `first_write_wins`，提交事件首写为准，
  不再比对一个重试本就无法复现的字段。
- **配置**：顶层 `[triage]` 段（enabled 默认 **false**、timeout_seconds 8s），
  default_factory 起步所以存量配置文件全部原样有效；config_schema_version
  1.11 → 1.12；ownership.yaml 登记，**不进** task 快照 allowlist（提交前的决定，
  与 graph 同理）。web-local 配置打开它供本机验收。
- **`evals/triage/`**：24 条三分类金集（清晰调研/清晰执行/真含糊）+
  `scripts/run_triage_eval.py`（真模型、温度 0、按类准确率 + default 计数入报告）。
  开关默认关闭的理由就是这份报告：部署先看自己模型的数字再开。

### 两端接入（同分支后续两个提交）

- **web**：主表单只剩目标；radio、报告开关与 `REPORT_WORDS` 删除；创建点击后
  decided 直提、ask 渲染问题与两个 chip（点选即显式提交、记 user）、default
  按部署默认；显式覆盖进高级设置（执行方式 自动/调研/通用 + 报告文件
  自动/要/不要），显式值跳过 triage 且优先；任务详情从 TaskSubmitted 读回
  执行方式与判定来源。eslint 0 / tsc 干净 / vitest 102 项 / build 通过。
- **CLI REPL**：`/graph` 增加 `auto` 档并成为默认——提交前 triage，判定打一行
  可见理由；ask 时交互会话终端里问一句（回车=部署默认），管道会话说明后取默认；
  `research|general` 钉死跳过判定，`default` 完全回到旧行为（无 triage、无
  intent）。`_mentions_report` 关键词表删除。tests/cli 74 项全过。
  非交互 `agent-workbench task submit` 保持确定性不 triage。

### 证据

- 无服务全量：1934 passed / 608 skipped；真实 PG(5433)+Qdrant(6333)：
  929 passed / 2 env skips（含新增：triage 服务 12 项、API 路由 3 项、
  注册表 intent 落事件 1 项、结构化输出提升对照 9 项）。
- ruff format/check 与 pyright strict 全绿。
- **triage 准确率（真实 DeepSeek，温度 0，24 题金集）**：清晰调研 10/10、
  清晰执行 10/10、**含糊 0/4——模型从不回答 unsure，全部硬判**（三题判
  research、一题判 general）。ask 机制本身由单测与 API 测试证明可用，但
  deepseek-chat 在当前提示词下不触发它。这份报告
  （`evals/triage/reports/report.json`）就是 `triage.enabled` 默认保持
  false 的理由；含糊题要么接受硬判（有溯源、可覆盖、可重试），要么后续
  调提示词再测。
- **真实端到端验收**（本机 API + 在跑的 task worker + 真实 DeepSeek）：
  web 表单输入"把这批会议纪要按议题去重合并成一份纪要"，triage 判定
  general（理由"这是一个文档处理任务，需要合并去重，属于执行类操作。"），
  Task 冻结 `v2_general`，`TaskSubmitted.intent` 完整记录
  `{model, model, reason}`（task_1ebcc1dde8d3…），worker 领取并跑通 v2
  工具循环（work 节点 32 步、review 出 revise 决定）。三条 triage 路径
  另以 curl 验收：清晰执行/清晰调研各自判对并给出中文理由。

## 2026-08-10 graph 字段接进 CLI 与 web 控制台，附带一次到目前最深的 v2 真实验收

PR #98 合并后，选 v2 只能直接调 API。这一轮把选择接进两个人用的界面，并顺手把
"v2 尚无真实模型端到端验收"这个口子推进到了一个新的、如实记录的位置。

### 接进去的方式，两边刻意不同

**CLI 不替用户选，web 替用户选默认。** repl 的 `/graph research|general|default`
是会话级设置，**不选就不发字段**——部署默认是服务端的决定，一个总是发送自己
"以为的默认"的客户端会把这个以为冻进每个 Task。`agent-workbench task submit --graph`
同理，省略即省略。web 表单则是两个单选钮（调研报告/通用执行）**总是发送可见选中项**，
默认调研报告：表单上显示一个值、实际提交另一个值（服务端默认）是界面在说谎。两边
的不同各自成立，都写了注释。

**没有自动路由，界面也不猜。** web 有一条专门的测试：objective 写着"调研"两个字、
选的是通用执行，提交的就是通用执行——措辞不决定流水线（ADR-031 §2.3）。

**重试忠实于原流水线。** 失败重试从 `TaskSubmitted.graph_version` 读回选择
（`findGraphChoice`：v1→research、v2_general→general、认不出的版本→不发字段取默认）。
graph 刻意不在 TaskInput artifact 里——走哪条流水线是提交的属性不是输入的属性——
所以时间线事件是唯一能忠实重试的来源；丢掉它的重试会把 Task 静默换图。

**两边的进度渲染都认识 v2 的节点。** CLI 一张 superset 阶段表（只渲染收到事件的
阶段，所以两图各自顺序正确）；web 按图声明两张阶段表，形态从时间线自己读出——
`TaskSubmitted` 带着 graph_version，第一条事件就定了形态，排队中的 v2 任务预览的
是自己的四个阶段，而不是 v1 承诺的检索和撰写。`review` 阶段 id 两图共用，因为
v2 的 reviewer 和 v1 的 critic 对读者是同一步。

### 本机真实验收：比以往任何一次都走得深，最后停在两个已知问题上

环境：真实 DeepSeek + 本机 PG 5433/Qdrant 6333，API/Worker 重启到 main（含 #98）。
从 web 控制台选「通用执行」提交（POST 载荷与 `TaskSubmitted` 均确证 `v2_general`，
四阶段生命周期正确渲染），真实 Worker 领取执行：

- `understand` 完成（第一次尝试超了 120s 模型时限，`provider_error` 可重试；
  **点重试按钮验证了 findGraphChoice**——重试的 Task 再次冻结 `v2_general`）；
- **`work` 跑了真实工具循环**：workspace_list ✓ → workspace_write 被
  `policy_denied: missing_permission_scope` 拒绝 → 模型适应拒绝、调整、完成报告。
  这正是 ADR-031 §2.2 说的那个节点第一次在真实模型下自己决定下一步；
- **`review` 用了只读工具**（workspace_list、workspace_grep）核对工作区——
  reviewer 的工具设计首次被真实模型实际使用；
- 然后 review 的最终消息不是严格单 JSON，`decode_review_output` 判节点失败，
  整个 Task 失败。

两个拦路的都不是这轮的改动，也都有档案：`missing_permission_scope` 就是本页下面
"一次没做成的真实验收"里记的同一件事（提交的 principal 没有 `workspace:write`）；
reviewer 的 JSON 脆弱性与那次记录的 research_external 缺陷同类，reviewer 更易踩中
（带工具、多轮循环后模型倾向写散文）。

### 收口：ADR-034 变基进来之后，v2 第一次真实端到端成功

上面那次失败之后，ADR-034（PR #99，一次纠正轮次）落进 main。本分支变基到它上面，
Worker 重启，同一个 objective 复验两次：

- **不带 scope**（对照，task_9425…）：work 在 scope 拒绝的反复解释里撞
  `token_budget` 上限而失败——预算按 ADR-030 生效，review 未被走到；
- **带 `x-principal-scopes: workspace:write`**（task_4313…）：**succeeded，17 秒**。
  时间线（43 条事件）同时补上了两条悬置的证据：
  - seq 16–19：`workspace_write` Proposed → PermissionResolved → Started →
    Completed——**带 scope 的成功路径首次有事件流记录**（此前"仍无事件流证据"）；
  - seq 23–38：review 第一轮带工具核对工作区（list、read），最终消息仍带叙述；
    **seq 39–42：纠正轮次实战首次命中**——独立 run，`steps: 1, tool_calls: 0`
    （ADR-034 §3.3 说的"够不到任何东西、只能重述"），解码成功，TaskSucceeded。

当初立项修 reviewer 的那个缺陷场景（task_9a59…：工具轮之后叙述式收尾）在复验里
**原样重演并被纠正轮次接住**——不是"没再发生"，是发生了且被设计接住，这比前者是
更强的证据。破坏验证：拆掉 `_decoded` 的 framing 落空分支 → reviewer 那条测试
（`test_a_narrated_verdict_…_at_both_reviewers`）恰好红，"本来就是 JSON 不买第二轮"
的对照保持绿。

**所以现状的准确说法是**：v2 从两个界面都能选中提交、被真实 Worker 领取，并且在
带 `workspace:write` 的 principal 下**有了第一次真实模型端到端成功**。仍然真的：
不带该 scope 的提交会在 work 的反复被拒里烧穿 token 预算而失败——本地控制台身份
默认不带任何 scope，这是下一个要么给默认、要么在界面暴露的决定，未在本轮处理。

测试：Python 1895 passed / 604 skipped（无服务），web vitest 99 passed + tsc +
eslint 全绿。CLI 侧钉住 v2 节点的阶段归属与顺序、/graph 三态、不选不发；web 侧钉住
findGraphChoice 映射、graphShapeOf 三来源、v2 四阶段声明、以及"措辞不决定流水线"。

## 2026-08-10 答案不是摘要：4096 那个上界切掉的是整张图的产物（ADR-035）

上一条末尾"单独立项"的那个数字矛盾，查下来比记的那笔严重。**不是外部研究节点多付一轮
的问题，是每一份报告都被切在 4096 字符。**

实测（真实 `ClaudeLikeAgentRuntime` + `ArtifactPersistingExecutor`，fake 模型）：

```text
model wrote          : 18010 chars
outcome.output_text  : 4096 chars
tail                 : 'ed paragraph. Grounded para… [truncated]'
stored artifact      : 4098 bytes
```

`ArtifactPersistingExecutor` 存的就是 `output_text`，所以那个上界就是 `synthesize` 写的
报告、`export_report` 导出的那份文件的上界——约 600 个英文词，句子从中间断开。Chat 同理：
用户看着 `ModelDelta` 把全文流完，拿到的 `AnswerCommitted` 是截短的。**没有任何地方说过
报告只有两页。**

**做了什么。** 新增 `AnswerText`（65 536），给的是**承载答案本身**的字段：
`AgentOutcome.output_text`、`ModelCompleted.text`、`AnswerCommitted.text`、
`UngroundedAnswerCommitted.text`、`ChatTurnResult.answer`。`BoundedText` 保持 4096 与
它本来的含义：**关于**一次 run 记录下来的摘要（ADR-019 引入的那两个 preview）与流式增量
的一片。这不是新形状——`ToolOutputText` 当初就是这样分出来的。

**ADR-019 没被推翻，是被收窄了。** 它立的"提示词与工具参数要有界"一字未动。它同一节里
还写过"事件流早就在携带模型生成的正文，因为答案本来就要从这里回到提问的人手里"——所以
问题不是答案该不该进事件，是进事件的答案按谁的上界。preview 每步一条，答案每轮一条。

**仍然裁在源头。** `_clip` 留着，只换量尺。在下游裁会发布一份和供应商返回的不一样的
答案，而 `ChatTurnResult` 那条 `answer == outcome.output_text` 的发布围栏正靠源头裁剪
成立。

**契约的数字现在被比着。** `MAX_EXTERNAL_PASSAGE_CHARS` 8000 → 2400，`LOCATOR_CHARS`
800，`20 × (2400 + 800) = 64 000 ≤ 65 536`；提示词插值这两个常量而不再自己抄数字。缺的
从来不是正确的数，是**没有东西在比较它们**。

**破坏验证做过三次**，三个可动的零件各一次：把 `MAX_OUTPUT_TEXT` 改回 4096 → 运行时与
chat 三条红；把 `ANSWER_TEXT_LIMIT` 改回 `BOUNDED_TEXT_LIMIT` → 四条红（含报告 artifact
那条）；把每条上限改回 8000 → 契约那条以 `assert (20 * (8000 + 800)) <= 65536` 的形式红。

**门禁**（本机，真实 PostgreSQL 5433 + Qdrant 6333）：

```text
pytest（--ignore=tests/e2e）   2495 passed / 11 skipped
ruff check / format             passed
pyright                         0 errors / 0 warnings
```

## 2026-08-10 结构化节点读不出来时再问一次（ADR-034）

关掉的是下面"事实二"那条缺陷：`research_external` 因为答案前面多了一句话而杀掉整个
Task。三次复现的那条消息里写着 `{"items":[]}`——ADR-032 §3.3 明文允许的答案。

**选的是"再问一次"，不是"把消息里的对象抠出来"。** 后者会连模型**描述**的 JSON 一起
收下，而"模型自己说出这个包"是 ADR-032 §3.2"只记录工具真的返回给你的内容"唯一的落脚
点——解码器从来没有、也无法核对 URL 是否真的被取过。理由与被否掉的第三个选项（用 tool
call 强制结构化输出）写在 ADR-034 §2 与 §4。

**解码器一个字没放松。** 节点接受的仍然只有"整条消息是且只是一个 JSON 对象"；变的是
读不出来时先把那条消息按模型自己的一轮放回去，要求单独再发一次那个对象。第二轮仍然读不
出来，节点照样失败——ADR-032 §3.3 那条"读不出不能降级成没读到"因此一个字没动。

**分界线是"是不是一个 JSON 对象"，不是"这个对象对不对"。** 新的
`StructuredOutputFramingError` 只覆盖前者。critic 评了另一份 draft、evidence item 的 url
是 "page 3"——这些是模型做出了断言并且错了，不给纠正轮次：那已经不是问"你刚才说了什么"，
而是把模型往"能通过校验的答案"上推，多出来的 locator 只能是编的。

**修在四个节点共用的那一处。** `plan` / `critic` / `review` / `research_external` 用的是
同一个严格解码器，暴露面完全相同；只有 `research_external` 复现过，是因为只有它带着工具
跑、也只有它会因为工具被拒而先解释一句。v2 的 `review` 值得单独点名：它持有三个只读
workspace 工具，而"刚用完工具的模型倾向于先说一句自己做了什么"正是那条消息的成因。

**纠正轮次不带工具，并且是一次独立的 run**：自己的 `agent_run_id`、自己的预算与墙钟，
花第二笔钱之前重新验证 claim。一轮够不到任何东西的对话，不可能带回第一轮没产出过的材料。
摘工具写在共用的运行处而不是各调用点：`research_external` 的工具来自动态目录，`review`
的来自自己的 profile，靠"每处记得摘"迟早会漏，漏的样子是一个能重新翻工作区、给出与被
复述那条不同裁决的 reviewer。

**破坏验证做过两次。** 把"find the trailing `{...}`"写进解码器：
`test_a_json_object_the_model_only_described_never_becomes_evidence` 两个断言各自独立
变红——节点会静悄悄成功，并把模型自称没读过的页面变成可引用证据。把动态工具交给纠正
轮次：`tool_names` 那条断言红。

顺带记两笔实测事实：

- 运行时的 `MAX_OUTPUT_TEXT = 4096` 会把模型答案截断，而 `_EXTERNAL_RESEARCH_CONTRACT`
  写的是"至多 20 条、每条至多 8000 字符"。**这两个数对不上**，外部研究节点实际上装不下
  契约允许的答案。本次不动它（一 PR 一变化），单独立项；
- 因此被截断的答案会在两次 run 之后才失败，而不是一次。这类答案今天同样是失败的。

**门禁**（本机，真实 PostgreSQL 5433 + Qdrant 6333）：

```text
pytest（--ignore=tests/e2e）   2491 passed / 11 skipped
ruff check / format             passed
pyright                         0 errors / 0 warnings
```

## 2026-08-10 阶段五收尾：v2 能被选中、能干活、横切性质在两张图上都被测过

接着 PR-5.1（v2 的节点与边）做完剩下三件事：`work`/`review` 的真实处理器与 agent
profile 并接入 LangGraph 适配器；PR-5.2 提交时选图并冻结；PR-5.3 横切性质对两张图
都成立的结构测试。

### 落地的决定，按重要性排

**一个 handler 工厂供两张图选取。** `build_task_handlers` 产出两张图全部节点，
`build_task_v1_handlers`/`build_task_v2_handlers` 各自按节点清单选取；组合根只调一次
工厂，两张图字面上共享 `understand` 与 `export` 的同一个 handler 对象。选取器对缺失
节点直接断言失败，因为适配器对没供 handler 的节点默认 pass-through——一个打错字的
节点名不是报错，是一个跑完整张图、什么都没做、然后报成功的 Task。

**worker 的修订顺序与 v1 相反，且只能是这个方向。** v1 在 writer 运行**前**清掉
critic 的裁决（`begin_revision`）；v2 的 `work` 带着裁决运行、之后才关闭它
（`revision_update`）——回去改的人读不到"哪里不行"，第二次尝试就是掷硬币而不是修复。
这同时是 `TaskState` 唯一允许的顺序：存着的 review 必须描述当前 revision_count，
"计数已进、旧裁决还在"的状态根本过不了校验。

**reviewer 拿只读工作区工具，是对 v1 铁律的"例外证明"而不是放宽。** critic 不给看
证据，因为那会变成评审研究；v2 的工作区**就是产物本身**，看不见工作区的 reviewer
评的是"对工作的描述"而不是工作。写权限仍然没有：能改工作区的 reviewer 是在重做活。

**图形态是提交时的"形状"选择，不是版本字符串。** `POST /v1/tasks` 可选
`graph: "research" | "general"`，映射进 `TaskService`；直接传 `v2_general` 是 422。
版本解析后存进 Registry 行，这就是冻结：Worker 读行、不读配置，部署改默认值只影响
之后没选择的提交。`graph_version` 本来就在幂等身份里，同 key 换图重试是 409 而不是
静默返回旧 Task。**没有自动路由**（ADR-031 §2.3）。

**每个版本注册自己的终态措辞。** 两张图在同样两件事上停（修订预算耗尽、人拒绝导出），
措辞各自成文。适配器按"写 checkpoint 的那个版本"读 `terminal_failure_reason`——读错
不会抛异常，只会把 v1 的句子安在 v2 的 Task 上，所有只断言"失败了没有"的测试都发现
不了，所以注册表按版本收拢（`GraphDefinition`）。

**顺手修掉一个真缺陷：故障注入包装器只遍历 v1 节点。** 它的输出会**替换**传入的
handler 映射，v2 的 `work`/`review` 会被静默丢成 pass-through——带故障注入的可靠性
测试跑 v2 时，图照样跑完、什么都不做、报成功。已改为遍历两张图节点并集，测试用
"未武装 controller 逐节点计数"钉住（抽样断言在这个缺陷下会假绿：共享的 `understand`
仍会命中）。

**`CANONICAL_V2_NODE_IDS` 是五个不是四个。** ADR-031 §2.1 数的四个是形状；这个元组是
"v2 checkpoint 可能停在的节点全集"，停在人工审批上的线程正好是第五个。

### 测试与证据

- 本机无服务：1889 passed / 604 skipped；真实 PG(5433)+Qdrant(6333)：
  **2482 passed / 11 skipped**。ruff、pyright 全绿。
- 新增 `tests/workflows/test_cross_graph_invariants.py`：全部按图注册表参数化，
  第三张图注册进适配器而漏掉 handler 清单/节点元组/终态措辞时，注册当天就红。
  `reconcile` 的每个动作（start/resume/wait_for_approval/resume_with_approval/
  settle_succeeded/settle_failed/wait_for_migration）对两个版本各跑一遍，含
  "v1 的 Task 停在只装了 v2 的 Worker 上"这个没人会想到试的方向。
- 真实 handler 契约：v2 全图文本 executor 走通（review 的裁决绑到 work 本轮产出的
  artifact id）；`work`/`review` 都实际进了工作区会话（写、然后另一节点读到）；
  失败的 work run 保留已花费预算。
- 真实中断审批在 v2 线程上：暂停、`inspect` 报出 approval id、批准恢复到导出、
  拒绝恢复到失败——同一个 `TaskApprovalGate` 对象服务两张图（ADR-031 §2.4）。
- **破坏验证 5 组，每组恰好红在守它的测试上**：拆回边→4 条红；包装器改回 v1-only→
  逐节点计数红；v2 注册 v1 措辞→3 条红；工作区门改回节点名清单→会话测试红；
  提交忽略 graph 选择→2 条服务层测试红。

### 仍然不是事实的

- v2 **没有真实模型的端到端验收**：以上全部是确定性 handler 与文本 executor 的证据，
  "一个真 DeepSeek 驱动的 work 节点把一件事做完"还没发生过（本机 fake-IP 代理仍挡着
  读外网，见下一节）。
- CLI repl 与 web 控制台还不会发 `graph` 字段；今天只有直接调 API 能选 v2。
- worker 的动态工具面等于 writer 的（research/synthesis/sandbox 三个 audience），
  没有为它单独收窄或放宽过任何 MCP 服务器声明。

## 2026-08-10 一次没做成的真实验收，和它换来的两条实测事实

目标是补上两条一直没有事件流证据的能力：`download_document` 落库，以及带对 scope 的
`workspace_write`。**两条都没做成**，但失败本身是有内容的，逐条写在这里，因为把它记成
"待验收"会掩盖其中一条真实缺陷。

环境：`config.web-local.toml`、真实 DeepSeek、本机 PostgreSQL 5433 + Qdrant 6333，
API 起在 8010（8000 被另一个进程占着，没有去动它）。

### 事实一：本机在 fake-IP 代理模式下读不了外网，而闸门是对的

`download_document` 与 `fetch_page` 每一次都是 `tool_failed`。在进程内复现，得到的是
`DestinationRefusedError: 'www.w3.org' resolves to an address that is not publicly routable`。

查解析：

```text
www.w3.org:  198.18.0.105(NOT-global)
example.com: 198.18.0.86(NOT-global)
arxiv.org:   198.18.0.106(NOT-global)
```

`198.18.0.0/15` 是 RFC 2544 的 benchmarking 段，也是 Clash/Surge 一类工具 **fake-IP
模式**发的地址。ADR-027 的地址闸门按"默认拒绝、只放行全局可路由"来写，benchmarking 段
正在被拒之列——**这不是 bug，是闸门在按设计工作**。

结论要说准：在这台机器切回真实 DNS 解析（规则模式）之前，**web MCP 的两个工具无法在本机
验收**。它们今天的证据仍然只有 PR #86/#87 那次（`fetch_page` 走完了全流程），
`download_document` 落库**仍然没有事件流证据**。

### 事实二（真缺陷）：外部研究节点会因为"答案前面多了一句话"而杀掉整个 Task

三次提交、三个不同 objective，全部以同一处失败告终：

```text
StructuredOutputError: structured output must be one JSON object
TaskNodeRunFailedError: task node research_external did not produce valid output
```

第三次的 `ModelCompleted.text` 是决定性的：

```text
The tool calls were denied due to permission scope. The objective explicitly states
this is explanatory writing based on existing knowledge and does not require external
sources. I'll return an empty items list.

{"items":[]}
```

**模型给出的正是 ADR-032 要的那个答案**，只是前面带了一句说明。解码器要求整条消息是且
只是一个 JSON 对象，于是这个正确答案被整条丢掉，节点失败，整个 Task 死掉。

这与 ADR-032 立的那条规矩不是同一件事。那条规矩说的是"解析不出来就失败，不要把'读不出'
降级成'没读到'"——它防的是**凭空造来源**。而这里模型明确写了 `{"items":[]}`，那是被
允许的答案。后果是：**只要研究工具这一轮什么都没读到，Task 就必死**，而在 web profile
上那是每一次。

修法必须小心，不能顺手废掉 ADR-032 保的性质（一个读不出来的节点不能看起来像读到了空）。
"抓消息里最后一个 `{...}`"会连模型**描述**的 JSON 一起收下，那正是那条规矩守的口子。
候选：一次纠正轮次、或走 tool call 强制结构化输出。已单独立项，不在本次改动里。

顺带记一笔：第二次提交失败还有我自己的一份责任——objective 里点名了工作区工具，planner
把它们写进计划后交给了够不到这些工具的 `research_external`。**给 v1 图写 objective 时
不要点名某个节点拿不到的工具名。**

### 因此仍未做

- `download_document` 落库：**无事件流证据**（本机环境受限，非代码问题）；
- 带 `workspace:write` 的 `workspace_write` 成功路径：**仍无事件流证据**，因为三次 Task
  都死在 `research_external`，根本没走到 `synthesize`；
- `workspace_edit` / `workspace_grep`（PR #92 / #93）：同上，**只有适配器与领域层证据**。

## 2026-08-10 WP15 阶段四 PR-4.1：成本上限从"存在"变成"能用"（ADR-030 §2.1/§2.2）

`RunBudget.max_cost_micro_usd` 与 `deadline` 从写下来那天起就在，但**没有任何东西把 token
换算成钱**，所以运行时对每一个带成本上限的请求都直接拒绝。那个拒绝是对的——不能执行的上限
不该被当成上限接受——但它留下的结果是：今天真正约束一个 run 的**只有步数**。

对"回答一次"这够用。对一个会迭代的节点这个代理坏掉了：第 3 步可能读 200 字节，第 7 步可能
读 200KB，用步数管它们等于假装两者一样贵。

**做了什么。** 新增 `domain/pricing.py`（纯算术，不做 I/O、不查供应商、不发现价格）；
`[model.*.pricing]` 四项费率，单位微美元每百万 token；运行时按每轮 token 累计花费。
那条拒绝分支**保留**，但条件从"整个运行时没有计量器"收窄成"这个 profile 没配价格"——
并在消息里指名是哪个 profile，让读的人去看配置而不是去看待办。

**一处容易算错的地方，单独记一笔。** `input_tokens` 里**含**缓存命中的部分（供应商口径，
`tests/contracts/test_deepseek_model.py` 钉着：`prompt_tokens=118` + `prompt_cache_hit_tokens=64`
到达时是 `input_tokens=118, cache_read_tokens=64`）。把四个字段当成互不相交的量来计价，
就会把缓存那批 token 按两种费率各收一次，后果是**缓存过的 prompt 比没缓存的更贵**，成本
上限恰好会在那些开了缓存来省钱的部署上最早触发。破坏验证做过：去掉那次减法，3 条测试红，
其中 `test_caching_a_prompt_costs_less_than_not_caching_it` 以最可读的方式失败。

**`max_steps` 域上限 100 → 1000，默认值仍是 12。** 角色改为兜底而不是预算。默认不动是刻意
的：12 是每个 chat run 和每个 v1 节点被实测过的值，为服务会迭代的节点而给所有人调高，会改掉
没人要求改的 run。

**墙钟是"一次尝试"的。** `max_seconds_per_agent_invocation` 在每次 invocation 解析时按当时
的时钟盖成 deadline，不是在组合根里建一个固定值——固定的那个会从进程启动就开始过期，之后
每次调用都是一出生就超时。代价是崩溃重放后的新尝试重新拿满额度；反过来（存进 Task）会让
一个熬过了外部故障的节点永远跑不完。

`config_schema_version` 1.10 → **1.11**。抬版的理由是 `max_steps` 放宽这一半：1.11 的配置
文件可以写 `max_steps = 500`，1.10 的二进制会在校验时拒绝它——这正是这个 pin 存在要抓的
方向。价格是纯增量，但它决定这个部署能不能用成本上限。

**门禁**（本机，真实 PostgreSQL 5433 + Qdrant 6333）：

```text
pytest（--ignore=tests/e2e）   2388 passed / 11 skipped
ruff check / format             passed
pyright                         0 errors / 0 warnings
```

**仍未做**：`workspace_edit`（PR-4.2）、`workspace_grep`（PR-4.3）、阶段五第二张图。
仓库**不出厂任何价格数字**，所以成本上限在任何现有 profile 上都还没被真实 Task 用过——
这条能力今天只有单元与运行时层面的证据，没有事件流证据。

## 2026-08-10 混合检索跨重建索引不可复现，融合移进适配器（ADR-033）

此前把它记成"CI 里一条偶发红的 tie-break 用例"。**这个记法低估了它**：红的不是测试，
是产品——`search_hybrid` 是有 sparse encoder 时的生产检索路径，它的结果跨重建索引不可
复现，而且会把严格最优的候选挤出第一名。

**诊断此前也是错的。** `_ranked` 按 `(-score, chunk_id)` 重排，理由写的是"Qdrant 对分数
相等的点不作承诺"。对 dense 这是对的且有效的（实测 10 次重建索引，次序每次相同）。对
hybrid 无效：Qdrant 的 RRF 按**臂内名次**计分（实测 `Σ 1/(2 + rank)`，rank 从 0 起，用 9
个不同分数逐一验算吻合），一个点在两臂里都并列时，它的名次是引擎内部的任意选择，于是
**融合分数本身就是随机的**。排序发生在分数之后，错误发生在分数之前——后排序够不着它。

同一份 fixture 重建索引 10 次，实测得到 **10 个不同的次序**，`chk_zenith`（cosine 1.0
对 0.9998，严格最优）的分数在 0.643 / 0.667 / 0.700 / 0.833 / 1.000 之间跳，并有 2 次
不在第一位。

**修法**（[ADR-033](./adr/0033-fusion-ranks-are-ours.md)）：`search_hybrid` 从"一次带两个
prefetch 的服务端融合"换成"两次并发的单臂查询 + 适配器里的一次 RRF"。**融合次数不变，
仍是一次**——两臂返回的都是没有被任何人融合过的原始结果，ADR-016 与端口注释禁止的"第二次
融合"没有发生。变的只是臂内名次由谁决定：两臂各自先过 `_ranked`，于是并列点的名次来自
`chunk_id`，重建索引后不变。`k` 保持 2 以免顺手改掉评测量纲。

**并列点共享名次，而不是被发给连续名次。** 两种写法都可复现，所以这一条不是为了确定性：
连续名次会让一个**没有分辨出高下的臂**去给次序投票。单词查询下 sparse 臂给每个命中块的
分数都一样，发给它 0,1,2,3 就是把字母序变成融合权重——实测后果是 `chk_zenith` 被 `apple`
挤到第二。一个臂没有意见，融合时就必须表现为没有意见。

**测试**。原来那条 `test_the_hybrid_and_dense_paths_agree_on_the_tie_break` 是 1/8 概率红；
新增的 `test_a_hybrid_query_returns_the_same_order_after_a_re_index` 是 **3/3 红**——差别
在于旧测试在同一个 collection 内重复查询，而引擎会稳定地重现它自己的任意选择，看不见
"重建索引后还一样吗"这个真正的性质。对照组 `..._dense_..._after_a_re_index` 当时就是绿的，
证明红的是 hybrid 不是 fixture。三处破坏验证逐个做过：并列改连续名次 → 3 条红；
砍掉 sparse 臂 → 对照组 `..._still_moves_the_fusion` 红；`RRF_K` 改 60 → 量纲那条红。

**门禁**（本机，真实 PostgreSQL 5433 + Qdrant 6333）：

```text
pytest（--ignore=tests/e2e）   2375 passed / 11 skipped
tie-break 用例连跑 30 轮        30 绿 0 红（改动前 8 轮里 1 红）
ruff check / format             passed
pyright                         0 errors / 0 warnings
```

**仍未做，且不能写成已做**：38 题 gold set 的评测**没有重跑**。MRR 0.960 / recall@1 0.947
是服务端融合下测的，改动后必须重测。要说清楚的是那组数字本来就是一个不可复现的检索器的
一次抽样。**`rag.llama_index.enabled` 保持 `false`**——ADR-017 第 3 步的堵点（噪声底比两条
路径的差异还宽）确实解除了，但等价评测没跑，没有证据的开关不翻。

## 2026-08-09 两处"装齐了但没接上"的能力（PR #87）

同一类缺陷，分成两个提交是因为它们可以分别 review、分别回滚。两处都是：组合根把能力
装齐了，配置、信封、scope、Gateway 全都对，而**真正跑的那条分支没接上**。

**外部研究节点从不调用模型（[ADR-032](./adr/0032-the-external-researcher-is-an-agent.md)）。**
ADR-027 §3.3 把动态 MCP 源给了 `researcher_external`，但 `build_task_v1_handlers` 里这个
节点在 `research is not None`（真 Worker 恒真）时直接调 `research.external.gather(...)`，
那是一次参数写死的 `external_search`；只有 demo 分支才会用到 profile 的动态工具。于是
`dynamic_tool_sources={"research"}` 在生产路径上是死代码。本机实测的形状是：信封里有
`mcp_web_fetch_page`，principal 带 `mcp:web`，Worker 也 discover 到了两个远端工具，而事件流
里 `research_external` 一条 `RunStarted` 都没有。

修法是**纯加法**：保留原来那次确定性搜索，仅当这个 Worker 真注册了 research 受众的工具时，
再跑一次带这些工具的 agent run，两半的 `evidence_refs` 用图自己的 fan-in reducer 合并。
目录为空的部署一步不多走、一分钱不多花、事件流一个字不变。没有把 `external_search` 一起
交给模型——它今天的确定性形态就是 evals 测的形状，为这件事改它是在没有需求的情况下动被测
基线（ADR-032 §2）。产出必须是证据包而不是散文：`synthesize` 把 `evidence_refs` 当
`EvidenceBundle` 读，所以 prompt 变成和 planner/critic 同形状的 JSON 契约；`{"items":[]}`
是允许的答案，解析不了则让节点失败，因为把"读不出"降级成"没读到"，下一个节点会在沉默上
写出一份有模有样的报告。

**`synthesize` 从未进入工作区会话。** ADR-028 的工作区在生产路径上 100% 不可用：
`synthesize` 广告了 `workspace_list / read / write`，模型也确实调，但每一次都是
`WorkspaceUnavailableError`，而 run 报告成功。原因是进入会话的 `_workspace_for(...)` 只被
`artifact_handler` 用了，真 Worker 的 `synthesize` 另建 `synthesis_node` 直接 `run(...)`，
没有包在会话里，于是 `WorkspaceScope.current()` 是 None——"没有节点进入过的工作区不该被凭空
创建"这条规则是对的，错的是没人进入。同一分支也因此从不回传 `workspace_version`，一个
attempt 写进去的东西下一个 attempt 看不见。

**为什么测试没挡住**，ADR-032 §1 单独记了一笔：
`test_each_server_reaches_the_agent_its_audience_names` 断言的是
`profile_with_dynamic_tools(...)` 的返回值——那证明了"目录会被交给这个 profile"，没有证明
"图里那个节点会用这个 profile 跑起来"。一个只测装配、不测调用的断言，正是这类缺陷的藏身处。
新测试因此都走真的执行路径：工作区那条断言的是真的 `WorkspaceWriteTool` 而不是 scope 变量，
因为线上撞到的是工具，不是变量。

**真实验收**（`task_d3dc69b3…`，DeepSeek + 本机 PostgreSQL/Qdrant，由该增量作者执行）：
`research_external` 的 `RunStarted` 带两个工具名，同一 run 里 `mcp_web_fetch_page` 走完
`ToolProposed → PermissionResolved → ToolStarted → ToolCompleted`，读到的正文成为一条
`source="external"` 且 URL 就是被读那页的证据，writer 的报告从它写出来；同一个 Task 里
`synthesize` 的 `workspace_list` 是 `ToolCompleted`。`docs/web-mcp-local.md` §5 的第 1、2 条
自此是事件流里的事实；**第 3 条（download 落库）仍未真跑**。同一次验收里 `workspace_write`
仍被拒，但那是 `missing_permission_scope`——提交的 principal 没有 `workspace:write`，与这两个
提交无关。

一条实测出来的成本事实：**默认 token 上限装不下会读网页的节点。** 一页正文 20–50 KB，两次读
约 28000 tokens，而 `multi_agent.max_tokens_per_agent_invocation` 默认 16000 会让 run 停在
半句 JSON 上（`budget_exceeded: token_budget`）——工具全部成功、节点仍然失败。默认值不动，
只有 `config/config.web-local.toml` 提到 120000。这不是新约束，而是 ADR-030 那条约束第一次
遇到会读东西的节点。

## 2026-08-09 WP15 阶段三：只读取用外部世界（WP14-02，已完成）

[ADR-027](./adr/0027-read-outward-write-inward.md) 的四个 PR 全部合入，逐项计划见
[read-outward-plan.md](./archive/read-outward-plan.md)。配置 schema `1.9 → 1.10`。

**PR-1 SSRF 从字面地址换成解析后校验（#83）。** 计划里明说这条必须最先做且独立成 PR：
后面两个 PR 让模型能自己命名 URL，那一刻起这个缺口从"要先污染搜索引擎索引"变成"往网页里
写一句话"——模型的输入里有检索到的网页文本，那是不受信任的内容。新增
`adapters/research/address_guard.py`，规则是**默认拒绝**而不是黑名单：只有全局可路由的地址
放行，回环、链路本地、私有、唯一本地、CGNAT、benchmarking、文档、未指定、组播全部落在补集
里。黑名单是要有人记得去扩的列表，`is_global` 是上游已经在维护的那个的补集。

两处 `is_global` 单独不够，各配一条测试：**组播的 `is_global` 是 True**，`224.0.0.1` 和
`ff02::1` 都会被"只看 is_global"的实现放行；IPv4-mapped、6to4、Teredo 里嵌的 v4 单独再判
一次。**一个主机名按它最差的那个地址判**——这里不决定客户端会连哪个，所以一条私有答案不会
被同组的公网答案洗白。**重定向改成在适配器里跟，逐跳过闸**：原来是 `follow_redirects=True`，
等于把目的地的选择权交给客户端，一个公网 URL 回 `302 Location: http://169.254.169.254/`
就是同一个 SSRF 多走一步。

没关上的部分写在明处：这里解析一次、HTTP 客户端连接时还会再解析一次，所以
**DNS rebinding 不在本次防护范围内**；关掉它要改传输层（连已校验的地址 + Host 头）。
ADR-027 §3.2 针对的"模型被诱导去命名内网 URL"是完全覆盖的，一个主动配合攻击者的解析器不是。

**PR-2 只读取用的 MCP server（#84）。** 新增 `apps/web_mcp/`，形状照抄 `apps/word_mcp/`：
无路径、无租户、无所有者字段，不认识工作区。**两个工具而不是一个带模式的工具**——PDF 走
HTML 抽取会变成一团乱码文本，而那读起来像一次成功的读取，所以 `fetch_page` 遇到非文本
content-type 直接按名字拒绝并点名 `download_document`。`download_document` 的 8 MiB 上限压在
`policy.max_tool_result_bytes` 默认的 10 MiB 之下，这样拒绝发生在这里、带着说明限额的理由，
而不是等整个文件过完线之后在适配器边界变成一句 "result too large"。`open_world_hint` 是
**True**——一个 URL 会答什么不是本进程能预言的，写 False 是好看而不是诚实。逐跳过闸的循环
抽成 `adapters/research/guarded_fetch.py`，第二个调用方到场是抽它的理由。

**PR-3 哪个 server 的工具进哪个 Agent 由配置声明（#85）。** `[[mcp.servers]]` 新增
`audience`（`research` / `synthesis`）。改的是"按什么分配"而不是"多给一个 profile"：让读网页
属于 `researcher_external` 而渲染文档属于 `writer` 的，是这个工具用来干什么，不是它走 MCP。
沙箱在这次 rebase 里**合并进同一个形状**而不是并排放着——它就是第三种 audience，两处并行的
形状换成一处。默认值 `synthesis` 是**防回归**：这个字段出现之前写的每一份配置都意味着
synthesis，默认成 research 会在升级时把 Word 渲染器从 writer 身上悄悄挪走。
**audience 不改变授权信封**——信封仍列出全部已配置的名字（信封是 Task 的上限，audience 是哪个
Agent 够得到它），有一条测试钉住两种 audience 投影出的信封逐字节相同。**profile 按注册表
加宽，不按配置**：启动时连不上的 server 什么都不贡献，按配置加宽会让节点去请求一个
ToolGateway 解析不到的工具，那是"节点必崩"而不是"少一个能力"。

**PR-4 本地 profile 与真实验收（#86）。** 新增 `config/config.web-local.toml`、
[本地 Web MCP 指南](./web-mcp-local.md)、`scripts/dev.sh` 的 `web-server / web-check /
web-api / web-worker`。探测脚本从复制改成泛化（`smoke_word_mcp.py` → `smoke_mcp_server.py`
加 `--label`）。web 与 word 是两份 profile 文件而不是一份：每一份都把自己的工具名冻进每个
新提交 Task 的授权信封，合成一份等于把每个 Task 同时按两者加宽。

手册 §5 四条真实验收里，**第 4 条（拿掉 `mcp:web` 后同一工具在 Gateway 被拒）完全由策略引擎
决定，因此写成了 `tests/adapters/test_mcp_scope_refusal.py`** 而不是留成一条没人会重复的手工
步骤。它走真的 `ToolGateway` 而不是直接调策略引擎——手册说的是"Gateway 拒绝"，而 Gateway 才是
Task 真正撞上的东西；只调引擎只能证明规则存在，证明不了有路由到它。同时断言两件事：面向模型
的错误码只说 `policy_denied`，而**是哪个 scope 缺了留在事件流的 `PermissionResolved` 里**——
运维诊断不了的拒绝是一张工单，而向模型报出内部 scope 名是给操纵它的人提示。

**本阶段明确不做**：填表、点击、任何 POST；驱动桌面软件界面；JS 渲染页面与截图（需要浏览器
内核，ADR-027 §3.5）——**SPA 页面取不到正文是已知边界不是 bug**；放宽 `retryable_effects`。

### 2026-08-09 全仓门禁复核（`main@a4dea2b`）

本节数字是在 `main@a4dea2b`（PR #87 合并后）重新实测的，不是各增量当时的门禁值：

```text
pytest（无外部服务，--ignore=tests/e2e）  1784 passed /  597 skipped
tests/e2e                                    3 passed /   11 skipped
tests/architecture + tests/config           114 passed
ruff check                                 passed
ruff format --check                        passed（421 files）
pyright                                    0 errors / 0 warnings
```

**上面这一组是在本机、没有外部服务时跑的**，因此 597 与 11 项环境跳过只能报告为跳过。
含 `tests/e2e` 的全仓为 1787 passed / 608 skipped，与 PR #87 提交信息里的数字一致。

**但"没有真实服务的证据"这个说法此前一直低报了自己。** CI 有一个独立 job
（`Migrations, PostgreSQL and Qdrant-backed stores`）在**每个 PR 上**对着真实
PostgreSQL 16 与 Qdrant 跑 `tests/contracts tests/persistence tests/api tests/vector`，
并先 `alembic upgrade head`。本文档此前只写"环境跳过"，读起来像这些不变量从未在 CI 里被
执行过，而它们一直在被执行：

```text
CI service-backed job（真实 PostgreSQL + Qdrant）   920 项，其中 2 项环境跳过
```

2 项跳过：一项是只在非锁定读路径上成立的 PostgreSQL 契约，一项需要 `embedding` extra 与
本地 BGE 权重——CI 不装该 extra，这是它与本机全量的唯一差别。

**这个 job 不是每次都全绿，而原因不是基础设施抖动。** 写这份文档的 PR 上，同一个 job 连跑
两次得到 `918 passed / 2 skipped`（[run 31351527183](https://github.com/he-zi-qiang/agent-workbench/actions/runs/31351527183)）
和 `1 failed / 917 passed / 2 skipped`（[run 31351722239](https://github.com/he-zi-qiang/agent-workbench/actions/runs/31351722239)），
两次之间只差文档改动。失败的那条是
`tests/vector/test_tied_score_order.py::test_the_hybrid_and_dense_paths_agree_on_the_tie_break`，
断言 dense 与 hybrid 两条路径对并列项给出同一个次序——**它偶发失败正是本文件多处记录的
"并列检索分数没有确定性次序"这个已知缺口的直接表现**，不是需要 quarantine 的坏测试。
把它写成"918 passed"会让这个 job 看起来比实际稳定，也会掩盖一条真实缺陷；修法是在适配器
边界给并列项定序（独立变更），在那之前这里如实记录它会红。

这一组**不覆盖** `tests/e2e`、Task Worker 端到端与需要模型 Provider 的路径，所以它不能替代
上一节那次真实 Task 验收，两者也不能相加。三组数字来自三种环境，只能分别引用。

## 2026-08-09 WP15 阶段一收尾与阶段二（一次性沙箱）

阶段一（ADR-028 任务工作区）此前停在组合根前面一步：`agent_profiles` 与授权信封都点名了
`workspace_list/read/write`，而 `apps/task_worker/composition.py` 的注册表里没有它们。这
不是少一个能力——`ToolGateway.advertise` 对解析不到的工具抛 `UnknownToolError`，所以当时
一个非 demo 的 Task Worker 跑到 `synthesize` 必崩。已修复，并补上一条按 profile 推导的
测试：每个 v1 profile 在默认信封下会请求的工具，必须都在组合根实际注册的集合里。

阶段二（ADR-029）的沙箱是纯函数：一次调用一个容器，文件进、文件出，无网络、只读根、非
root、丢弃 capability、tmpfs 可写层、内存/CPU/进程数/墙钟上限。这些是
`apps/sandbox_mcp/executor.py` 里的常量，配置够不到——ADR-029 §3.2 的立场是断网是整条
重放保证成立的前提。ADR 明说验收不能是"我们设了 flag"，所以隔离全部对着真容器验，每条
配对照组：联网失败/纯计算成功、写只读根失败/写自己那层成功、死循环被杀/快脚本正常返回、
uid 65534、看不到主机路径、两次调用之间无残留、fork 撞上 pids 上限。

Task 侧是原生工具 `sandbox_run`（不是通用 MCP 绑定）：它要读写工作区与 Task 状态，那是
Worker 的内部权限，而 ADR-026 的规矩是 MCP server 不接收路径与所有者。模型给的是工作区
文件名，拿回的是新工作区版本；base64 不进模型上下文。

`[sandbox]` 默认关闭，schema 升到 `1.9`。开启它同时做两件事：`sandbox_run` 进授权信封，
且信封风险上限抬到 `external`。Worker 启动时探测一次，而且是真发一次 `run_python`——能连
上的 socket 既不证明有容器运行时也不证明容器能起。探测失败则记结构化日志、不注册这个
工具、进程照常启动（§3.6）；此时信封里仍有这个名字（它是提交时冻结的），所以 agent
profile 是按**实际注册到的工具**加宽的，不是按配置。

两个部署前提，缺了不会让 Worker 起不来：`docker pull python:3.12-slim` 与
`agent-sandbox-mcp`。可直接用的本地 profile 是 `config/config.sandbox-local.toml`。

**本轮明确未做**：阶段三（只读取用网页与下载）、阶段四（成本/时限预算、`workspace_edit`、
`workspace_grep`）、阶段五（第二张图 `v2_general`）。沙箱内联网、跨调用状态、GPU、字节级
确定性重放均不在范围内（ADR-029 §4）。

一条被 e2e 抓住的真 bug：工具在没有输入文件时也发 `inputs: []`，而 server schema 里
`inputs` 可选且 `minItems: 1`——一个只做计算的脚本会在跑起来之前被拒。stub client 因此改成
按 server 的真实 schema 校验参数，接受任何东西的 stub 看不见这类错误。

门禁：无外部服务全仓 `1679 passed / 597 skipped`（其中 13 条跑真容器），
`tests/e2e` `3 passed / 11 skipped`。无 docker 或本地没拉 `python:3.12-slim` 时带修复指引
跳过。Ruff、format、Pyright 全绿。

## 2026-08-09 项目自有 Word MCP（本地 Optional Lab）

Word 生成不依赖 Codex 的 Documents skill，也不驱动桌面版 Microsoft Word。项目自有的
loopback MCP Server 只广告 `render_document`；Task Worker 将它冻结为
`mcp_word_render_document`，并继续经过现有 Tool Gateway、Task authorization envelope、
`mcp:word` principal scope、统一工具事件与 tenant/owner 受限的 ArtifactStore。

Word 能力使用独立的 `config.word-local.toml` 显式 opt-in。常规 `config.local.toml` 保持
MCP 关闭，避免把 Word 工具放进每个新 Task 的权限上限；Word profile 还将开场工具调用设为
optional，避免没有 `mcp:word` 的普通任务被强迫提议 Word。`scripts/dev.sh word-worker`
在没有模型 Provider key 时直接拒绝启动，不用 demo graph 冒充真实闭环。

本地操作与验收见[本地 Word MCP 指南](./word-mcp-local.md)，完整决策见
[ADR-026](./adr/0026-word-docx-is-an-mcp-artifact.md)。协议健康、目录发现、配置投影和真实
Task/Artifact 的测试证据在 Word Server 合并后记入本节；在此之前不得把单独的
`/health + tools/list` 写成完整 Task E2E。

## 2026-08-09 MCP Adapter（WP14-01，已完成）

MCP 不作为第二套 Agent 框架：官方 Python SDK v2 只存在于 `adapters/mcp/`，远端工具先
冻结成项目自己的 `ToolBinding`，再经过既有 Runtime、Tool Gateway、Task 信封、principal
scope、安全重放边界与事件流。完整决策见 [ADR-025](./adr/0025-mcp-adapter.md)，逐项验收见
[MCP Adapter 实施计划](./archive/mcp-adapter-plan.md)。

本轮已单独跑通一条不依赖数据库或模型供应商的协议—Runtime 集成 E2E：官方 SDK 内存 server
完成 `server/discover` / `tools/list` / `tools/call`，显式 allowlist 解析为
`mcp_office_render_document`，只有 writer 的
Task 请求能看到它；调用经过 Gateway 授权后按
`ToolProposed → PermissionResolved → ToolStarted → ToolCompleted` 落入真实事件流。
长文本结果同时落入 tenant/owner 受限的 ArtifactStore 并由 ToolResult 引用。该测试不经过
PostgreSQL Task Registry、claim 或 checkpoint，不能引用成完整持久化 Task E2E。
同一轮还补齐了真实 Provider 的收敛语义：`tool_calling_required=true` 只强制开场轮；
ToolResult 后保留工具目录并回到 auto，DeepSeek HTTP Adapter 的两轮契约测试证明第二轮可
直接完成，而不是被迫重复调用到预算耗尽。

启动增量 benchmark 由 `scripts/benchmark_mcp_startup.py` 生成，不手写统计：

| 环境 | SDK | 工具数 | 预热/样本 | median | p95 | min–max |
|---|---:|---:|---:|---:|---:|---:|
| macOS 26.5.2 arm64，Python 3.12.13，基于 `8595b1f` 的工作树 | MCP 2.0.0 | 20 | 3 / 30 | 0.707 ms | 0.970 ms | 0.662–1.073 ms |

这个数字只覆盖 SDK `server/discover`、`tools/list` 与本地 binding 构造，使用官方内存 transport，
**不包含网络延迟**；它是本项目适配层的下界，不是生产 endpoint 的延迟承诺。
本次命令的原始机器可读输出已保存为
[`mcp-startup-benchmark-2026-08-09.json`](./mcp-startup-benchmark-2026-08-09.json)。

当前证据：

```text
MCP Adapter + 协议—Runtime E2E          37 passed
无外部服务全仓                         1540 passed / 597 skipped
tests/e2e                              1 passed / 11 skipped
architecture + config                 86 passed
Ruff format / lint                     passed
Pyright                                0 errors / 0 warnings
uv lock --check                        passed
dependency license allowlist           passed
```

597 个无服务跳过项与 11 个持久化 E2E 跳过项需要 PostgreSQL、Qdrant 或本地 BGE 权重；
当前 Compose 未运行，本轮没有把这些描述成通过。唯一常规 warning 来自 LangGraph
checkpoint serializer 的待弃用默认值，与 MCP 行为无关。

## 2026-08-03 LlamaIndex 检索适配器落地，但**没有**切流量（ADR-017 步骤 1 完成，步骤 2 未通过，未合并）

**先说结论，因为它和这一刀原本的意图相反。** 适配器建好了、契约测试跑通了、
`rag.llama_index.enabled` 第一次有了消费者——然后它被设成 **`false`**。ADR-017 第 3 条
规定"默认流量切到 LlamaIndex"要以第 2 条的等价评测为前提，而那次评测的结论是
**测不出来**：不是两条路径不一致，是这套测量装置分辨不了它们。详见本节最后一段。

`rag.llama_index.enabled` 从落地那天起就是 `true`，而 `src/` 里读它的只有解析它的
settings 类本身——**零消费者**。这和 PR #71 修掉的 `multi_agent` 预算字段是同一类缺陷
（"配置描述了一个不存在的系统"），只是这一条断言的正是简历依赖的那句话：本项目用
LlamaIndex 做 ingestion/retrieval。把开关关掉不会改变任何行为，因为没有第二条路径。

**接缝在"谁提出候选"这一步。** 新端口 `CandidateRetrieverPort` 只负责一件事：给一个
问题提出一批候选块。授权、重排、截断到 top_k、组装引用全部留在 `RetrievalService`
里——ADR-017 允许框架拥有第一步，不允许它拥有之后任何一步，因为之后每一步都是文本抵达
读者的方式。`RetrievalService` 因此不再持有 embedder / index / sparse_encoder，只持有
这个端口。

两个实现，**不是平级的**：

| 实现 | 角色 |
|---|---|
| `ReferenceVectorIndexRetriever` | ADR-017 之前的路径，原样搬到端口后面，命名上就写明是迁移基准 |
| `LlamaIndexCandidateRetriever` | `VectorStoreIndex.from_vector_store(...).as_retriever()`，LlamaIndex 负责 query embedding、检索器契约与 Node 映射 |

**Qdrant 仍是唯一融合方。** 走的是 LlamaIndex 自己的扩展点——一个实现
`BasePydanticVectorStore` 的适配器，把查询交给本项目的 `VectorIndexPort`。没有用
`llama-index-vector-stores-qdrant`：它会对"点在集合里怎么排布"和"融合在哪里发生"给出
第二种意见，而这两件事 ADR-017 分别判给本项目和 Qdrant。一次 hybrid 查询因此只产生
**一次** `search_hybrid` 调用，两路 prefetch 与那一次 RRF 都在数据库里；本进程从不同时
持有两个有序列表，这是"绝不融合第二次"唯一能被确证的方式。

**认不出来的过滤条件是拒绝，不是忽略。** tenant / knowledge base / principal 是以
LlamaIndex 的 `MetadataFilters` 形式到达适配器的，而它们是"限定在一个客户"和"查全部"
的区别。丢掉一个没人看懂的过滤条件不会报错，它只会**返回更多行**，而后面的 ACL 复核
仍然会放行调用方自己有权读的那些——于是每一条既有断言都保持绿色，查询却已经跨过了租户
边界。所以翻译层对以下每一种都失败关闭：没有过滤条件、OR/NOT 组合、嵌套分组、不认识的
键、非等值算子、同一个键出现两次、缺任何一个必需键。

**Node 往返不许发明事实。** `page` 为空的块回来必须仍然为空——没有分页的格式没有第 1
页，写一个进去等于把读者送到本系统编造的位置；`source_revision` 必须仍是那个整数，它要
和 PostgreSQL 的行做相等比较，变成字符串不会报错，只会让每个候选都被判成陈旧，然后检索
看起来像"什么都没找到"。缺字段一律拒绝构块，而不是补默认值。

**三个 `Literal[False]` 字段没有进投影，理由写在投影层。** `agent_executor_enabled`、
`query_engine_generates_final_answer`、`fusion_enabled` 都是单值类型，进程侧检查只能拿
常量和自己比——读起来像执行，实际上不可能失败。真正约束它们的是结构：架构守卫拒绝任何
模块 import LlamaIndex 的 agent / chat engine / query engine / response synthesizer，
**并且**拒绝 `.as_query_engine()` 这类不需要新 import 就能召唤的调用；守卫另配一条反向
断言，要求 `as_retriever` 确实还在——两条只会说"不"的规则，在适配器被整个删掉的仓库里
同样成立。

**契约测试是同一套断言跑两遍。** `tests/vector/test_authorized_retrieval.py` 按
`CandidateRetrieverPort` 参数化：ACL 网关、source revision 栅栏、查询后撤销授权的那道
屏障、发布前的第二次复核、引用与块一一对应，两条路径各跑一遍，都用真实 Qdrant 与真实
PostgreSQL。断言 tool schema 与风险等级的那几条**故意不参数化**——它们不依赖哪个检索器
跑过，标上去会夸大覆盖范围。

**破坏验证：15 处，第一轮 14 处被抓住。** 漏网的那一处值得单独记，因为它抓住的是一条
**名不副实的测试**：删掉"过滤条件必须用 AND 组合"的检查后，没有任何测试变红。原因不是
没测，而是那条用例用了两个 `tenant_id` 做 OR——删掉 condition 检查之后它**仍然**被拒绝
了，被**重复键**那道守卫拒绝的。测试通过的理由和它的名字无关。改成三个必需键各出现一
次、只有 condition 是 OR（这才是真正危险的形状：它匹配该知识库的**或**该 principal 可读
的每一篇文档，跨租户），删掉检查后变红；重复键那条另立为独立用例。补完后 15/15。

**依赖的代价，如实记。** `llama-index-core` 带进来约 50 个传递依赖（nltk、tiktoken、
pillow、networkx 在内），装在每一个安装本项目的进程里，包括从不检索的那些。它是主依赖
而不是 extra，理由和 langgraph 一样：藏在 extra 后面只会换来一个跳过适配器测试、因而对
它什么都证明不了的 CI。许可证门禁有 5 个包不匹配现有拼写，**逐包实测而不是估计**：
`ISC`（griffe / griffecli / griffelib）与 `MIT-CMU`（pillow）是既有许可类别的另一种拼
法，按本仓"每种拼法各列一行"的做法加进 allowlist；`tiktoken` 的 License 元数据字段不是
标识符而是**整段 MIT 许可证正文**，无从列出拼法，因此按 FlagEmbedding 的先例点名豁免并
写下理由——绝不加 `UNKNOWN`。

### 步骤 2：等价评测没有通过，因为它测不出来

同一个索引、同一份 38 题 gold set（digest `a26070043b0ffde1`）、同样的 top_k 与候选
预算，两条路径各测一遍。**dense 臂两条路径逐位相同**，并且与 2026-07-28 的历史 dense
记录一致。hybrid 臂不同：

| hybrid | reference | llama_index | 2026-07-28 记录 |
|---|---|---|---|
| recall@1 | 1.0000 | 0.9737 | 1.0000 |
| recall@3 | 1.0000 | 1.0000 | 1.0000 |
| MRR | 1.0000 | 0.9868 | 1.0000 |

**但这个差不属于适配器。** 把两个检索器各跑两遍之后：

```text
reference 与自己不一致        9/38 题
llama_index 与自己不一致     10/38 题
两条路径之间 chunk 次序不同   6/38 题
两条路径之间 top-3 文档不同   3/38 题
```

**每个检索器和自己的不一致，都多于两者之间的不一致。** 机制是分数并列：两条路径发给
Qdrant 的 dense 向量、sparse 权重与三个 limit 全部逐位相同，拿回来的分数向量也逐位相同
（`[1.0, 0.476191, 0.476191, 0.35, 0.35, ...]`），**只有并列项之间的次序在变**。10 篇
文档的语料上 RRF 频繁并列，而并列名次没有定义好的次序。

所以这次评测的噪声底比它要测的效应更宽，等价与否都无法断言。能断言的只有一条：**没有
任何证据表明 LlamaIndex 路径更差**，也**没有任何证据表明它等价**。据此把
`enabled` 设为 `false`——"测不出来"不是切流量的理由。

**这不只是评测的问题。** `RetrievalService` 在重排之后才截断到 top_k，所以并列次序不稳
意味着**同一个问题两次提问可能得到不同的上下文与不同的引用**。基线 §15 要求"固定数据集
和可展示指标，而不只是一次成功截图"，一个第二次运行会换答案的演示不满足这一条。同时它
也给 `ABLATION.md` 里"两轮完全相同"那句话加了限定：当时相同的是聚合指标，底层次序并不
稳定。

### 并列分数定序（同分支后续增量）

`QdrantVectorIndex` 的两条搜索路径现在按 `(-score, chunk_id)` 返回结果。分数仍然主导，
定序只发生在**分数相等**的候选之间，不可能把低分候选提到高分之上。

`chunk_id` 做次键是因为它由 chunk 推导而来，**重建索引后仍然相同**。按引擎自己的返回
次序或按写入次序定序，能让"重复查询次序一致"通过，却会在下一次重建索引后再次漂移——把
不确定性挪到更难看见的地方。

并列是**构造出来的**，不是碰运气碰到的：五个点共用同一个向量，因此每一次运行都必然同
分。三处破坏全部被抓住：

| 破坏 | 失败测试 |
|---|---|
| 完全去掉定序（修复前的行为） | 2 条，含 `test_a_repeated_hybrid_query_returns_the_same_order` |
| 次键改成引擎自己的次序 | 2 条 |
| id 从次键变成主键 | 3 条 |

第一行是这次诊断从"观察到一次"变成"可重复实验"的地方：去掉定序，重复同一个 hybrid 查询
就会给出不同排列。

**一条如实记下的、目前不可证伪的测试**：`test_a_repeated_dense_query_returns_the_same_order`
在去掉定序后仍然是绿的——这个 fixture 上 dense 臂本来就稳定，变红的是 hybrid 臂。这与
评测结果一致（dense 两轮逐位相同，hybrid 不同），所以它作为回归守卫保留，但**不计入本次
修复的证据**。

**仍未做**：等价评测没有在定序之后重跑，因此 `rag.llama_index.enabled` 保持 `false`。
装置现在具备分辨能力了，但"具备分辨能力"不是"已经分辨过"。

### 评测脚本自身的两处修正，都是本轮引入的

**一、预热顺序污染了延迟。** 第一次跑把 embedder、sparse encoder、reranker 三套
BGE-M3 量级权重同时驻留（约 6.6 GB），机器进 swap 9.9 GB/11.3 GB，Qdrant 自己的计数器
显示 hybrid 臂**每 50 秒才完成一次查询**，而同一个脚本 2026-07-28 记录的中位数是
7.2 s/题。reranker 现在按需加载且该臂改为显式开启（`AGENT_WORKBENCH_EVAL_RERANK=1`）:
hybrid 在当前 gold set 上已打满 1.000，重排一个完美排序的 delta 必然为 0，跑它只是花一
小时确认算术。**先跑的那条路径付掉全部换页成本**，因此第三轮里 reference 的
36 724 ms 与 llama_index 的 159 ms 中位数**不是性能差异，是测量顺序的产物**，不可引用。

**二、候选深度被端口收窄了。** 旧脚本向 Qdrant 要 `limit=TOP_K` 但让两臂各
prefetch `CANDIDATES`；`CandidateRetrieverPort` 只有一个 `limit`，于是 prefetch 塌成
了 3，RRF 开始在两份**已经截断**的列表之间做选择——旧脚本里正好有一段注释记着它曾经犯
过同一个错。表现是 hybrid 在**两条路径上同时**从 1.000 掉到 0.969，这也正是它能被认出
是装置缺陷而不是适配器回归的原因。改为每个臂都要 `CANDIDATES`、之后再截到 `TOP_K`，
reference 随即复现了历史的 1.000。

按基线/计划对照的第二个缺口。WP11 的现状是"部分"，但差距比这两个字大：图从写下来那天
起就有六个调模型的节点，而每一个都自己拼请求——system prompt 来自一个模块的私有字典，
消息来自另一个模块的私有函数，evidence 段由第三个模块在请求建好之后追加。**没有任何
地方给这些 Agent 命名，也没有任何地方声明它们各自能读什么。** 于是"Agent 上下文互相
隔离"是三段代码碰巧这么写的产物——这种性质会一直成立，直到有人加一个节点。

**Profile 把它变成声明。** `workflows/agent_profiles.py` 里六个 profile
（framer / planner / researcher_internal / researcher_external / writer / critic）
各自声明身份、prompt、可用工具，以及**准许读入的输入**。给一个 profile 送它没有声明
的输入会被拒绝而不是静默塞进 prompt，因此隔离在每次 Agent 运行都要经过的那一个点上被
强制执行：

* 两个 researcher 都不准 `evidence`——它们**产出**证据；能读到对方结果的 researcher
  会把并行扇出变成"带额外步骤的串行"，也会让扇入 reducer 承诺互相独立的两个结果变成
  相关的；
* critic 准 draft，不准 evidence。能读证据的 critic 是在评审研究，而研究是 writer 的
  输入、且已经发生过了；
* 没有 profile 准读更早节点的产出。那些在 artifact store 里，抄进 prompt 会让上下文
  随图增长而不是随问题增长。

**权限只会收窄。** profile 的工具清单是它自己的上限，与 Task 提交时的授权信封取交集；
`permitted_tools` 里没有任何一条路径能返回信封未授权的工具——"子 Agent 权限大于父
Task"因此是写不出来的，而不是靠评审拦住的。v1 六个 Agent 的工具清单都是空的：外部效果
走专用端口与节点，把工具塞进研究 Agent 的模型循环，正是 gateway、账本和审批节点存在
的理由所要挡住的事。

**三个配置字段第一次有了消费者。** `multi_agent` 的四个预算字段此前在 `src/` 里
**零消费者**，配置描述了一个不存在的系统：

| 字段 | 之前 | 现在 |
|---|---|---|
| `static_agent_node_limit` | 无人读取 | 装配时断言 profile 数不超过它，超了进程不启动 |
| `max_parallel_agent_invocations` | 无人读取 | `BoundedParallelExecutor` 的信号量真正限流 |
| `max_tokens_per_agent_invocation` | 无人读取 | 进入每次调用的 `RunBudget.max_total_tokens` |
| `max_agent_invocation_attempts_per_task` | 无人读取 | **仍然无人读取**，见下 |

第二条以前是"描述"而不是"上限"：图扇出两个 researcher，LangGraph 并发跑它们，配置里的
数字碰巧等于图当时的行为——加第三条分支会让真实并行度上去而设置读起来一模一样。测试用
限流 1 和限流 2 各跑一次并测峰值重叠，因此测的是上限，不是"执行器本来就串行"。

```text
ruff / pyright                       全过
pytest（真实 PostgreSQL + Qdrant）   1845 passed / 11 skipped
```

四条新性质都做了破坏验证：删掉 evidence 准入检查、把交集换成 profile 自己的清单、去掉
信号量，对应测试分别变红。

**这一刀没做的（WP11 剩余）：**`max_agent_invocation_attempts_per_task` 要跨 retry 与
reclaim 计数，需要持久的 per-Task 计数器而不是传进进程的一个数字——把它投影进配置只会
让它离"看起来被执行了"更近一步，所以它留在 settings 里不投影。同样留给下一刀的还有
partial failure 的显式表达、父 Task → 子调用的取消传播。能力表那一行仍是"部分"。

## 2026-08-03 证据包与它暴露出来的收集端（未合并）

按基线/计划对照出的四个缺口，从投入产出最高的一个开始：计划 §11 从写下那天起
就要求 `artifacts/evidence/<gate>/manifest.json`，而仓库从未产出过一份。替代它的
是散文——状态文档里的测试计数、README 里的评测数字、"demo 能跑"这句话。每一条都
是真的，每一条都不可核对：读者无法判断某个数字出自哪个 commit，三周后写它的人
同样判断不了。

**`agent-evidence`（`bootstrap/evidence.py`）分开记录两类东西。** 派生事实来自配置
与仓库——startup config revision、run semantics 模板修订、policy 标签与规范指纹、
graph version、模型/嵌入/重排身份、Qdrant 索引版本、commit；没有一项是手填的，
所以没有一项能是一厢情愿。附件是别人产出的文件，按 SHA-256 与字节数记录，`verify`
据此复核——只记一次、从不重算的哈希，只能证明它当时是对的。

两条规则都是拒绝：附件不存在或为空则不写清单（记下路径让读者自己发现，正是清单
开始断言无人产出的证据的方式）；工作树脏则不写，除非显式 `--allow-dirty` 并把
`git_dirty` 记成真——清单要指名一个 commit，若磁盘内容与之不同，那个 commit 只是
装饰。没有附上的东西由已附上的**反推**进 `missing`：关卡允许不完整，不允许对此
沉默。

**收集端。** 做上面这件事时发现"OTel trace 样本"根本无从取得：
`observability.otel_enabled` 是 `Literal[True]`，适配器从落地那天起就在导出，而
compose 里从来没有 `otel-collector` 这个服务，配置默认值指向的是一个本拓扑未定义
的主机。端口也是错的——导出器是 OTLP over **HTTP**，POST 到 `<endpoint>/v1/traces`，
默认却写着 `4317`（gRPC 端口）。遥测按设计 fail-open，于是这一整条链路从未工作过，
也从未有人被告知。这正是 PR #67 想消灭的那种"开关打开了但什么也没打开"。

现在 collector 在默认栈里（不是 `demo` profile：API 无论有没有人启用合成 Worker
都在导出），没有任何服务 `depends_on` 它——collector 的问题绝不能变成运行的问题，
这是遥测工厂本来就守的规矩。它把收到的东西写进卷，`docker compose cp` 即可取出
成为附件。默认端口改为 `4318`，并由测试**分别读取**配置与 collector 配置来断言
两端一致；在两处各写死 4318 的测试，在两端指向不同端口时同样会通过。

```text
ruff format / lint                   全过
pyright                              0 errors
pytest（真实 PostgreSQL + Qdrant）   1833 passed / 11 skipped
真实 collector 链路                  适配器 → OTLP/HTTP:4318 → traces.jsonl 已验证
```

链路是拿本项目自己的遥测适配器打通的，不是拿 curl：`build_telemetry` 发一个 span，
容器里的 collector 落出真正的 OTLP JSON。端口回改 4317 后新测试变红，回改前后各跑
一次确认。

**没有打勾的那一项。** 计划 §12 的"首个 evidence manifest"仍未打勾：工具存在不等于
关卡已经通过。要打勾得跑一次真正的关卡并把测试报告、评测报告、trace 样本和演示
附上去。

## 2026-08-03 三处围栏被自己应该拦的东西满足（已合并 main，PR #68）

三个缺陷，其中两个是同一个缺陷换了件衣服：**围栏在拿它本该检查的东西当通行证**。
三处都不是"少写了一道校验"，而是校验读到的值来自被校验方。

**1. 已记录的 intent 仍在授权第二次外部副作用。** `record_intent` 找到什么行就返回
什么行，而 `intended` 行让 `may_dispatch` 为真。于是账本存在的理由本身仍会发生：W1
写下 intent，导出真的发出去了，W1 在报告结果前死掉；租约过期，W2 领走 Task，走到同
一个 `operation_key`——被告知"intended，继续"。基线第 9.3 节早就写明这个窗口的正确
状态是人工核对（`needs_reconciliation`），只是没有任何代码执行这次转换。现在
`record_intent` 在唯一可能的时刻做它：**epoch 比当前 attempt 更旧的 `intended` 行被
判给人，而不是判给下一个 Worker**。这是账本唯一一次未被要求就做的状态转换，方向只
可能是拒绝副作用。规则写在 port 上而不是留给实现自选：一个跳过它的账本能满足其他
每一条规则，同时把这个洞原样留着。

**2. 被顶替的 Worker 可以借用继任者的 epoch。** 每个节点都向 Registry 问 Task 当前
的 `lease_epoch`，然后用问到的值去写。租约在图运行途中失效的 Worker 因此读到的是顶
替它的那个 Worker 的 epoch，而每一次带围栏的写入都拿这个值和"当前租约"比对——当然相
等，因为它就是刚刚发下来的那个。账本存在的全部目的是拒绝被顶替的 Worker，重读则正
是被顶替的 Worker 满足它的方式。基线第 9.2 节写的是"匹配 `lease_owner` 与**领取时
的** `lease_epoch`"，此前只有 Registry 自己的状态转换在遵守，因为只有 Worker 手里有
`ExecutionLease`。现在 Worker 把它发布出来：`TaskExecutionScope` 把 claim 携带到节
点，围一次 graph invocation，返回即失效；invocation provider 读回来，并在 Registry
不同意（owner 不对、epoch 不对、已不是 running）时拒绝执行。身份仍然每次重新解析，
也必须如此——恢复后的图以 Task **现在**属于谁的身份运行；租约是相反性质的事实，是
关于**本进程**的断言，是节点唯一不许自己去取的东西。拒绝发生在节点而不只在账本：被
顶替的 Worker 若只在导出那一步被拦，它已经为一个不属于它的 Task 花掉了模型预算、写
了 artifact，而只守最后一次写入的围栏会放过它之前的一切。

**3. 引用可以指向没人给模型看过的段落。** `knowledge_search` 把搜索授权到的内容写进
journal，发布围栏与引用围栏都读这份 journal。它记录的是**整次检索**，渲染出去的却是
其中一个**子集**——结果预算会丢掉放不下的段落——所以自预算落地那天起，两者就不再是同
一份清单。而这正是引用围栏唯一的前提：`verify_citations` 只在模型**点名**且**被展示
过**时才给出引用；journal 里放着更宽的清单，一个模型产出而非读到的 chunk id 就能验
证通过，并带着本系统的权威回到提问者面前。现在 `_render` 把文本和它由哪些段落构成一
起返回，下游不必第二次推导预算就知道模型看见了什么；journal 两半都收窄到这份清单——
引用收窄，是为了让没展示过的 chunk 无法通过验证；authorized revisions 收窄，是因为发
布围栏问的是答案**建立在**什么之上，从未进入 prompt 的段落不在其中，围它反而会因为一
份本次运行从未泄露过的文档而拒掉本来正确的答案。

**一个测试在断言缺陷。** `test_the_export_node_passes_the_live_lease_epoch_to_the_ledger`
把 Registry 行设成 epoch 9、而 Worker 持有另一个 claim，然后**要求**导出以 9 发出。
它现在是两个测试——写入发生在 Worker 实际持有的 claim 之下，租约已经变更则拒绝——外加
一个"在完全没有 claim 的情况下到达节点"的用例。

每个新测试都做过破坏验证：逐个还原修复，确认测试变红，再恢复。账本的两个跑在真实
PostgreSQL 上。整图 handler 测试是"claim 能穿过 LangGraph"的证据——没有 context 传播
时，它的 export 节点会拒绝而不是导出。

```text
ruff format / lint                   全过
pyright                              0 errors
alembic 唯一 head                    0019_tool_executions
pytest（真实 PostgreSQL + Qdrant）   1821 passed / 11 skipped
```

11 项跳过需要 BGE 权重（`embedding` extra）。这组数字取自 `feat/react-chat-work-ui`
当前工作树，因此同时包含下一节的前端增量；PR #68 自身在 GitHub Actions 的三个
job（配置/架构/lint/类型/测试、迁移与 PostgreSQL/Qdrant 存储、secret scan）上全绿后
以 squash 合入 `main`。

**对前一节记录的修正：**2026-07-31 那节写"`RetrievalJournal` 按 run 记录**模型看到
的**全部证据"。那句话描述的是意图，不是当时的行为——直到这次修复它才为真。历史小节
按惯例保持原样，事实以本节为准。

## 2026-08-02 React Chat / Work 控制台

分支 `feat/react-chat-work-ui` 已把原来的单文件静态原型替换为 React 19、TypeScript、
Vite 和 pnpm 锁定构建。Chat 与 Work 是两条一级工作流；Knowledge、Approvals、
Evaluation、System 只承担证据与操作辅助，不伪造后端没有提供的产品状态。

前端直接复用现有 REST/SSE 契约，并守住以下发布边界：Chat 不展示
`ModelCompleted.text`，只发布 `AnswerCommitted`、`AnswerWithheld` 或已确认完成的同步
响应；Work 只在 `export_artifact` 调用、产物与后续 `TaskSucceeded` 严格关联后提供最终
报告。开发 Header 身份、会话本地列表、上传完成不等于已索引等限制均在界面中明确标注。

```text
ESLint / strict TypeScript              passed
Vitest                                  45 passed
Playwright（desktop / mobile）           2 passed
Vite production build                   passed
Docker web-build（锁文件冷安装）         passed
Compose runtime / ready / UI / asset     passed
桌面 / 390px 移动端浏览器检查            passed，无水平溢出或 JS console error
pytest（无外部服务）                     1264 passed / 568 skipped
ruff / pyright                          passed / 0 errors
```

跳过项需要真实 PostgreSQL、Qdrant 或 BGE 权重；不能把这组无外部服务结果写成状态存储的
重新验证。LlamaIndex Adapter 与 RAGAS runner 仍按下一节保持 Planned。

## 2026-08-02 RAG 技术路线纠偏（ADR-017）

用户确认项目仍选择 **LlamaIndex** 作为 ingestion/retrieval 主框架，并且需要
**RAGAS** 离线评测。ADR-017 已取代 ADR-016；现有自研 RAG 代码保留为迁移期 reference
baseline，不再代表最终框架口径。此变更只纠正文档与展示语义：LlamaIndex Adapter、
RAGAS runner 仍未实现，能力表保持 Planned，后续必须以依赖、Adapter、contract test
和同数据集评测作为完成证据。

## 2026-07-31 Chat 检索与引用（未合并，PR #63）

分支 `pr-051-tool-execution-ledger`。本节记录 3.2 / 3.3；同分支的 2.2 与 3.1 见下两节。

**3.2 复核结论：条目已过期。** API 早已装配 sparse，`RetrievalService` 有 sparse
时走 Qdrant Query API 的一次 RRF。真实的洞是**正例从没被断言过**——所有装配测试都把
sparse 打桩成不可用，"有词法运行时却只接 dense 臂"能全绿然后被当 hybrid 评测。已补。

**3.3 引用改为可验证。** 只在模型**点名**且**被展示过**时才给出引用。没见过的 chunk id
一律丢弃——那是模型产出的字符串，回显等于让猜出来的标识带上本系统的权威。
连带把 `ChatTurnResult` 的 citations 与 authorized_revisions **相等**改为**包含**：
栅栏可以更宽（读了没引用的段落权限仍须成立），引用不能落在栅栏外。

```text
ruff / pyright                       全过
alembic 唯一 head                    0019_tool_executions
pytest（真实服务）                   1634 passed / 11 skipped
```

**代价（如实记）**：不按 `[chunk_id]` 约定作答的模型会得到零引用。这是如实而非虚报。

## 2026-07-31 Agentic 检索并存路径（未合并）

完成 [待办清单](./archive/followup-checklist-2026-07-29.md) 的 **3.1**。要点是它**不是**
把固定两步改成 agentic，而是并存：`TurnExecution` seam 之下 turn 生命周期共用，
`chat.retrieval_shape` 选形态，**默认仍是 `fixed`**。

| 落地 | 事实 |
|---|---|
| 授权 | agentic envelope 点名 `knowledge_search`，风险上限保持 read 默认 |
| 预算 | `max_agentic_steps` / `max_agentic_searches`，settings 跨字段校验 |
| evidence gate | `RetrievalJournal` 按 run 记录**模型看到的**全部证据，`finally` 取回 |
| 不变 | 固定路径仍然 registry 空 + envelope 空，有测试守着 |

```text
ruff / pyright / config-check        全过
alembic 唯一 head                    0019_tool_executions
pytest（真实服务）                   1622 passed / 11 skipped
```

**仍未做**：两条路径的对照评测。

## 2026-07-31 外部副作用 ledger（未合并）

分支 `pr-051-tool-execution-ledger`，基线 `main@13136e7`（上一节的 HITL 增量已合入）。
完成 [待办清单](./archive/followup-checklist-2026-07-29.md) 的 **2.2**，迁移 `0019`。

| 落地 | 事实 |
|---|---|
| `tool_executions` | `UNIQUE(task_id, operation_key)`；四态 `intended/succeeded/failed/needs_reconciliation` |
| 稳定 operation key | 业务 key，`tool_call_id` 只记录不入键；同 key 不同 canonical 参数冲突拒绝 |
| 两段提交 | 先 intent 后 dispatch，中间**重算授权**；全部写入按 Task 活跃 lease 栅栏 |
| 人工核对 | 判据是"有没有拿到答案"：超时/取消/预算耗尽 → 交给人，不重试也不写成失败 |
| 装配拒绝 | 注册了带 operation key 的工具却没有 ledger 的进程**起不来** |

另有一条与产品无关但影响证据可信度的修复：本机 `~/Documents` 的同步会持续生成
`* 2.py` 副本（三天 30 个），其中一个让 `alembic heads` 报两个 head、并让 pytest 多收
232 条重复用例。已加三层防护（`.gitignore` / `tests/conftest.py` 不收集 /
architecture guard 直接读 `migrations/versions`）。**此前公布的门禁数字未受影响**——
CI 在干净 checkout 上跑出的 `1074 passed / 506 skipped` 与本地一致。

```text
ruff format --check .                 passed（310 files）
ruff check .                          passed
pyright                               0 errors / 0 warnings
alembic 唯一 head                     0019_tool_executions
pytest（真实服务）                    1606 passed / 11 skipped
```

**仍未做**：`export_artifact`（WP10-07，唯一真实写节点）——协议就位，当前 build 里
还没有任何工具带 operation key。

## 2026-07-30 HITL Approval 收尾（未合并）

分支 `pr-050-postgres-checkpointer`，在下节 2026-07-29 快照之上再加四个提交
（`7014046`、`33ebbbb`、`a257e45`、`25895ca`），完成
[待办清单](./archive/followup-checklist-2026-07-29.md) 的 **2.1 全部内容**。

| 落地 | 事实 |
|---|---|
| Graph interrupt | `approval` 节点真正 `interrupt()`；恢复时**回查账本**，不信任 resume payload |
| 拒绝路径 | `approval` 成为条件节点：approved → export，rejected → 终态失败（**不**导出） |
| Worker | reconciliation 第 5/6 分支由真实 interrupt 驱动；无账本时 park 而非猜测 |
| Approval API | `GET /v1/approvals/{id}`、`POST /v1/approvals/{id}/decisions`；跨 owner/tenant 一律 404 同正文 |
| 发现路径 | 新增 `TaskApprovalRequested` 事件进 Task timeline（无列举端点） |
| 唤醒 | `NOTIFY task_ready`，四处入队事务内发送，payload 只有 `task_id` |

统一门禁（真实 PostgreSQL `127.0.0.1:5433` + Qdrant `127.0.0.1:6333`）：

```text
ruff format --check .                 passed（304 files）
ruff check .                          passed
pyright                               0 errors / 0 warnings
alembic 唯一 head                     0018_approvals
pytest（真实服务）                    1569 passed / 11 skipped
pytest（无外部服务）                  1074 passed / 506 skipped
```

两行 pytest 是同一套测试的两种环境，不能相加。

破坏验证四轮共 34 处，第一轮抓住 31 处；三处漏网已按性质分类并补测，最终 34/34。
详情与分类见待办清单 2.1 条目。

**仍未做**：`task_ready` 的监听端（Worker 仍轮询，属 3.5 同批工作）；
`tool_executions` 副作用 ledger（2.2）。

## 2026-07-29 当前工作分支快照（未合并）

本节记录分支 **`pr-050-postgres-checkpointer` 自 `5d943af` 之后的 A–F 汇合增量**，
不是 `main` 的发布快照。下面的历史章节保留各增量当时的基线和证据；当前结果以
本节记录的统一门禁为准。

| 修复组 | 当前状态 | 已落地事实 | 仍需验证或补全 |
|---|---|---|---|
| **A：Task 工作流终态语义** | **完成** | 显式成功/失败 disposition；revision 预算计数；critic 拒绝且预算耗尽时失败关闭 | 统一回归已通过 |
| **B：Task 提交幂等与租户隔离** | **完成** | tenant-scoped 幂等键、输入 fingerprint、冲突复用已存身份；Task API 按 owner/tenant 隐藏越权资源 | 统一回归已通过 |
| **C：单 Worker 纵向切片** | **完成** | TaskInput Artifact、Task API/CLI、独立 `agent-task-worker` 入口、poll loop 与显式 demo composition | 生产身份仍未实现，入口只适合受控环境 |
| **D：真实 Task Agent handlers** | **主体完成并通过回归** | `plan`/`critic` 结构化处理、内部检索/evidence Artifact、TaskRunContext、取消与授权上下文装配；外部检索经过 Tool/Policy 边界 | 真实外部搜索 Provider 尚未实现；当前 Adapter 在 Provider 缺失时失败关闭 |
| **E：可靠 Task Core** | **主体完成并通过状态测试** | PostgreSQL `SKIP LOCKED` claim、lease/heartbeat/epoch、stale reclaim、retry/dead-letter、专用 advisory guard、fenced checkpointer、生命周期事件及确定性 failpoint | HITL 与外部副作用 ledger 属于后续工作，不在本组已完成范围 |
| **F：产品化补全** | **部分完成** | Qdrant 启动不变量、常驻摄取入口及 claim/heartbeat/fencing、Task 生命周期时间线、本机 Compose 演示拓扑 | HITL Approval、真实外部搜索、OTel/Langfuse、CrewAI 对比、UI、生产身份与生产部署仍未完成 |

当前汇合工作树已于 2026-07-29 通过统一门禁：

```text
ruff format --check .                 passed（287 files）
ruff check .                          passed
pyright                               0 errors / 0 warnings
agent-config-check                    development / test 均为 status=ok
docker compose config --quiet         passed
alembic 唯一 head                     0016_task_principal_scopes
pytest（无外部服务）                  1054 passed / 409 skipped
pytest（真实 PostgreSQL + Qdrant）    1452 passed / 11 skipped
```

两行 pytest 是同一套测试的两种环境，不能相加。真实状态测试使用 PostgreSQL
`127.0.0.1:5433` 和 Qdrant `127.0.0.1:6333`；11 项跳过中 10 项需要真实 BGE
embedding/sparse 权重，1 项是只适用于非锁定 recovery read 的契约变体。下文
2026-07-28 的门禁数字仍只属于其注明的历史提交。

## 文档基线

状态：**已纳入 Git 版本管理**。

- [架构与技术选型基线 v1.3](./architecture-baseline.md)；
- [代码实施计划 v1.0](./implementation-plan.md)；
- [配置管理契约](./configuration.md)（当前 schema `1.10`，每一次抬升在该文件里对应一条 ADR）；
- [2026-07-25 仓库核验报告](./archive/repository-audit-2026-07-25.md)；
- [2026-07-27 仓库复核报告](./archive/repository-audit-2026-07-27.md)。

这些文档描述目标架构和增量计划，不代表其中列出的产品能力已经实现。

## 当前基线与编号对应

主分支基线：**`main@f5800d2`**（2026-07-28）。PR-047～PR-049 与后续 WP06
状态订正文档已全部合入。

**PR-035～PR-049 的全部增量都已经合入 `main`。** 下面各节里写的"尚未合入 `main`"
是当时开发分支上的状态，已按实际合并结果订正；每节保留的测试证据仍是**该增量当时**
的门禁数字，不是当前数字。

本文件的 `PR-0NN` 是**增量编号**——一个编号对应一次行为变化；GitHub 的 `#NN` 是
**合并编号**——一次合并可以携带多个增量。两套编号不相等，也不能互相推算：

| 文内增量 | GitHub 合并 | 提交 |
|---|---|---|
| PR-033 摄取 worker | `#49` | `4d03f69` |
| PR-034 `knowledge_search` Tool | `#50` | `de426e2` |
| PR-035 安全发布基线 | `#51` | `7025425` |
| PR-036 ～ PR-043 Chat 可靠性 | `#52` | `07f4a27` |
| PR-044 Task 工作流状态 | `#53` | `3538e26` |
| PR-045 Reranker | `#54` | `3b7829b` |
| PR-046 Sparse 加载守卫 | `#55` | `e93d7a1` |
| 文档基线订正 | `#56` | `260ca0e` |
| PR-047 控制流与 fan-in reducer | `#60` | `4ec04d2` |
| PR-048 Agent node | `#58` | `0830e55` |
| PR-049 LangGraph adapter | `#59` | `341cbf5` |

### 2026-07-28 全仓门禁复核（`main@341cbf5`）

```text
ruff format --check .    passed（214 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
agent-config-check       development / test / production 均为 status=ok
alembic 唯一 head        0009_chat_turn_lease

pytest（无外部服务）              907 passed / 260 skipped
pytest（真实 PostgreSQL + Qdrant） 1156 passed /  11 skipped
```

**两行 pytest 是同一套测试的两种环境，不能相加。** 第二行是本项目第一次真正跑通
需要外部服务的那 260 项——它们全部通过。此前每一轮增量都只能写"真实 PostgreSQL
用例因未配置 DSN 而跳过"，也就是说 lease、`SKIP LOCKED` 回收、原子发布、迁移这些
不变量一直靠**跳过的测试**撑着。现在它们是实测过的。

剩下的 11 项跳过全部需要真实 BGE 权重
（`AGENT_WORKBENCH_TEST_EMBEDDING_MODEL` 未设置）。

复现方式（PostgreSQL 镜像 digest 与 CI 固定的一致）：

```bash
docker run -d --name aw-postgres -p 5433:5432 \
  -e POSTGRES_USER=agent -e POSTGRES_PASSWORD=ci-only \
  -e POSTGRES_DB=agent_workbench_test \
  postgres:16@sha256:33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20
export AGENT_WORKBENCH_TEST_DSN="postgresql+asyncpg://agent:ci-only@127.0.0.1:5433/agent_workbench_test"
export AGENT_WORKBENCH_TEST_QDRANT_URL="http://localhost:6333"
alembic upgrade head && pytest -q
```

容器映射到 **5433** 而不是 5432：开发机上可能已经跑着原生 PostgreSQL，它会遮蔽
Docker 的端口映射，症状是 `role "agent" does not exist`——一个看起来像配置错误、
实际是端口被占的失败。

此前记录的 1 项 deselect 是当时沙箱禁止 `socket.bind()` 所致，属于**环境差别而不是
代码变化**；本轮环境允许该调用，loopback 真实性测试正常执行并通过。

`production` profile 需要 CI 同款环境变量（固定 model ID、40 位 embedding/reranker
revision、DeepSeek 与 Qdrant 密钥）才能通过；只用 `.env.example` 会在 Qdrant 密钥
这一项失败关闭，这是配置契约的预期行为。

## 2026-07-28 PR-058 WP07-03：Task 提交时的语义与授权身份落库

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。迁移 `0012`。

`task_runs` 增加五列：`run_semantics_snapshot`、`run_semantics_revision`、
`submitted_policy_revision`、`submitted_policy_fingerprint`、
`submitted_authorization_envelope`。确定性快照的机制**此前已在 settings 层实现**
（`run_semantics_snapshot()` / `task_run_semantics_revision()` / `policy_identity()`），
本轮做的是把它**持久化**并让提交路径带上它。

### 快照与 policy 身份分开存

快照是**恢复时沿用**的东西；policy **每次 claim、每次 dispatch 都重新求值**。两列 policy
身份只记录"调用方当时是在哪套规则下被授权的"，因此有效授权始终是
"提交时的 envelope ∩ 当前规则"，而不是"当时允许什么就一直允许什么"。

envelope 读回来是**模型**而不是裸 dict：一个当成 JSON 读的授权上限，是没有任何东西会再校验
一遍的上限。

### 三处"不能是可选"的地方

**`TaskSubmission` 的这些字段是必填的。** 一个可以省略语义的提交会产出一个"恢复时无物可恢复"
的 Task，而这个疏漏只会在恢复的那一刻才被发现。

**列是 NOT NULL 且无默认值**，所以迁移在表里已有行时会直接失败。这是故意的：在这些列存在之前
提交的 Task 没有"提交时语义"，回填一个占位符等于**编造**一份——而恢复时会把编造的那份当真。

**`semantics` 是注入的 callable 而不是一个值**：涉及知识库的 Task 要在**每次提交**时解析
Qdrant alias（WP07-04），不是启动时解析一次。

### 幂等的判定范围跟着变宽

同一个 dedup key 配**不同的语义修订或不同的 policy 指纹**，现在是
`TaskSubmissionConflictError`。否则调用方的工作会在它从未要求过的语义下运行——正是快照要防的
那个失败，只不过是从幂等路径而不是从恢复路径进来的。

### 有牙验证

4 处破坏。其中**两处是我自己写坏的空操作**（改了等价代码 / 加了个 `# noqa`），不算发现；
如实记下来是因为"破坏了但测试没红"如果不追究，就会被当成覆盖率证明。真正的两处：

| 破坏 | 结果 |
|---|---|
| 语义不算作"提交是什么"的一部分 | 2 条测试失败 |
| 快照列改成 nullable | 迁移漂移测试 2 条失败（第一轮漏跑，因为它不在我选的目标文件里） |

本轮门禁：

```text
ruff check / format      passed（235 files）
pyright                  0 errors / 0 warnings
alembic 唯一 head        0012_task_submitted_semantics
pytest（无外部服务）              955 passed / 363 skipped
pytest（真实 PostgreSQL + Qdrant） 1307 passed /  11 skipped
```

### 本轮明确未做

- **没有任何地方从 settings 构造 `SubmittedSemantics`**：服务收一个 callable，组装点仍不存在；
- Qdrant alias 解析与 generation reservation（WP07-04）没做，所以三个 `resolved_*` 列还没有；
- 快照内容本身没有新增校验（"与两个 resolved 字段不一致时 fail closed"要等 WP07-04）。

## 2026-07-28 PR-057 WP07 开始：Task 与它的开场事件同事务提交

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP07-02 的第一半，
也是 WP07 的第一条退出条件：**状态和事件同事务提交**。

新增一个事件类型 `TaskSubmitted`，`PostgresTaskRegistry.submit` 用**同一个连接、同一个事务**
写入 `task_runs` 行和这条事件。二者要么一起提交，要么一起回滚——不会存在"有 Task 但没有任何
东西说明它为什么存在"，也不会存在"事件描述了一个被回滚掉的 Task"。

事件只带**提交决定了什么**，不带它引用的东西：objective 在 `input_ref` 后面。事件会被重放进
时间线和 SSE 帧，调用方提交的正文没有理由在那里被复述一遍。

日志是**默认构造**而不是可选注入：没有办法造出一个"跳过事件"的 registry，只能换一个日志。

### 两处被测试逼出来的修正

**冲突检查必须在 append 之前。** 同一个 dedup key 配不同请求，本来会先撞上事件的
`event_key` 幂等校验，报成 `EventKeyConflictError`——一个关于本项目**自己记账方式**的错误，
扔给一个只是犯了普通错误的调用方。挪到 append 前之后，它是 `TaskSubmissionConflictError`。

**时间线不再有"空"这个状态。** 一个刚开出来的 Task，时间线上已经有它自己的开场事件了——
因为二者同事务。相关测试从"没有事件"改成断言**第一条就是 `TaskSubmitted`**。

### 有牙验证：6 处破坏，5 处被抓住

| 破坏 | 失败测试数 |
|---|---|
| 事件不带幂等 key | 1 |
| 冲突检查挪到 append 之后 | 2 |
| 事件写进另一条流 | 6 |
| 干脆不写事件 | 11 |
| `TaskSubmitted` 改成 transient | 38 |
| **append 改成自己开事务** | **0** |

最后一行是**没有修掉的**，如实记下来：`PostgresEventLog.append` 内部就是
`begin()` + `append_durable_in_transaction`，所以从外部看，"用调用方的事务"和"自己开一个"
在 append 那一刻**完全一样**——两者此时都还没提交。要区分只能在 `submit` 里加一个仅供测试
存在的接缝。没有加。改为覆盖它确实成立的两条性质：**append 失败则 Task 不存在**，
以及**重复提交不追加第二条事件**；再加一条从**第二个连接**观察、断言事务外看不到该事件。

本轮门禁：

```text
ruff check / format      passed
pyright                  0 errors / 0 warnings
pytest（无外部服务）              955 passed / 359 skipped
pytest（真实 PostgreSQL + Qdrant） 1303 passed /  11 skipped
```

### WP07 剩下的

WP07-01（仓储与状态机）与 WP07-05/06（`events`/`event_streams`、per-stream sequence、
cursor codec）此前已经落地。仍未做：WP07-02 的 `NOTIFY task_ready` 与输入存储、
WP07-03 语义快照与 submitted policy identity、WP07-04/08 Qdrant generation reservation
与 Task-aware GC、WP07-07 其余 durable 事件。

## 2026-07-28 PR-056 统一事件时间线：WP06 最后一条退出条件

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-09。
`TaskService.timeline()` 按游标切片返回一个 Task 的事件。

### "统一"是指**一条流**

一个 Task 的事件全部落在**它自己的 workflow thread** 这一条流上，所以
`(stream_id, sequence)` 一个游标就等于"这个 Task 到此为止的全部"，重连的客户端也只回传
一个值。Task 与 thread 是一对一——Registry 的唯一约束**双向**保证——这正是让单游标成立的
前提。流 ID 的推导只写在 `task_stream_id()` **一个地方**，写入方和读取方不可能对"事件去哪了"
产生分歧；把它改成所有 Task 共用一条流会让测试失败。

### 三个不显眼但要紧的地方

**授权检查在读日志之前，且用的是同一条。** 时间线如果对别人的 Task 给出不同答案，泄露的恰好
是 Task 查询拒绝泄露的那件事——这个 id 存在。所以它先走 `get()`，两者同样是 `NotFoundError`。

**外来游标是拒绝，不是忽略。** 游标是客户端提供的值；忽略它等于对一个"想接着看另一条流"的
客户端，从头把**这个** Task 的历史端上去。

**空切片不推进游标。** 返回流末尾会跳过"这次读和下次读之间到达"的事件；没投递出去，位置就
没有移动。

### 有牙验证里的又一个诚实结果

7 处破坏，第一轮 6 处。漏网的是"**limit 不设上限**"——原测试传了一个超大 limit 然后断言
"返回 3 条"，可库里本来就只有 3 条，**上限有没有生效根本看不出来**。

它没有靠"多存 500 条事件"来修，而是搬到了能观察的地方：用一个**记录被要求了什么**的假日志，
直接断言服务传给日志的 limit 是被夹住之后的值。PostgreSQL 那侧只保留"limit=0 被拒绝"。
补完后 7 处全部被抓住。

| 破坏 | 失败测试数 |
|---|---|
| 先读日志再做授权 | 1 |
| 忽略外来游标 | 1 |
| 游标不推进 | 3 |
| 空切片把游标推到流末尾 | 2 |
| limit 不设上限 | **第一轮 0**，改测试后 1 |
| 没有日志时返回空时间线 | 1 |
| 所有 Task 共用一条流 | 1 |

本轮门禁：

```text
ruff format --check .    passed（234 files）
ruff check .             passed
pyright                  0 errors / 0 warnings

pytest（无外部服务）              955 passed / 353 skipped
pytest（真实 PostgreSQL + Qdrant） 1297 passed /  11 skipped
```

### WP06 退出条件

**全部满足。** 事件时间线这一条按计划的说法"M3a 可用内存/测试 EventLog，WP07 替换为
PostgreSQL durable EventLog，不改变接口"——本轮直接用了已经存在的 `PostgresEventLog`，
所以那次替换已经不需要了；接口是 `EventLogPort`，两种实现同一个口子。

### 本轮明确未做

- **没有 HTTP/CLI 入口**：时间线只有服务方法，`event_stream.replay_source` 那套 SSE
  与 `Last-Event-ID` 是 WP09；
- Worker 目前**不往这条流里写**任何东西：写入的是 Agent 节点的 `EventSink`，而组装
  Worker → 节点 → sink 的那个组装点还不存在（WP06 之后）。所以真实运行里这条流现在是空的，
  测试用真实 `EventLogPort` 追加的是 Agent 运行会产生的同一种事件；
- Task 生命周期本身还没有事件类型（`TaskSubmitted` 等）：`EventPayload` 是封闭联合，
  加成员要动 schema 版本，属于 WP07-07。

## 2026-07-28 PR-055 TaskService 与可切换的 FakeExecutor

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。
补完 WP06-07 的 TaskService/查询接口，并完成 WP06-08。

### TaskService：两件仓储**故意不做**的事

**thread_id 与 graph_version 由它铸造，不由调用方给。** 仓储收下这两个字段是因为它们是行上的
事实；**选**它们是决定。让调用方给 thread，重试就可能为同一个 dedup key 给出**不同**的 thread，
唯一约束会直接拒绝——本该幂等的重试变成报错。让调用方给版本，客户端就能把自己钉在一个没人再部署
的图上。

**按 id 读是一条授权边界。** "不存在"和"不是你的"必须给出**同一个**答案——答案不同本身就是泄露，
它确认了这个 id 存在。所以两者都是 `NotFoundError`，没有 "forbidden"。归属**两项都查**：只查
tenant 会把一个租户的 Task 暴露给租户内每个人；只查 owner 会让跨租户的同名 id 撞进别人的 Task。
有一条测试直接断言两种拒绝的**类型、code 与消息逐字相同**。

### WP06-08：切换在节点注入，不在配置

M3a 验收门槛是"**单个 Agent 节点**可切换 FakeExecutor/自研 Runtime"。本轮把
`FakeAgentExecutor` 作为**正式代码**发布（不再只是测试替身），因为它换来的东西不是测试：整条
Task 链路——提交、领取、跑图、checkpoint、崩溃后恢复——可以在**没有 provider、没有 key、没有
费用**的情况下端到端跑完。

**没有**去放宽 `runtime.executor`。那个单值 `Literal` 编码的是一条已冻结的不变量——
**Tool Loop 只有一个所有者**。加一个 `"fake"` 值等于推翻它，而按项目规矩那需要先写 ADR、
升 config schema 版本，不是顺手加个开关。有一条测试**正面钉住**这一点：
`RuntimeSettings.model_validate({"executor": "fake"})` 必须失败。

fake 只做边界真正承诺的两件事：**返回终态结果**、**观察取消**。取消返回 `cancelled` 结果而不是
抛异常——调用方是个无论如何都要记录和路由的图节点，这正是只走happy path 的替身最容易漏掉的一条。
其余全部是请求的函数：同一请求给同一结果（CI 不依赖模型），不同请求给**不同的 sha256**
（常量摘要会让任何做去重的东西把两次无关的运行当成同一次），并且**记账不为零**——一次不花钱的运行
会让所有预算检查静默通过。

### 有牙验证

9 处破坏，全部被抓住，0 处漏网：

| 破坏 | 失败测试数 |
|---|---|
| 读取时忽略 tenant / 忽略 owner | 1 / 2 |
| "不是你的"与"不存在"给不同答案 | 1 |
| thread_id 跨提交复用 | 1 |
| graph_version 不来自服务的决定 | 1 |
| fake 取消时抛异常而不是返回 | 1 |
| 每个 fake artifact 同一个摘要 | 1 |
| fake 运行不记账 | 1 |
| fake 忽略脚本化的回答 | 2 |

本轮门禁：

```text
ruff format --check .    passed（233 files）
ruff check .             passed
pyright                  0 errors / 0 warnings

pytest（无外部服务）              953 passed / 344 skipped
pytest（真实 PostgreSQL + Qdrant） 1286 passed /  11 skipped
```

新增 15 条测试，**全部不需要外部服务**。

### 本轮明确未做

- **WP06-09 事件时间线**：Task 查询目前返回 Task 本身，不返回统一事件时间线；
  那是 WP06 最后一条未满足的退出条件；
- TaskService 不写入 `input_ref` 指向的东西——提交事务与输入存储是 WP07-02；
- 没有 HTTP/CLI 入口调用它；组装点仍然不存在。

## 2026-07-28 PR-054 单 Worker runner：整条链第一次跑通

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-07 的第四步。
`TaskWorker` 把仓储、恢复判定和工作流适配器接起来。

### 它自己不做任何决定

领任务是仓储的条件更新，判断两个事实合起来是什么意思是 `reconcile`，跑图是适配器的。
这里只剩把它们连起来的循环，以及**什么时候停**。

**"跑图"被表达成"再判断一次"**，而不是第二套状态机。Worker 启动一张图之后再问同一个问题：
位置变了，答案就跟着变——从 `start` 变成 `settle_succeeded` 或 `wait_for_approval`。
在 `ainvoke` 后面直接写"然后标记成功"，就是把恢复判定又抄了一遍措辞不同的版本，而改动原版之后
还在跑的正是那份抄件。测试直接断言一次成功运行产生的 action 序列是
`["start", "settle_succeeded"]`——**两次判断，不是一次**。

循环**有上界**（3 次）。既不结束也不等待的图，Worker 结算不了；对它无限循环比如实记下来更糟，
所以预算用尽就进 `failed`。

每轮都**重新读 Registry**，不信任领取时那一行：图跑着的时候落下来的取消，正是判定的第一个分支
要看到的事实。

### 端口多了一个 `inspect`

判定需要"这个 thread 停在哪"，而**不能靠试着 resume 再读异常**。所以
`TaskWorkflowPort` 增加 `inspect(thread_id) -> CheckpointPosition | None`，
`CheckpointPosition` 也从 application 移到端口——端口不能反向依赖 application，而"图停在哪"
本来就是这个边界的词汇。

适配器实现里有一处**诚实的取舍**：checkpoint 的版本若未记录或本进程建不出来，就**只报版本、
不报待执行节点**——待执行节点是 LangGraph 对那张图的计算，要拿它就得先编译那张图。文档写明
这不含糊：这种位置在任何人读它的待执行节点之前就已经被判定 park 了。

### 有牙验证：11 处破坏，第一轮 10 处

漏网的那一处是**"inspect 把建不出来的 checkpoint 报成当前图的版本"**——没有测试失败，因为
现有用例里"Registry 的版本建不出来"总是先一步挡住，从来没走到"Registry 版本没问题、
但**checkpoint** 是另一张建不出来的图写的"。

补的测试构造了这个状态：先用一个**有** v9 的 Worker 把它跑起来（checkpoint 记下 v9），
再把 Registry 的版本改成 v1 并重新排队，然后交给一个**没有** v9 的 Worker。这时若谎报成 v1，
一个读不出来的 checkpoint 会显得"已完成"，Task 被结算成 succeeded——补上后 11 处全部被抓住。

| 破坏 | 失败测试数 |
|---|---|
| 信任领取时的行，不重新读 Registry | 1 |
| 只判断一次，什么都不结算 | 3 |
| 判断次数无上界 | 1 |
| 抛异常的图被留在 running | 2 |
| 失败原因带上 provider 的异常正文 | 1 |
| park 时不带原因 | 2 |
| 已终态的 Task 再结算一次 | 1 |
| `start` 与 `resume` 对调 | 4 |
| `inspect` 报告没有 checkpoint | 6 |
| `inspect` 报告它没读过的待执行节点 | 2 |
| `inspect` 谎报建不出来的 checkpoint 的版本 | **第一轮 0**，补测试后 1 |

### 整条链的证据

第一个 Worker 死在 `critic`，Task 进 `failed`；把它的 engine、saver、workflow、registry、
handler 闭包**全部丢弃**；重新排队后，一个**从零构建**的 Worker 领走它并跑完——
action 序列是 `["resume", "settle_succeeded"]`（不是 `start`），第二个进程里
`understand` 与 `research_internal` 的调用次数是 **0**。

（重新排队这一步是测试手工做的：按 lease 过期自动重排是 WP08 的 reaper，这条测试关心的是
之后发生什么，不是谁触发的。）

失败原因只记**异常类型**不记异常正文：provider 的异常文本里有请求体和 prompt 片段，而这个字符串
会进事件和 API 响应。有测试断言 `"RuntimeError" in detail` 且 `"died mid-run" not in detail`。

本轮门禁：

```text
ruff format --check .    passed（229 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
alembic 唯一 head        0011_task_runs

pytest（无外部服务）              938 passed / 344 skipped
pytest（真实 PostgreSQL + Qdrant） 1271 passed /  11 skipped
```

### 本轮明确未做

- **没有轮询循环、没有 `LISTEN/NOTIFY`**：只有 `run_once()`。什么时候再调一次是调用方的事，
  长驻循环与唤醒属于协调；
- **没有 lease、没有 advisory lock、没有 fencing**，所以这个 Worker **只能跑一个**。多 Worker
  是 WP08，`FencedCheckpointer` 也在那里；
- `load_state` 是注入的 callable 而不是端口：`input_ref` 指向什么由提交事务（WP07-02）决定，
  在这里发明一个存储就是替那次改动先做了决定；
- TaskService 与查询接口（含统一事件时间线，WP06-09）还没有；
- `approval_decision` 恒为 `None`——本版没有任何图能中断，审批是 WP10。

## 2026-07-28 PR-053 Task Registry 仓储：状态机是数据，SQL 由它推导

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-07 的第三步。
`TaskRegistry` 端口 + `PostgresTaskRegistry`。转换全部是**条件 UPDATE**，`WHERE` 里的
合法来源状态**从领域的转换表推导**（`sources_for`），不在 SQL 里重写一遍——规则写两遍，
留下来跑的总是没被改的那一遍。

实施计划 §7.4 的"接口层不能直接写状态字符串"落实为：方法按**发生了什么**命名
（`mark_succeeded` / `park_for_migration` / `await_approval` / `cancel`），没有
`transition(to=...)` 这种把状态字符串又还给调用方的口子。

`ALLOWED_TRANSITIONS` 里终态**没有任何出边**——"迟到的审批不能复活已取消的 Task"落到实处
就是这一条。`waiting_migration` 同样没有出边：计划里没写谁执行迁移、怎么执行，凭空加一条边
就是发明一个没人设计过的流程。这两点都有测试钉住。

### 破坏验证发现的三件事，两件是真缺陷

第一轮 11 处破坏只抓住 8 处。三处漏网各自的结论不同，都不是"补个断言"能了事的：

**一、`start_next` 的 `status = 'queued'` 条件被删掉，没有测试失败。**
写代码时的注释说"单 Worker 下它和子查询等价"。**实测证明这句话是错的**：让另一个事务先把该行
改成 `running` 但不提交，再跑 claim——

```text
带 status 条件：返回 None
去掉 status 条件：返回 task_x   ← 同一个 Task 被派发了两次
```

PostgreSQL 在行被并发更新后会**重新校验 UPDATE 的限定条件**，但**不会重跑限定条件里的
子查询**，于是 `task_id = (SELECT ... WHERE status='queued')` 仍然匹配一个已经不是 queued
的行。这个条件是唯一挡住重复派发的东西。注释已按实测改写，并补了对应测试。

**二、`submit` 改成"先读后插"，没有测试失败。**原来的并发测试用 `asyncio.gather` 起 5 个
提交，实测它们**确实并发**（全部开始早于任何一个结束），但在没有屏障的情况下几乎总是自然串行，
race 根本没发生。改成**确定性**写法：另一个事务先插入冲突行且不提交，`submit` 会阻塞在唯一
索引上，提交后它只能走"输掉"的那条分支。此时——

```text
ON CONFLICT DO NOTHING：返回赢家的 Task
先读后插：            IntegrityError
```

（用强制屏障单独验过一次：5 个"先读后插"并发，4 个 IntegrityError。）

**三、`_move` 里"该带原因/不该带原因"的检查删掉后没有测试失败——因为它根本到不了。**
五个公开方法的**签名**已经决定了要不要 `reason`，多传或少传都是 `TypeError`。那段检查是
死代码，删掉了。

换上的是一个**真的够得着**的检查：`reason=""`。空串既满足列的 `NOT NULL`，也满足
`status_detail` 的 CHECK，于是**写得进去、读不回来**——`TaskRun.status_detail` 要求非空。
这是失败最糟糕的形状。现在用 `TaskRun` 同一个类型去校验，两边不可能各说各话；删掉这个检查会让
2 条测试失败。

补完之后 11 处破坏全部被抓住，0 处漏网。

本轮门禁：

```text
ruff format --check .    passed（227 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
alembic 唯一 head        0011_task_runs

pytest（无外部服务）              938 passed / 334 skipped
pytest（真实 PostgreSQL + Qdrant） 1261 passed /  11 skipped
```

### 本轮明确未做

- Worker 还没有；仓储只是它要用的东西；
- 没有 lease、没有 `SKIP LOCKED`、没有 advisory lock、没有 priority——多 Worker 是 WP08。
  但上面那条"重复派发"的实测结论已经预先钉住了 WP08 里最容易写错的地方；
- `waiting_migration` 与 `waiting_approval` 的出边留给各自的流程（迁移程序、WP10 审批）。

## 2026-07-28 PR-052 Task Registry 的行，以及一处订正

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-07 的第二步：
`task_runs` 的产品生命周期，schema 与约束。仓储与 Worker 还没有。

### 订正：状态少了一个

[PR-051](#2026-07-28-pr-051-worker-的恢复判定基线-95-的七种情形) 的
`TaskStatus` 写了七个状态，并声称"这是基线点过名的全部"。**实施计划 §7.4 列的是八个**，
多一个 `dead_letter`（重试预算耗尽的毒任务）。本轮补上，并归入终态——它的作用正是让毒任务
不再被反复领取。`reconcile` 不需要改：终态分支本来就按集合判断，全枚举测试也自动覆盖到它。

### 这一轮**不**加的列

按实施计划 §7.2 的分工，各自留给自己的工作包：lease、epoch、attempt、`available_at`
退避与 recovery reason 属于协调（WP08），而**单 Worker 一个都不需要**；run semantics
快照与 submitted policy identity 是 WP07-03；resolved Qdrant collection / index version /
generation reservation 是 WP07-04。分开落是本仓已有的做法——`0008_chat_turns` 落 Turn，
`0009_chat_turn_lease` 才落它的执行 lease。

### 两条硬约束

**一个 owner 的一个 submission key 不能开出第二个 Task。** "重复 submission key 返回同一
Task"是退出条件；*返回*同一个是仓储的事，*不可能存在第二个*是这条唯一约束的事。它按 owner
分域：全局唯一会让一个用户选的 key 拒绝掉另一个用户的提交，而且会告诉对方这件事发生了。

**`thread_id` 双向唯一。** 一个 Task 对应恰好一个 thread，一个 thread 背后恰好一个 Task，
因此 reconciliation 永远不会拿到"一个 checkpoint 对两行 Registry"。

`status_detail` 的约束是**双向**的：`waiting_migration` / `failed` / `cancelled` /
`dead_letter` 必须带原因，其余四个状态必须不带。只做前一半的话，一段解释会活过让它失效的那次
转换，然后被当成一个其实没问题的 Task 的当前说明来读。

### 有牙验证里的一个诚实结果

7 处破坏，**6 处被抓住，1 处没有**——而那 1 处是有意义的：单独删掉状态词汇表的 CHECK，
**没有任何测试失败**。原因是 `status_detail` 那条 CHECK 把状态配对成两组，一个未知状态两组
都不属于，于是它自己就把行挡下来了。也就是说词汇表 CHECK 在可观测行为上是**冗余**的。

它仍然保留：等哪天 `status_detail` 规则被放宽，词汇表就会在**没人察觉**的情况下失去约束。
这个结论写进了那条测试的文档，测试本身也改成同时验证"带原因"和"不带原因"两种未知状态写法，
而不是继续声称自己在验证那条已被覆盖的 CHECK。

| 破坏 | 失败测试数 |
|---|---|
| 单独删掉状态词汇表 CHECK | **0（冗余，已记录）** |
| 数据库词汇表里少 `dead_letter` | 1 |
| 需要人处理的状态可以不带原因 | 10 |
| 不需要人处理的状态可以留着原因 | 5 |
| 同一 owner 可以重复提交同一 key | 1 |
| submission key 改成全局唯一 | 1 |
| 两个 Task 共用一个 thread | 1 |

本轮门禁：

```text
ruff format --check .    passed（224 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
alembic 唯一 head        0011_task_runs

pytest（无外部服务）              937 passed / 307 skipped
pytest（真实 PostgreSQL + Qdrant） 1233 passed /  11 skipped
```

## 2026-07-28 PR-051 Worker 的恢复判定：基线 §9.5 的七种情形

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-07 的第一步。

Task Registry 表示产品生命周期，LangGraph checkpoint 表示图执行位置；本项目不把两者
伪装成一个分布式事务，所以"领到一个 Task"就等于要判断**这两个事实合起来是什么意思**。
架构基线 §9.5 把这个判断列成了七种情形，本轮把它写成一个**全函数**。

### 为什么是纯函数

将来调用它的 Worker 要同时持有 advisory lock、lease 和 guard 连接，这七个分支里的每一个
经由真 Worker 去够都很贵——"图停在一个后来被拒绝的审批上"这种情形，要 lease、要锁、要一张
能 interrupt 的图，跑完还只覆盖七分之一。写成对值的判断后，全部分支微秒级可达，Worker
那边只剩真正需要 I/O 的部分。

这和本仓已有的做法一致：`workflows/research_graph.py`（纯声明）先落，
`adapters/langgraph/workflow.py`（执行）后落。

### 判定顺序不是随意的

终态**先于**读 checkpoint 判断：一个已取消、但 checkpoint 里还有待执行 node 的 Task，
不是"还没跑完的活"，是"被人停下的活"。反过来先读 checkpoint 就会把它变回 running。

`waiting_migration` **必须显式挡住**，不能落到后面：它不是终态，它的 checkpoint 看起来
完全可恢复，只读 checkpoint 会把它直接放回去跑，撤销掉别人正等着做的那个决定。

版本比较有两半，都进 `waiting_migration`：**本进程根本没有这张图**（未注册），和
**checkpoint 是另一张图写的**。第三种情况是 checkpoint **没记录**版本——它不比"版本不一致"
更可恢复，猜"大概就是 Task 注册的那个版本"是在最贵的地方做猜测。这一条正好接上
[PR-050（三）](#2026-07-28-pr-050三workflow-身份进-checkpoint端口层跨进程续跑)：
版本现在写在 checkpoint metadata 里，所以这个判断**在调用 resume 之前**就能做出，
而不是去 catch 一个异常。

"图已结束"与"停在审批"两条**互斥而不只是有序**：`CheckpointPosition` 拒绝被构造成
"没有待执行 node 却在等审批"，所以两个分支谁也藏不住谁。

### 一个诚实的说明

七种情形里有两种（停在审批、审批已有决定）描述的是 **M3a 还产生不出来的图**——`approval`
在 WP10 之前是无副作用占位节点。仍然实现它们，是因为另一条路是"对一张被中断的图静默回答
resume"，而且输入结构为了让其它分支成立本来就得携带审批信息。这一点写在模块文档里。

### 有牙验证

11 处破坏，全部被抓住，0 处漏网。其中最值得记的一条：把 `waiting_migration` 的显式分支
去掉后**只有 1 条测试失败**——而那条测试是做破坏验证之前**补上**的。第一版测试里
"totality" 那条只断言"每种输入都得到某个 action"，`waiting_migration` 落到 `resume`
一样满足它。全枚举不等于全断言。

| 破坏 | 失败测试数 |
|---|---|
| 终态检查挪到读 checkpoint 之后 | 5 |
| `waiting_migration` 落到后面 | 1 |
| 未注册的 graph version 照跑 | 1 |
| 没记录版本的 checkpoint 当作匹配 | 1 |
| 版本不一致照样 resume | 1 |
| 已结束的图去 resume 而不是结算 | 2 |
| 忽略审批决定 | 3 |
| 等审批时仍占住 Worker | 1 |
| `wait_for_migration` 不再改状态 | 2 |
| 允许"已结束且在等审批" | 1 |
| 决定里丢掉 approval id | 1 |

本轮门禁：

```text
ruff format --check .    passed（222 files）
ruff check .             passed
pyright                  0 errors / 0 warnings

pytest（无外部服务）              935 passed / 291 skipped
pytest（真实 PostgreSQL + Qdrant） 1215 passed /  11 skipped
```

新增 19 条测试，**全部不需要外部服务**，所以两列的 `passed` 同步 +19、`skipped` 不变。

### 本轮明确未做

- **没有 Worker 调用它**。claim/lease/advisory lock、`task_runs` 仓储与状态机
  （WP07-01）、TaskService 与查询接口都还没有；
- `TaskStatus` 只定义了基线点过名的七个状态，没有 `task_runs` 表、没有列、没有仓储。
  这里定义的是**词汇表和判定**，不是存储；
- 判定不涉及 Qdrant generation reservation（WP07-04）与取消传播的具体事件写入。

## 2026-07-28 PR-050（三）workflow 身份进 checkpoint，端口层跨进程续跑

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。WP06-06 的最后一步。
`LangGraphTaskWorkflow` 不再持有 `_thread_versions` 字典；"这个 thread 存在吗"和
"它是哪张图写的"两个问题都改成**问 checkpointer**。

### graph version 存哪里：选了第三个位置

三个候选：`task_runs.graph_version`（实施计划 §6，属 WP06-07）、adapter 自己的一张表、
或者**checkpoint 的 metadata**。选第三个。

机制是契约自带的：`get_checkpoint_metadata` 会把 `config["configurable"]` 上的标量
拷进每一个 checkpoint 的 metadata。所以 adapter 只要在调用图时把
`{"configurable": {"thread_id": ..., "graph_version": ...}}` 传下去，版本就自动、逐个
checkpoint 地持久化了——不需要 adapter 自己的表，也没有第二处需要保持同步的写入。

**这不是把 Task 产品状态复制过来。** `task_runs.graph_version` 记的是"这个 Task 被要求
用哪张图跑"；这里记的是"实际写下这个执行位置的是哪张图"。ADR-014 禁止的是 checkpointer
成为前者的第二个 writer，而后者本来就属于架构基线 §9 说的"图执行位置"。

代价也记下来：PR-049 时 `workflow.py` 里写着"LangGraph 的 checkpoint 不携带 graph
version"。**那句话现在不成立了**，因为这次让它携带了。

### 没记录版本的 checkpoint 拒绝续跑

不是本 adapter 写的 checkpoint（本次改动之前的行，或者绕过 adapter 直接驱动的图）
metadata 里没有 `graph_version`。这时 `resume` **失败关闭**，报出的
`checkpoint_graph_version` 是 `<unrecorded>`——一个 `GraphVersion` 的正则
（必须以字母数字开头）**永远匹配不上**的字符串，所以调用方拿它和任何真实版本比较都得到
"不同"，而这正是答案。猜"大概就是唯一注册的那个版本"是在最贵的时刻做的猜测。

版本比较**发生在编译图之前**：否则一个由本进程已不再注册的版本写的 checkpoint，会被报成
"未知版本"而不是"它由 v1 写的"。

`run` 的存在性检查也改成问 checkpoint，因此**另一个进程起过的 thread 不再是空闲的**。
它是检查不是锁：两个 first run 抢同一个 `thread_id` 由 Task lease（WP08）排除，不在这里。

### 有牙验证

6 处破坏，全部被抓住，0 处漏网：

| 破坏 | 失败测试数 |
|---|---|
| 版本不放进 config，于是没有 checkpoint 记录它 | 7 |
| 没记录版本时按调用方要的版本当作已记录 | 1 |
| `run` 不问 thread 是否已存在 | 3 |
| `resume` 完全不比较版本 | 4 |
| 版本比较挪到编译图之后 | 2 |
| `resume` 又去信任对象内存而不是 checkpoint | 6 |

端口层的关键证据：第一个进程死在 `critic`，随后 adapter、saver、engine、连接池、
handler 闭包**全部丢弃**，第二个进程只拿到 `thread_id` 和 graph version，
`resume` 跑完全程——`understand` 与 `research_internal` 在第二次运行里调用次数为 **0**，
返回的 `TaskWorkflowResult.state` 带着上一个进程采集的证据。

另有一条测试直接对 `workflow_checkpoints.metadata` 做 JSONB 谓词查询，按 graph version
选出 thread，并断言**没有任何 checkpoint 是未记录版本的**——这是把 metadata 存成 JSONB
而不是不透明字节换来的东西。

本轮门禁：

```text
ruff format --check .    passed（219 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
alembic 唯一 head        0010_workflow_checkpoints

pytest（无外部服务）              916 passed / 291 skipped
pytest（真实 PostgreSQL + Qdrant） 1196 passed /  11 skipped
```

### 本轮明确未做

- **仍然没有组装点构造 `PostgresCheckpointSaver`。** adapter 的默认值依旧是
  `InMemorySaver`（含义现在是明确的："只在本进程内有效"），要跨进程的调用方自己传一个
  持久 saver 进来。真正的装配点是 WP06-07 的 Task Worker；
- `adelete_thread` 与 checkpoint 保留策略仍未做；
- 两个进程同时对一个 thread 调 `run` 只被"检查"挡住，不被"锁"挡住。

## 2026-07-28 PR-050（二）saver 本身：跨进程恢复第一次有了证据

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。对应 WP06-06。
`PostgresCheckpointSaver` 实现 `BaseCheckpointSaver` 的四个异步方法，写进
[PR-050（一）](#2026-07-28-pr-050一checkpointer-的表结构)落的三张表。

### 落在哪一层

文件是 `adapters/langgraph/checkpointer.py`，不是 `adapters/persistence/`。表可以住在
persistence 里——它们只是 SQL，而且必须和其它表共用一份 metadata，Alembic 漂移测试才
看得见它们；但 saver 本身**依赖 langgraph**，放进 persistence 就等于让持久化层依赖一个
工作流框架。`adapters/langgraph/` 仍然是"唯一允许 import langgraph 的包"。

### 三个值得记下来的决定

**一次事务写完 checkpoint 和它的 blob。** blob 只写了一半的 checkpoint 会恢复出一个
少了通道的状态，而且恰好在恢复的时候恢复出来——那是最没人盯着的时刻。

**`aput_writes` 有两条冲突规则，不是一条。** 普通写入**先到为准**：一个 task 重试时，
它先前已经落库的写入不能被替换，否则恢复出来的那一步会看到同一份工作的第二个结果。
error / interrupt / resume / scheduled 这些**负数槽位**相反，**后到为准**：最新的那个
才是该 task 的当前状态。实现上按 `idx` 正负拆成两批，分别
`ON CONFLICT DO NOTHING` 与 `DO UPDATE`。

**`get_next_version` 带随机后缀。** 基类默认发整数版本号。那样两个进程写同一个 thread
会为同一通道都算出 `n+1`，把不同的字节写进**同一个 blob 主键**，一方静默覆盖另一方。
随机后缀让它们的 key 不同；32 位零填充的计数器让 `>`——pregel 循环用它判断哪些节点
已经见过某个通道——仍然按数值排序。

同步的一半（`get_tuple` / `list` / `put` / `put_writes` / `delete_thread`）**显式拒绝**并
说明原因，而不是继承基类那个不带消息的 `NotImplementedError`：这里的同步入口只能自己
起事件循环，而本项目每个调用者都已经在一个循环里了。

`alist` **先读完再逐个 yield**。异步生成器如果跨 yield 持有连接，调用方消费多久就占用
多久，提前 break 还会直接泄漏一条。代价写在注释里：不带 `limit` 的列举会把一个 thread
的历史读进内存——上界是该 thread 跑过的步数。组装用三条查询（checkpoint、blob、write
各一条），不是 2n+1 条。

### 一个被实测纠正的预期

LangGraph 自己的序列化把**元组还原成列表**（`('a','b')` → `['a','b']`，嵌套的也一样）。
`InMemorySaver` 同样如此，所以这不是本实现的问题，而是契约的格式。`TaskState` 在
`model_validate` 时把它们收回元组，因此恢复后的运行察觉不到——测试断言的是**恢复出的
领域状态相等**，而不是逐个字段形状相等。第一版测试按形状比较，直接失败，这条记录的是
纠正后的理解。

### 有牙验证：13 处破坏，其中 1 处暴露了测试的漏洞

每次只改实现里的一件事，跑整份测试文件，还原后重新确认全绿：

| 破坏 | 被抓住的测试 |
|---|---|
| `aget_tuple` 取最旧而不是最新 | 3 条 |
| `aget_tuple` 忽略 namespace | 1 条 |
| 普通写入改成覆盖 | 1 条 |
| 特殊写入改成丢弃 | 1 条 |
| `empty` 通道被当成值还原 | 9 条 |
| `alist` 忽略 `before` / `filter` / `limit` / thread | 各 1 条 |
| 版本号不带随机后缀 | 1 条 |
| 同步一半悄悄能用 | 5 条 |
| 从不加载 pending writes | **第一轮：0 条** |
| 不记录 parent checkpoint | 1 条 |

倒数第二行是重点：**第一轮破坏验证发现"pending writes 根本不加载"没有任何测试会失败**。
那不是无关紧要的——`research_internal` 与 `research_external` 在同一步并行，若一个成功
一个崩溃，幸存者的结果已经记在该步的 checkpoint 上、但这一步没有完成；恢复时**只有失败
的那一支可以重跑**，重放幸存者会把它的预算算两遍、把它调用过的东西再调一次。补上这条
测试后 13 处破坏全部被抓住，0 处漏网。

### 跨进程恢复的证据

关键的一条测试：第一次运行在 `critic` 里抛异常；然后 handler、编译后的图、saver、engine、
连接池**全部丢弃**，只留下 `thread_id`；第二个"进程"从零重建，`ainvoke(None, config)`
接着跑完。断言是——第二次运行里 `understand` 与 `research_internal` 的调用次数为 **0**，
`critic` 为 1，最终状态带着**上一个进程采集的证据**。

配套的对照组用 `InMemorySaver` 做同一件事，证明它**做不到**——否则"从 checkpoint 恢复"
和"从输入把整张图重跑一遍"在断言里长得一模一样。

另有一组差分测试：同一张图、同一份输入，分别跑在 `InMemorySaver` 和本 saver 上，要求
两者的最终状态与整段 checkpoint 历史一致。参考实现比"作者自己写下的期望"更适合当 oracle。

本轮门禁：

```text
ruff format --check .    passed（218 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
alembic 唯一 head        0010_workflow_checkpoints

pytest（无外部服务）              913 passed / 286 skipped
pytest（真实 PostgreSQL + Qdrant） 1188 passed /  11 skipped
```

新增 21 条测试，其中 6 条（同步拒绝、版本号）不需要数据库。

### 本轮明确未做

- **没有任何组装点构造它。** `LangGraphTaskWorkflow` 仍然默认 `InMemorySaver`，
  它的 `resume` 也仍然查**进程内存**里的 thread → graph version 映射，所以
  **端口层**的"重启后用原 `thread_id` 续跑"还没通——通的是 checkpointer 层。
  把两者接起来要先决定 graph version 存在哪里，那是下一次变化
  （已由上面的
  [PR-050（三）](#2026-07-28-pr-050三workflow-身份进-checkpoint端口层跨进程续跑)完成：
  版本进 checkpoint metadata）；
- `adelete_thread` 未实现（基类的 `NotImplementedError` 原样保留），checkpoint 保留与
  清理策略同样还没有；
- 并发写同一 thread 只靠随机版本号避免 blob 主键互相覆盖，**没有**跨进程的互斥；
  真正的互斥是 WP08 的 lease 与 guard 连接。

## 2026-07-28 PR-050（一）checkpointer 的表结构

状态：**在分支 `pr-050-postgres-checkpointer` 上，尚未合入 `main`**。对应 WP06-06 的第一步。
[ADR-014](./adr/0014-own-postgres-checkpointer.md) 决定自研 saver，本轮只落**它要写进去的表**，
saver 本身一行都还没写。

### 三张表不是设计选择，是契约的形状

`aput` 只收到 `new_versions`——本次真正变化的通道——所以通道值必须存在一张**按版本
索引**的表里，而不是每个 checkpoint 复制一份；`aput_writes` 记录的是"某个 task 已经
产出、但消费它的那一步还没 checkpoint"的中间结果，所以它需要自己的表。

LangGraph 序列化出来的东西**在这里保持不透明**：`dumps_typed` 返回 `(type, bytes)`，
`loads_typed` 收回同一个二元组，因此两半都存、都不解释。把它拆成"可读"的列，等于本
项目声称自己理解一个并不属于它的格式，而理解错的代价恰好在恢复 checkpoint 时兑现。

表名带 `workflow_` 前缀：生态里不带前缀的 `checkpoints` 正是官方 saver 用
`CREATE TABLE IF NOT EXISTS` 建的表，列结构与这里不同，不该被它悄悄认领。

### 实测：**不能**给 writes 加指向 checkpoints 的外键

这是本轮唯一一个"看起来显然该做、实测证明会坏事"的决定。LangGraph 默认
`durability="async"`，**不等 checkpoint 落库就发出下一步的 writes**。把 `aput` 人为拖慢
50 ms 后测量：

```text
durability=async   11 次 writes 调用，11 次都在对应 checkpoint 提交之前开始
durability=sync    11 次 writes 调用， 1 次在对应 checkpoint 提交之前开始
```

三种 durability 模式、一个抛异常的节点（ERROR write 路径）和一次 resume 都测过。
所以外键在这里**不是约束不变量，而是让正常运行失败**。它的缺席由一条测试固定下来，
测试的文档说明了原因，免得后来者把它当成疏漏"补上"。

### 有牙验证

12 处等价破坏，逐个只改数据库里的一件事，然后跑对应测试；全部失败，还原后全绿：

| 破坏 | 失败测试 |
|---|---|
| 给 writes 加 checkpoints 外键 | 写入早于 checkpoint 的用例 |
| blob 主键去掉 `version` | 一通道多版本共存的用例 |
| blob 主键加宽到不再唯一 | 同通道同版本只存一次的用例 |
| checkpoint 主键去掉 `checkpoint_ns` | 子图命名空间隔离的用例 |
| writes 主键去掉 `idx` | 负数索引与普通写共存的用例 |
| `idx >= 0` 约束 | 同上 |
| `payload` 改成 text | msgpack 非 UTF-8 的用例 |
| `metadata` 改成 bytea | 按 key 查询 metadata 的用例 |
| 空 blob payload 被拒 | "写入了空值"与"从未写入"区分的用例 |
| `checkpoint_id` 收窄到 16 字符 | 真实运行全量落库的用例 |
| `task_path` 收窄到 8 字符 | 同上 |
| `parent_checkpoint_id` 设为 NOT NULL | 同上 |

最后一栏那三条来自同一个测试：它把 v1 图**真跑一遍**，把 LangGraph 实际要求存的每一行
（11 个 checkpoint、40 个 blob、32 条 write）原样写进表里再读回来，逐字节比对。它证明的
不是"某个手搓的行能存下"，而是"契约真正产生的东西能存下"。

本轮门禁：

```text
ruff format --check .    passed（216 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
agent-config-check       development / test / production 均为 status=ok
alembic 唯一 head        0010_workflow_checkpoints

pytest（无外部服务）              907 passed / 271 skipped
pytest（真实 PostgreSQL + Qdrant） 1167 passed /  11 skipped
```

新增 11 条测试全部需要真实 PostgreSQL，因此无外部服务那一列的 `passed` 不变、
`skipped` 从 260 涨到 271。

### 本轮明确未做

- **saver 本身**：`BaseCheckpointSaver` 的 `aput` / `aput_writes` / `aget_tuple` /
  `alist` 一个都没实现，`LangGraphTaskWorkflow` 仍默认 `InMemorySaver`，所以
  **进程重启不保留执行位置这一条至今没有变化**（这一条已由上面的
  [PR-050（二）](#2026-07-28-pr-050二saver-本身跨进程恢复第一次有了证据)在
  checkpointer 层解决）；
- **thread → graph version 的映射仍在进程内存里**。它没有进这三张表：按实施计划
  §6 它属于 `task_runs.graph_version`，而 ADR-014 写明 checkpointer 不得成为 Task
  产品状态的第二个 writer。它随 WP06-07 的 `task_runs` 落库；
- checkpoint 的保留与清理策略。三张表都有 `created_at`（blob 只能靠它——它只能经由
  checkpoint 不透明载荷里的 `channel_versions` 到达，SQL 无法把它 join 回来），但还
  没有任何东西会删除它们。

## 2026-07-28 PR-049 LangGraph Adapter 与 graph version 注册表

状态：**已合入 `main`（GitHub `#59` / `341cbf5`）**。对应 WP06-05。**本轮第一次引入 `langgraph` 依赖。**

Adapter 拥有编译、checkpoint 和调度；**不拥有任何路由决定**。每条边都来自
`workflows.research_graph`——所以"重放 checkpoint 的那张图"和"控制流测试断言的
那张图"是同一份声明。在这里重述一条路由规则，就是造出第二份定义，而它会静默漂移，
且只在恢复路径上暴露。

### 依赖决策：主依赖，不是 extra

`langgraph` 进 `[project.dependencies]` 而不是可选 extra。`embedding` extra 的存在
理由是**体积**（运行时加权重好几个 GB）；langgraph 是纯 Python 小包，放进 extra
只能换来"CI 跳过 workflow adapter 的测试"，也就是对它什么都证明不了。

许可证门禁需要补两个字符串，都来自传递依赖，且都宽松：

```text
MPL-2.0 AND (Apache-2.0 OR MIT)   ← orjson
Apache-2.0 OR MIT                 ← ormsgpack
```

`MPL-2.0` 本身早已在允许列表内，所以这是列表的**扩充**而不是策略放宽。用 CI 同款
`pip-licenses --allow-only` 命令对 140 个包的实际解析结果逐个验证过，新增 17 个包
（langchain-core、langgraph-checkpoint、langsmith、zstandard 等）全部通过。
`uv lock --check --offline` 通过，uv 版本与 CI 固定的 `0.11.31` 一致。

### 状态即通道，且不能悄悄少一个

`TaskState` 以普通 mapping 进入图，字段就是图的通道。两个引用通道挂
**与控制流同一个排序并集 reducer**，因此 LangGraph 自己的 fan-in 与
`workflows.fan_in` 产生相同的合并结果。

`TaskState` 新增字段而这里没加通道，会在**第一次 checkpoint round-trip 时被丢掉**。
所以有一条测试直接断言两个字段集合相等，而不是指望它们自觉保持同步——破坏验证里
删掉一个通道会让 3 条测试失败。

### 预算耗尽走 `END`，不走 approval

`route_quality_gate` 返回 `None` 时，adapter 映射到 LangGraph 的 `END`。这是一个
**LangGraph 认识而领域不认识**的节点名，正是要点：图必须停下，而 `TaskNodeId`
不该为了描述"停下"而多出一个成员。

`resume` **不传初始状态**：状态已经属于 checkpoint，再传一次正是"崩溃后把原始输入
追加两遍"的来源。测试用计数 handler 证明 `understand` 只跑过一次。

graph version 注册表按版本编译并缓存。未注册的版本**失败关闭**，不回退到最新图——
checkpoint 记录了它由哪个版本写入，猜错的代价恰好在最贵的时候产生。

### 有牙验证

六处等价改动，还原后逐字节一致：

| 破坏 | 失败测试数 |
|---|---|
| 预算耗尽路由到 `approval` | 1 |
| `resume` 重新提交初始状态 | 2 |
| `run` 不拒绝已存在的 thread | 1 |
| 通道 reducer 改成覆盖 | 1 |
| `resume` 忽略 graph version 不匹配 | 1 |
| `GraphState` 少一个通道 | 3 |

本轮全仓门禁：`907 passed / 260 skipped`（新增 12）；Ruff、Pyright 全过。
langgraph 无 type stub，按包内既有的 FlagEmbedding / sentence-transformers 同款做法，
用窄 `pyright: ignore` 加 `cast` 收窄，而不是为整个包放宽类型检查。

### 本轮明确未做

- **PostgreSQL checkpointer（WP06-06）**：当前用 `InMemorySaver`，因此
  **进程重启不保留执行位置**，"从 checkpoint 恢复"尚无证据；
- Task Worker / TaskService / Task 查询接口（WP06-07）、事件时间线（WP06-09）；
- `plan`/`critic` 的结构化输出解码；`approval`/`export` 仍是无副作用占位；
- thread → graph version 的映射目前在**进程内存**里，随 WP06-06 的持久
  checkpointer 一并落库。

## 2026-07-28 PR-048 Agent node：只经 `AgentExecutor`，且失败也要记账

状态：**已合入 `main`（GitHub `#58` / `0830e55`）**。对应 WP06-03。

节点通过 `AgentExecutor` 到达模型，除此之外没有别的路径——不持有 tool registry、
不持有 model port、不持有 Runtime 内部对象。**一个能自己组装循环的节点，就是第二个
Runtime，而且不带第一个的预算、取消和 Policy 保证。** 测试直接断言节点实例只持有
一个 `_executor` 字段。

本轮只做**产物是一个 artifact 的节点**：`understand / research_internal /
research_external / synthesize`。它们的状态贡献就是 `AgentOutcome.output_ref`
已经携带的引用。`plan` 与 `critic` 需要把模型输出**解码成结构化值**（TaskStep 列表、
pass/revise 决定），那需要一份解码契约，所以刻意不在本模块里。
`ARTIFACT_PRODUCING_NODES` 在调用时校验，因此将来给某个节点加上结构化产物时，
它不能继续走 artifact 那条路而把那个产物悄悄丢掉。

### 失败的运行也要记账

`AgentExecutor` 的契约是"预期失败返回终态 outcome，而不是抛异常——调用方是一个
必须记录并路由的 graph node"。所以节点**先把 run id 和 usage 折进状态，再判断成败**：

```text
run → 记录 agent_run_id + usage → 判断是否可用 → 可用则返回，不可用则带着已记账的状态抛出
```

`AgentNodeFailedError` 携带那份**已经扣过费的状态**。只记录成功的节点，会让同一个
Task 在一个"看起来从没动过"的预算里无限重试。对照组就是同一节点、同一次调用，
唯一差别是 outcome 失败。

### 完成但没有产物，是失败而不是空成功

这四个节点的全部工作就是产出那个 artifact。`completed` 但 `output_ref is None`
被判为失败——否则图会继续往下走，把一个背后什么都没有的目标交给下一个节点。
`cancelled` 同理：即使 outcome 上挂着 artifact 引用也不采纳，因为它背后的工作是被
中途停掉的。

### 提示词是投影，不是转录

节点只发送 objective 和 plan，**不发送先前节点累积的输出**。那些东西在 artifact
store 里；把它们抄进 prompt，会让上下文随图增长而不是随问题增长。测试用
evidence/outcome 引用做 canary 断言它们不出现在 prompt 里。

`TaskRunContext`（trace/stream/principal/envelope/budget）**刻意不进 `TaskState`**：
一个携带 principal 和授权信封的 checkpoint，会让 resume 重放 Task 启动时的授权，
而不是重新推导它。由 Registry 在 claim 时提供。

### 有牙验证

六处等价改动，每处都**只**让对应的那一条测试失败，还原后逐字节一致：

| 破坏 | 失败测试数 |
|---|---|
| 只在成功时记账 | 1 |
| 接受"完成但无产物" | 1 |
| 把 cancelled 当作内容 | 1 |
| prompt 回放累积引用 | 1 |
| usage 覆盖而不是累加 | 1 |
| `absorb_draft` 保留陈旧 review | 1 |

本轮全仓门禁：`895 passed / 260 skipped`（新增 13）；Ruff、Pyright 全过，跳过项不变。

### 本轮明确未做

- `plan` 与 `critic` 两个节点（需要结构化输出解码契约）；
- `approval` / `export` 占位节点；
- LangGraph adapter、checkpointer、Task Worker、事件时间线。**仍然没有任何 Task
  可以端到端运行**；本轮只是把"节点如何与模型交互、如何记账"这一层钉死。

顺带把 `research_graph._evolve` 提升为公开的 `evolve`：重新校验这条规则原本要在
两个模块里各写一遍，而重复的规则就是会漂移的规则。

## 2026-07-28 PR-047 固定研究图的确定性控制流与 fan-in reducer

状态：**已合入 `main`（GitHub `#60` / `4ec04d2`）**。
对应 WP06-02（条件路由）与 WP06-04（确定性 fan-out/fan-in reducer）。

新增 `src/agent_workbench/workflows/`——框架无关的核心包，架构守卫已实际覆盖它
（把 `import langgraph` 放进去会让 `test_core_keeps_frameworks_and_concrete_sdks_at_outer_boundaries`
失败，已实测）。**边写成数据、路由写成纯函数**，因为一个嵌在已编译图里的路由决定，
只能靠运行那张图来测试。LangGraph adapter 将来编译这份声明，而不是持有同一决定的
第二份措辞。

两条对恢复起支撑作用的性质放在这一层而不是 adapter 里：

- **fan-in 是排序并集**。合并结果与哪个 research 分支先完成无关，重复合并同一份
  contribution 也不改变结果。于是 fan-in 中途崩溃后的 checkpoint 会**收敛**，而不是
  累积重复引用。测试直接断言交换律、幂等性，以及"重放一个分支等于没重放"；
- **revision 预算耗尽的 quality gate 不返回下一个节点**。它返回 `None`，而不是
  `approval`：把耗尽路由到审批，等于**批准一份 critic 明确否决的草稿**，恰好在质量门
  最该起作用的时候把它变成形式。调用方必须显式处理这个值，不能靠忽略返回值走到审批。

三条 quality gate 条件边各有测试，且**互为对照组**——`pass → approval`、
`revise 且有预算 → synthesize`、`revise 且预算耗尽 → 无后继`，后两条唯一的差别就是预算。

`begin_revision` 在推进计数的同时**丢弃旧 review**：`TaskState` 要求存储的 review 描述
当前 `revision_count`，保留它要么校验失败，要么更糟——让下一次质量门读到一份关于
**已被重写的草稿**的陈旧结论。

### 一个本轮修掉的真实缺陷

reducer 最初用 `model_copy(update=...)` 构造新状态。**Pydantic 的 `model_copy` 跳过
校验器**，而 `DomainModel` 没有开 `revalidate_instances`，所以一个返回乱序或重复引用
的 reducer 会被**原样存进 checkpoint**，直到以后某次读取才失败。改为经
`model_validate` 重新校验的 `_evolve`。

这一条的守卫只有在 reducer 真的回归时才起作用，所以测试**注入**那次回归
（monkeypatch 掉 `merge_refs` 让它返回乱序），证明 `fan_in` 拒绝而不是落盘。
修复前没有任何测试能发现这个问题——sabotage 4 只让这一条失败，正说明它此前是裸的。

### 有牙验证

五处等价改动，每处都让**对应**测试失败，还原后逐字节一致、23 条全过：

| 破坏 | 失败测试数 |
|---|---|
| 预算耗尽改为路由到 `approval` | 2 |
| `merge_refs` 保留插入顺序（不排序） | 4 |
| `begin_revision` 保留陈旧 review | 2 |
| `_evolve` 改回 `model_copy` | 1 |
| `route` 忽略空 plan | 1 |

本轮全仓门禁：`882 passed / 260 skipped`（新增 23 条），Ruff format/check、Pyright
`0 errors / 0 warnings` 全部通过。跳过项与 `main@e93d7a1` 完全相同，本增量不触碰
任何外部依赖。

### 本轮明确未做

- **WP06-03 Agent node**：`understand / plan / research_* / synthesize / critic`
  的处理函数尚不存在，本轮只有它们之间的控制流；
- **WP06-05 LangGraph Adapter**：`langgraph` 仍不是项目依赖，`adapters/langgraph/`
  不存在；
- **WP06-06 PostgreSQL checkpointer**、**WP06-07 Task Worker / TaskService /
  Task 查询接口**、**WP06-09 Task 事件时间线**；
- 因此**没有任何 Task 可以端到端运行**，也没有 checkpoint 恢复证据。本节只证明
  "下一个节点是谁"和"并行结果如何合并"这两件事是确定的、有界的、可测的。

## 2026-07-28 PR-046 Sparse 加载守卫：拒绝没有 lexical head 的编码器

状态：**已合入 `main`（GitHub `#55` / `e93d7a1`）**。

BGE-M3 训练好的 lexical 投影放在 `sparse_linear.pt` 里，和基础权重**并排**而不在其中。
FlagEmbedding **不把这个文件的缺失当作错误**：它当场构造一个新的 `Linear(1024, 1)`
然后继续。于是下游每一道检查都通过、又都毫无意义——模型加载成功；输出宽度确实是
250002，但那个宽度来自 tokenizer 词表，**不来自这个投影**；编码器返回结构完好的
sparse 向量。**只有数字是错的，而且是随机地错**，随机不触发任何告警。

后果是：两个进程加载同一个固定 revision，拿到的是不同的权重。这让 `index_identity`
变成**贴在一个随机变量上的标签**——一个进程写入的 point，被另一个进程的向量空间检索。
本项目此前发布的每一个 hybrid 数字，都只是某个无关分布的一次采样。代价是两个被撤回
的诊断（"短查询编码为空"不是错的，是**随机的**），以及一次结论会反转的消融。

**修复是拒绝，不是下载。** 把文件取到本地只修好一台机器；拒绝构造一个投影不在缓存里
的编码器修好所有机器，而且错误信息里带着取回它的命令。

两条守卫，改动前都失败：

- 同一个固定 revision 的同一查询，在两次加载之间必须编码为**同一个向量**——这正是
  `index_identity` 赖以成立的契约；
- 直接命名成因：一个**新构造**的 `Linear` 不可能两次产生相同的数字。症状测不出这一条。

`evals/rag/reports/ABLATION.md` 与 `SPARSE-DIAGNOSIS.md` 已按加载修复后的重跑结果
订正，`dense.json` / `hybrid.json` 同步更新，`hybrid-run1.json` 保留作对比。选择
FlagEmbedding 的背景决策见 [ADR-013](./adr/0013-bge-m3-sparse-encoder.md)（PR-026
留下）；本轮补的是那个决策**没有覆盖到的加载缺口**。

附带修正：投影守卫的 `huggingface_hub` 导入对类型检查器屏蔽并经 `cast` 收窄。该库
随 `embedding` extra 才安装，本机装了它、CI 没装，所以本地 pyright 通过而 CI 是唯一
能看见这个错误的地方——处理方式与包内既有的 FlagEmbedding / sentence-transformers
一致，不让一个可选依赖变成跑门禁的必需品。

## 2026-07-28 PR-045 Reranker：重排已授权候选，fail-open 且不扩大范围

状态：**已合入 `main`（GitHub `#54` / `3b7829b`）**。对应 WP05-03 / WP05-04。

cross-encoder 同时读 query 和 passage，而检索器做不到：dense 与 sparse 都在**问题存在
之前**就把 passage 编码完了。这值得在几十个候选上多花一个数量级的算力，也正是这一步
需要 timeout 的原因。

**Port 返回每个 passage 一个分数（按位置对应），而不是一个重排好的列表。** 一个返回
passage 的 adapter 可以少还一个、重复一个、或者还回一个从没给过它的东西，而调用方
分不清那是 bug 还是排序结果。分数让契约可以**按长度**校验，并把重排留给
**知道 PostgreSQL 授权了什么**的那一层。于是"reranker 不可能引入一段提问者无权读的
passage"**由构造成立，而不是靠信任**。

位置固定在**授权之后、`top_k` 之前**：

- 更早，cross-encoder 会读到提问者无权读的文本；
- 更晚，它只能在检索器已经切好的名单里做提升，永远推翻不了检索器的选择——而那不是
  消融要比较的东西。

**fail-open 且窄。** timeout、异常、分数条数对不上，全都回退到**已授权的原顺序**：
一个质量步骤不该把一个能用的回答变成一个错误。`CancelledError` **不捕获**——它继承
`BaseException`，一个被取消的请求必须保持取消。回退目标就是输入列表本身，因此
**没有任何一条路径会扩大已授权范围**。

**缺 reranker 不损失 Chat 能力**，这和缺 embedder 不同：一个让回答变差，另一个让人
根本无法构造查询。但由于**在端点上，未重排的进程和重排过的进程无法区分**，
`ApiDependencies` 记录 `reranker_unavailable` 原因；否则一份针对静默未重排部署写出来
的消融报告，会把差异记在模型头上。`RetrievalResult.reranked` 出于同一理由存在：它
区分"真的重排过"与"fail-open 回退"，否则一次超时会被读成"rerank 没有差别"这种被
制造出来的零结果。

**未做**：三臂消融的 `hybrid-rerank` 臂仍未跑。hybrid 在当前 38 题 gold set 上已打满
1.000，**rerank delta 在这里必然为 0**；要测出 rerank，先得有更难的 gold set，而不是
先跑第三臂。详见 `evals/rag/reports/ABLATION.md`。

## 2026-07-28 PR-044 Task 工作流的 checkpoint-safe 状态

状态：**已合入 `main`（GitHub `#53` / `3538e26`）**。这是 WP06 的第一块，**只有领域
状态与 Port**：LangGraph adapter、节点 handler、checkpointer 和 Task Worker 都还不存在。

图的 checkpointer 被允许持久化这份状态，所以这个模块是围绕**什么绝不能进来**写的：

```text
执行位置      → checkpointer
产品状态      → Task Registry
大输入/大输出 → 各自的 store
```

每个字段都小且 JSON 可序列化。这份状态不允许长出 `current_step`、消息记录、文档正文
或任何 provider/框架对象。

**校验器拒绝，而不是规范化。** 未排序的 `depends_on` 是一个**错误**，而不是在这里被
悄悄排好——建立规范顺序的是并行 fan-in 的 reducer，在入口处排序会**掩盖一个已经不再
做这件事的 reducer**。plan 依赖只能指向排在它之前的 step，于是一个给定 DAG 只有一种
拓扑表示，一个 checkpoint 也就只重放到一个状态、而不是若干个。同时禁止自依赖和重复
依赖。

`ports/task_workflow.py` 把 `thread_id` 与 `graph_version` 显式放在**每一次操作**上，
adapter 不能拿一份不同的图定义去静默恢复一个 checkpoint。`resume` **刻意不接受初始
`TaskState`**：状态已经属于 `thread_id` 指向的那个 checkpoint，再接受一次就使"崩溃后
把原始输入追加两遍"重新成为可能。

canonical v1 节点 ID 固定为 `understand / plan / route / research_internal /
research_external / synthesize / critic / quality_gate / approval / export`，与架构
基线、checkpoint metadata、事件和测试一致；重命名任一节点都必须提升 `graph_version`。

golden 载荷集新增 `TaskState`：原有 13 个聚合**逐字节未变**，重新生成的文件只增 52 行、
不删任何行。

## 2026-07-28 PR-043 Chat lease 过期原子终态

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；静态检查与本地可运行门禁通过**。

PR-041 让硬崩溃遗留的 `running` Turn 最终失败，但当时 reaper、claim 和迟到
prepare 都可能写过期终态，Turn failure 与 durable Event 也不在同一个原子边界。
PR-043 把 PostgreSQL 的过期语义收敛到唯一 writer：

```text
ChatExpirationCoordinator.expire_due
  → SELECT oldest due running FOR UPDATE SKIP LOCKED
  → failed(deadline, stale_execution) + clear lease
  → ChatTurnExpired
  → COMMIT one Turn
```

实现不变量：

- PostgreSQL 中只有 `ChatExpirationCoordinator` 可以持久化
  `stale_execution` 过期终态；普通 failure/cancellation writer、prepare 和 cleanup
  都先锁 session/Turn，再用数据库时钟复核 lease；
- claim 只创建或读取 Turn，不机会式回收。协调器提交前，同 key 仍观察原
  `running`，新 key 仍得到 busy，不能先解锁 session、后补 Event；
- 迟到的 `prepare_release`、`finish_failed` 和
  `finish_running_if_current` 不写 candidate、failure、assistant 或 Event；它们在锁内
  发现仍为 `running` 但 lease 已到期时抛 `ChatTurnLeaseExpiredError`，协调器已经提交
  时只观察既有终态；调用方也不能把构造的 `stale_execution` outcome 送进普通
  cleanup 绕过协调器；
- 每个候选使用一个独立 PostgreSQL 事务。更新 Turn 后写 Event 失败，或 Event 写完后
  出错，都会一起回滚；毒化候选保持 `running`，跨轮稳定扫描游标保证
  `batch_size=1` 时后续 due Turn 仍可处理；
- answer release 与 expiry 共用 SHA-256 派生的有界终态键
  `chat-turn:{sha256(turn_id)}:terminal`。同一 Turn 的 answer 与 expiry 不能各占一个
  幂等键；已存在的相同 `ChatTurnExpired` 可收敛，冲突 payload 则使该候选回滚；
- `ChatTurnExpired` 只观察 Chat Turn 的发布生命周期。Runtime 可能已经有
  `RunCompleted`；此时仍可随后出现 `ChatTurnExpired`，但不能伪造第二个
  `RunFailed`，事件也不携带候选答案、引用或 output reference；
- Memory coordinator 不宣称数据库耐久性或分布式原子性，但保持可观察失败回滚：
  Event append 失败时 Turn 仍是 `running`、session 仍 busy、Event 不可见；成功后
  Event 与 Turn 才在同一进程锁边界内一起可见。

确定性测试覆盖两个事务 fail seam、`batch_size=1` 毒化候选隔离与跨轮推进、两个
reaper 竞争、prepare/reaper
竞态、claim 在原子提交前后的可见性、共享终态键以及 Runtime/Chat 两种终态事件的区分。
本地全仓门禁为 `800 passed / 257 skipped / 1 deselected`；唯一 deselect 是当前
受限沙箱禁止 `socket.bind()` 的 loopback 真实性测试。Ruff、Pyright、compileall、
Alembic 唯一 head 与 `git diff --check` 均通过。真实 PostgreSQL 并发用例因未配置
`AGENT_WORKBENCH_TEST_DSN` 而显式跳过，未被描述为已在本机执行。

## 2026-07-28 PR-042 Chat pending 发布无流量恢复

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；无外部服务门禁与静态检查通过。
真实 PostgreSQL 并发用例在该增量当时因未配置测试 DSN 而跳过**。

PR-041 关闭了 `running` 硬崩溃后永久 busy，但 `prepare_release` 已提交、
原子发布尚未开始的窗口仍需要原客户端用同一 key 重试。若客户端消失，
`release_pending` 会一直占用单会话 active Turn。本轮把该恢复改为与请求流量无关：

```text
短查询 list_release_pending
  → 取 session 的 tenant/owner scope
  → 重新进入 ChatReleaseCoordinator
  → 锁 session/Turn/引用文档/event stream
  → 重新校验当前 ACL + source_revision
  → AnswerCommitted | AnswerWithheld + assistant + terminal Turn 原子提交
```

实现要点：

- 新增 `PendingChatRelease` 端口模型和 `list_release_pending(limit)`；Memory/PostgreSQL
  共用同一契约，只返回 `release_pending`，按 `turn_id` 稳定排序并携带 session owner；
- PostgreSQL 列表查询使用短连接普通 MVCC `SELECT + JOIN`，不把扫描事务或行锁带进
  模型/发布阶段；多个恢复器可看到同一候选，最终由 Turn 锁、terminal state 和稳定
  `chat-turn:{sha256(turn_id)}:terminal` 幂等收敛；
- `ChatPendingReleaseRecovery` 隔离单候选失败，后续候选仍会尝试；失败项保留 pending，
  下一轮继续重新授权，绝不把旧候选降级为未经检查的发布；
- API lifespan 同时管理 running reaper 与 pending recovery；两者都只依赖
  PostgreSQL/EventLog，不依赖 embedding、Qdrant 或模型，因此 Chat 降级不可用时仍能
  清理先前遗留状态；
- 确定性测试模拟 `after_prepare_before_release`：不再发送原请求，后台恢复后仍只写
  一个 `AnswerCommitted`，并证明会话可 claim 新 key；另测单候选失败不终止同批后续项。

完整无外部服务门禁为 `771 passed / 242 skipped`；Ruff、Pyright、compileall 和
Alembic head 检查通过。真实 PostgreSQL 契约还覆盖 owner scope、limit/order，以及
普通 pending scan 不等待另一个 Worker 持有的 Turn 行锁。

本节当时保留的 execution expiry 多 writer 和裸 Turn failure 边界已由 PR-043
关闭：claim/late prepare/cleanup 不再写过期事实，独立协调器按 Turn 原子提交
`failed(stale_execution)` 与 `ChatTurnExpired`，且不伪装成 Runtime `RunFailed`。

## 2026-07-28 PR-041 Chat 固定执行 lease 与孤儿回收

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；无外部服务门禁、静态检查和迁移
head 检查通过。真实 PostgreSQL 并发用例在该增量当时因未配置测试 DSN 而跳过**。

同步 Chat 没有 checkpoint、attempt-aware event 或副作用恢复协议，因此硬崩溃后
“把同一个 Turn 交给另一个 Worker 重跑”并不安全。本轮选择更小且可证明的语义：

```text
claim
  → running + 固定 lease_until
  → release_pending | failed | cancelled

lease 到期
  → failed(deadline, stale_execution)
  ↛ running（禁止续租、转移和自动重跑）
```

实现要点：

- 迁移 `0009_chat_turn_lease` 增加 `lease_until`、`running ⇔ lease 非空` 数据库
  约束和过期扫描部分索引；升级时旧 `running` 行立即具备可回收截止时间；
- claim 使用 PostgreSQL `statement_timestamp()` 计算
  `api.request_timeout_seconds + chat.orphan_grace_seconds`，不信任应用机器时钟；
- PR-043 后 claim 不再机会式终态化：协调器提交前，同 key 保持 `running`，新 key
  保持 busy；
- `prepare_release` 在 Turn 行锁内复核数据库时间；过期时只抛
  `ChatTurnLeaseExpiredError`，不写候选、failure、Event 或 assistant；
- `ChatTurnReaper` 委托 `ChatExpirationCoordinator` 按 `(lease_until, turn_id)`
  使用 `FOR UPDATE SKIP LOCKED` 回收；Turn failure 与 `ChatTurnExpired` 在每 Turn
  独立事务中提交，多个 API 实例可以竞争而不重复处理；
- `finish_failed` 与 `finish_running_if_current` 同样在锁内复核 lease，只能清理
  尚未过期的 `running`，不能覆盖 prepared/terminal，也不能成为第二个 expiry writer；
- `asyncio.timeout` 现在真正消费 `api.request_timeout_seconds`；外层
  `CancelledError` 使用 shielded best-effort cleanup 后仍重新抛出；
- Chat 路由用 `CancellationSource` 传递原因，并直接取消实际 Chat task，断开不再等待
  retrieval 自己轮询；API lifespan 先停止 reaper、有界排空 cleanup，再关闭
  HTTP/Qdrant/PostgreSQL 依赖。

PR-041 当时的契约覆盖固定 lease、请求 deadline、外部 task 取消和 terminal-only
reaper；其中“不同 key 机会回收”和“迟到 prepare 直接写失败”的断言已由 PR-043
替换为：原子提交前同 key 保持 running、新 key busy、late prepare 无写入，以及
coordinator 提交后同 key 返回失败并允许新 key。PR-041 当时完整无外部服务门禁为
`763 passed / 237 skipped`，Alembic 唯一 head 为 `0009_chat_turn_lease`；这不是
PR-043 的当前门禁数字。

本节最初保留的 `release_pending` 无人重试边界已由 PR-042 关闭；execution lease
的多 writer 与非原子 Event 边界已由 PR-043 关闭。固定 Chat lease 仍刻意不提供
自动重放或故障转移。

`0009` 当前按停机迁移设计：部署必须先停止旧版本写入，再升级 schema，最后启动新版本。
它不是 expand/dual-write/contract 三阶段 migration，不支持新旧应用副本滚动共存。

## 2026-07-28 PR-040 Chat 原子授权发布栅栏

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；静态门禁与内存测试通过。真实
PostgreSQL/Qdrant 并发用例在该增量当时因未配置服务而跳过**。

PR-039 的稳定 `event_key` 能让重复发布返回同一个事件，但“先查 ACL、后写事件”的
两个事务之间仍存在 TOCTOU：复核成功后可以恰好发生撤权；进程也可能在
`prepare_release` 后退出，重试再把旧候选发布出去。本轮新增
`ChatReleaseCoordinator`，生产适配器把以下步骤放进一个 PostgreSQL 事务：

1. 固定按 `conversation session → chat turn → document_id 排序后的文档行
   → event stream` 加锁；
2. 在文档行锁内复核 tenant、deleted、精确 `source_revision` 与 owner/ACL；
3. 若任何来源变化，构造不含候选正文、ArtifactRef、citation 与 revision 的安全
   withheld 结果；
4. 写唯一 `AnswerCommitted/AnswerWithheld`、追加 assistant message、把 Turn 转为
   `committed/withheld`；
5. 一起提交或一起回滚。

所有合规的 Document/ACL writer 必须先锁同一 document row，再修改内容、revision
或 ACL；这是发布栅栏成立的写入协议，不能用绕过 repository 的 ACL-only SQL 破坏。
`ChatTurnResult` 现在持久化排序且唯一的 `authorized_revisions`，并强制其文档集合与
citations 一致，因此 `release_pending` 重试具备完整的再授权输入。

新增的 PostgreSQL/Qdrant 确定性竞态测试覆盖：

- prepare 后故障、撤权先提交、同 key 重试：只能产生 `AnswerWithheld`，API 对象、
  history、event 与 `chat_turns.result` 均不含候选正文；
- 发布先取得文档锁、撤权并发到达：通过 `pg_blocking_pids()` 证明撤权事务真实等待，
  发布提交后撤权才推进 revision，形成可解释的线性顺序。

稳定 `event_key` 继续作为重复调用的防御，但已不再承担跨两个提交恢复一致性的职责。
内存协调器仅保持同一可观察契约，不宣称提供跨进程授权栅栏。

## 2026-07-28 PR-039 幂等 Chat Turn 与发布恢复

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；本地确定性测试通过。真实
PostgreSQL/Qdrant 用例在该增量当时因未配置服务而跳过**。

本轮把“消息历史”与“一个请求执行到了哪里”分开建模。新增
`ChatTurnStore`、PostgreSQL `chat_turns` 表和迁移 `0008_chat_turns`，生命周期为：

```text
running
  → release_pending
  → committed | withheld

running
  → failed | cancelled
```

核心不变量：

- `POST /v1/chat/sessions/{session_id}/messages` 强制要求 `Idempotency-Key`；
- `(session_id, idempotency_key)` 唯一，同键不同 request hash 返回 409；
- `run_id` 全局唯一，并由 tenant/principal/session/key 稳定派生；
- PostgreSQL 会话行锁和 partial unique index 共同保证每个会话最多一个
  `running/release_pending` Turn；并发请求不会交错消息或重复计费；
- `claim_turn` 在同一事务内完成 ownership 校验、历史快照、user message 和 Turn
  创建；鉴权先于幂等查询，错误主体不能利用 key 探测 Turn；
- `release_pending` 只保存内部候选结果，**不写可见 assistant message**；
- `AnswerCommitted/AnswerWithheld` 使用稳定 `event_key`，重复调用不会重复事件；
- PR-040 已进一步把最终授权复核、答案事件、assistant 与 Turn 终态合并为同一个
  PostgreSQL 提交，取代本节最初的“先事件、后 Turn”两阶段实现；
- withheld Turn 的持久化 `AgentOutcome` 会清空候选正文、ArtifactRef 和 citation，
  被拒绝的模型输出不能藏在 retry ledger 中。

确定性测试覆盖同键重试、不同 hash 冲突、跨会话 key 作用域、全局 run 冲突、
并发 claim、状态转换幂等、所有权先验、发布后故障注入和秘密不落盘。API response
新增 `turn_id`，终态重试返回原 `turn_id/run_id/result`。

本轮仓库级无外部服务门禁为 `747 passed / 223 skipped`；Ruff format/check、
Pyright、compileall 全部通过，Alembic 唯一 head 为 `0008_chat_turns`。跳过项需要
PostgreSQL、Qdrant 或本地 BGE 权重；受沙箱禁止 `socket.bind()` 的单个真实性测试按
既有方式从本轮本地门禁排除。

本节最初记录的 `running` 永久 busy 风险已由 PR-041 的固定 lease、PR-043 的单一
expiry writer 与 terminal-only reaper 关闭。它刻意不提供自动故障转移；若未来允许
重新执行同一 Turn，必须同时加入 owner、epoch、heartbeat、全写入 fencing、
checkpoint 与 attempt-aware event。

## 2026-07-28 PR-038 durable Event 幂等发布

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；内存契约与静态门禁通过。真实
PostgreSQL 契约在该增量当时因未配置测试 DSN 而跳过**。

迁移 `0007_event_idempotency_key` 为 `events` 增加可空 `event_key`，并以
`(stream_id, event_key) WHERE event_key IS NOT NULL` 唯一索引建立流内幂等边界。
内存与 PostgreSQL EventLog 共用同一契约：

- 同一 stream、同一 key、同一 scope/payload/parent 返回原 envelope；
- 同键不同内容 fail closed，且不消耗 sequence；
- 并发八次 append 只产生一个 event，下一事件 sequence 连续；
- 同一 key 可在不同 stream 独立使用；
- transient event 禁止携带 key；
- `ScopedEventSink`、`ObservingEventSink` 和 `AnswerReleaseSink` 全链路透传 key。

EventLog 仍未提供历史 schema upcaster registry 与 poison-row 隔离/跳过策略。

## 2026-07-28 PR-037 EventLog 版本化回放元数据

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；本地静态门禁通过。真实 PostgreSQL
新用例在该增量当时因未配置测试 DSN 而跳过**。

`EventEnvelope` 一直声明 `schema_version` 与 producer timestamp，但 PostgreSQL
`events` 表此前没有保存版本，append 也没有写入 envelope timestamp。结果是：

- 旧 row 回放时会被默认套用当前 schema version，消费者无法知道它由哪个契约产生；
- append 返回的时间与 replay 返回的数据库落库时间不同，同一事件 round-trip 后发生
  变化。

本轮新增迁移 `0006_event_schema_version`，把已有 row 明确回填为 schema v1，随后移除
临时 server default，要求以后每次 append 显式写版本。`PostgresEventLog` 现在同时写入
`envelope.schema_version` 与 `envelope.timestamp`，read 时显式重建两者；未知版本会在
领域模型边界失败关闭。

相关测试覆盖：

- append 返回值、数据库列和 replay envelope 的 schema version 完全一致；
- 固定 producer clock 后，数据库与 replay 保留同一个时间；
- 将持久 row 人为改成未知版本时，replay 拒绝解析；
- 从 `0005` 升级时已有事件回填 v1，最终列为 `NOT NULL` 且没有残留 default。

本地相关门禁为 `30 passed / 22 skipped`；仓库级无外部服务门禁（排除受当前沙箱禁止
`socket.bind()` 的测试文件）为 `702 passed / 189 skipped`。Ruff format/check 与
Pyright 全部通过，Alembic 唯一 head 为 `0006_event_schema_version`。跳过项需要
PostgreSQL、Qdrant 或本地 BGE 权重。本轮尚未实现 schema upcaster registry、
poison-row 隔离/跳过策略；稳定幂等键已由后续 PR-038 补齐。

## 2026-07-28 PR-036 顺序多轮上下文

状态：**已合入 `main`（GitHub `#52` / `07f4a27`）；无外部依赖测试通过**。

此前 ConversationStore 会保存消息，但每次模型调用只收到当前问题与当前
`ContextPacket`，所以“有历史记录”并不等于“模型能多轮对话”。本轮把调用语义改为：

```text
读取并验证 session ownership
→ 取得已经提交的 conversation history 快照
→ 为当前问题执行一次新的授权检索
→ 历史原始消息 + 当前 evidence prompt 交给 Runtime
→ 最终授权后提交本轮 assistant 消息
```

只回放 ConversationStore 中已经存在的原始用户问题与最终 assistant 消息。上一轮 RAG
passage 不进入历史，未通过发布门的候选答案也不会进入历史；因此后续轮次不会从内部
prompt 重新带入旧 source revision 的原文。撤权路径只会回放安全拒答。

本轮确定性测试证明：

- 第二轮模型请求按 `user → assistant → user` 顺序收到第一轮已提交消息；
- 当前问题只出现一次，且只有当前问题附带当前检索 evidence；
- 撤权时生成过的候选秘密不会进入下一轮模型请求，只有安全拒答会进入。

本节落地时的边界是**顺序调用下的多轮上下文**；后续 PR-039 已补上
`chat_turns` 事实表、并发非交错和请求/发布幂等恢复。历史 token window/compaction
与模型实际使用 Citation 的结构化校验仍未完成。

## 2026-07-28 PR-035 安全发布基线

状态：**已合入 `main`（GitHub `#51` / `7025425`）；本地确定性测试通过**。

本轮关闭了 2026-07-27 复核报告中的两条直接读取风险：

1. **答案发布门**：固定 2-step Chat 不再把未完成最终 ACL 复核的模型正文写入
   durable `ModelCompleted` 或 live `ModelDelta`。Runtime 事件仍保留调用、用量和终态，
   但答案正文只在复核成功后进入 `AnswerCommitted`；撤权时只写安全的
   `AnswerWithheld`。
2. **source revision 读取栅栏**：Qdrant 候选除当前可读外，还必须满足
   `candidate.source_revision == PostgreSQL 当前 source_revision`。旧 revision 和不可能的
   future revision 都不能进入 `ContextPacket`。

同时修正了三项相邻的 Chat 语义：

- failed/cancelled AgentOutcome 不再保存空 assistant 消息或返回 HTTP 200；API 分别返回
  结构化的 502/504/409 终态；
- Route、`AgentRunRequest`、EventScope、AgentOutcome 和 API response 共用同一个
  `run_id`，Chat stream 固定为 session ID；
- session ownership 在 embedding/Qdrant 检索前验证，猜测 session ID 不能触发昂贵检索。

确定性测试覆盖：

```text
正常路径：ModelCompleted 正文为空
        → ACL/evidence confirm
        → Chat Turn 写入内部 release_pending（History 不可见）
        → AnswerCommitted 含最终答案
        → Turn 转 committed 并追加可见 assistant

撤权路径：模型已生成秘密文本
        → confirm_unchanged 失败
        → HTTP 只返回拒答
        → History / EventLog / SSE 均不含秘密
        → AnswerWithheld 只含安全替代文本

失败路径：RunFailed/RunCancelled
        → History 只保留 user message
        → 无 AnswerCommitted/AnswerWithheld
        → API 非 200
```

2026-07-28 本地质量门：

```text
ruff format --check .    passed（178 files）
ruff check .             passed
pyright                  0 errors / 0 warnings
pytest                   711 passed / 188 skipped / 1 environment failure
```

唯一失败仍为受限沙箱禁止 `socket.bind()` 的 loopback 真实性测试；与本轮代码无关。
外部 PostgreSQL、Qdrant 和真实 BGE 权重未配置的测试按契约跳过。

本轮**没有**宣称完成：

- Qdrant 旧 Point 物理 replace/delete；当前已做到“不可读取”，尚未做到“已清理”；
- Ingestion Worker 的 advisory lock、heartbeat、retry/dead-letter 和常驻进程；
- Agentic `knowledge_search` 的 run-scoped evidence ledger 与最终提交门；
- 历史 token window/compaction、模型实际引用校验；幂等 `chat_turns` 和并发
  Turn 非交错已由后续 PR-039 补齐；
- LangGraph Task、Task Registry、Multi-Agent、CrewAI benchmark。

## 2026-07-25 仓库核验总览

核验基线：`main@f071323`，PR-001～PR-015 与 ADR-012 已合并。当前配置登记
231 个 Settings 叶子字段、47 个组。

已经实现并有测试证据：

- Domain、Ports、Fake Adapter、可复现 CLI；
- 自研 Runtime 的串行 Tool Loop、Policy/Tool Gateway、预算与取消、并行只读、
  exclusive 屏障和 Hook Bus；
- DeepSeek 流式 HTTP Adapter 的离线 contract tests；
- PostgreSQL ConversationStore、Document/Version/ACL、事务 Outbox；
- Local ArtifactStore 与 Upload / Artifact / Health API。

明确尚未实现：

- Chat RAG、LlamaIndex、Qdrant、BGE、RAGAS；
- LangGraph Task、Task Registry、lease/fencing/checkpoint、Multi-Agent；
- 生产身份认证、S3、Worker、SSE、UI、Docker Compose；
- DeepSeek 的进程级装配和真实服务 E2E。

本次门禁共收集 544 项测试。无数据库环境为 499 passed、45 skipped；PostgreSQL
16 集成套件为 160 passed，Alembic `0001 → 0002` 通过。两组测试有重叠，不能
相加。Ruff、Pyright、三 profile、CLI golden、许可证、Gitleaks 与 Actionlint
均通过；GitHub Actions 证据见
[run 30184299195](https://github.com/he-zi-qiang/agent-workbench/actions/runs/30184299195)。

当前阻断项：

1. ~~默认 `api.host = "0.0.0.0"`，开发 Header Identity Resolver 可能被本机
   之外的调用者访问并伪造身份~~（2026-07-26 已修复，见下方 P0-1 一节）；
2. ~~`requires_approval=True` 没有阻止 write tool 执行~~（2026-07-26 已修复，
   见下方 P0-2 一节）；
3. ~~Upload/Document/Artifact 缺少同 tenant 内的 owner/ACL 对象授权~~
   （2026-07-26 全部修复，见下方 P1-1、P1-3、P1-2 三节）；
4. ~~tool/token/cost budget 不是硬上限~~（2026-07-26 已修复，见下方 P1-5 一节）；
5. ~~Policy 改写可绕过参数字节上限，Policy/Hook deadline 不完整~~
   （2026-07-26 已修复，见下方 P1-6 / P1-7 一节）；
6. ~~DeepSeek 对损坏 SSE frame fail open，Artifact 下载不是真正流式~~
   （2026-07-26 均已修复，见下方 P1-9 / P1-4 两节）；
7. ~~Outbox claim 没有 lease/fence，worker 崩溃后不可恢复~~
   （2026-07-26 已修复，见下方 P1-10 一节）。

**七条阻断项已全部关闭**，核验报告的 14 条缺陷同样全部修复并附回归测试。每一条的
触发条件、判断依据、有牙验证结果与**明确留下的缺口**记在下面各自的小节里；报告的
§7 是同一份记录的另一半。

完整触发条件、文件位置和建议修复顺序见
[仓库核验报告](./archive/repository-audit-2026-07-25.md)。核验那一轮只校正文档，没有改
生产逻辑；此后的修复各自一个 PR，逐条记在下面，并同步回核验报告的第 7 节。
纪律是：**没有覆盖触发条件的回归测试，任何一条都不算关闭。**

## PR-001 Bootstrap

状态：**已实现并通过本地测试，已纳入 Git 版本管理**。

已交付：

- Python 3.12 src-layout 包；
- `pyproject.toml` 和 `uv.lock`；
- 已迁移并适配正式目录的配置基线；
- 脱敏的 `agent-config-check` 入口；
- clean-room 与合规文档；
- 使用 `main` 分支的独立 Git 仓库。

2026-07-23 验证证据：

```text
uv 0.11.31
Python 3.12.13
uv lock --check --offline: passed
pytest: 43 passed
agent-config-check test profile: status=ok
non-editable install with packaged default config: passed
```

仍不属于 PR-001 的内容：

- CI 和配置 ownership 架构测试；
- API、进程 Container 和 readiness；
- Runtime、Domain 与 Ports；
- Persistence、RAG、Workflow 与 Multi-Agent；
- Docker Compose 和外部服务连通性。

## PR-002 Config CI

状态：**已实现并通过本地同构检查**。

已交付：

- 覆盖 230 个 Settings 叶子字段的 `config/ownership.yaml`；
- ownership 唯一性、生命周期和 Task snapshot 正向 allowlist 架构测试；
- 核心层框架依赖、反向依赖、原始配置读取和 `os.environ` 边界测试；
- development、test、production 三个离线配置 profile；
- Ruff、严格 Pyright（产品源码）、pytest、许可证与 Git 历史密钥扫描 CI；
- GitHub Actions 与下载型工具都固定到 release SHA 或 SHA-256；
- 删除没有消费者的 `admin_token`、`webhook_token` 配置入口。

2026-07-23 本地验证证据：

```text
uv lock --check --offline: passed
ruff format --check: passed
ruff check: passed
pyright: 0 errors, 0 warnings
pytest: 60 passed
development/test/production config profile contract tests: passed
dependency license allowlist: passed
Gitleaks 8.30.1 working-tree/history scan: passed
```

该 PR 只证明配置和 CI 合同，不代表 API、Worker、模型或外部 Adapter 已经
启动。

## PR-003 Domain

状态：**已实现并通过本地测试**。

已交付 `src/agent_workbench/domain/`，只依赖标准库与 Pydantic：

- `schema.py`：`DomainModel`/`VersionedModel` 基类、schema 版本闭合校验、
  `extra="forbid"`、frozen 与 `hide_input_in_errors`；
- `identifiers.py`：平台自铸 ID 前缀与生成器，同时接受 provider 原样
  ID（`toolu_...`）；
- `errors.py`：13 个稳定 `ErrorCode`、`ErrorInfo` 与领域异常层级；
- `artifacts.py`：`ArtifactRef`（content-addressed，不含 URL/路径）；
- `messages.py`：provider-neutral 消息与 text/tool_use/tool_result 块；
- `tools.py`：`ToolSpec`/`ToolCall`/`ToolResult` 与 `align_results()`；
- `context.py`：`ContextPacket`/`ContextChunk`/`Citation`/`SourceLocator`；
- `policies.py`：`PrincipalContext`、`AuthorizationEnvelope`、
  `ExecutionContext`、`PolicyDecision`；
- `runs.py`：`TraceContext`、`RunBudget`/`BudgetUsage`/`TokenUsage`、
  `AgentRunRequest`、`AgentOutcome`、Runtime 状态机与 `StopReason` 词汇表；
- `events.py`：基线 6.7 的 19 种事件、统一 envelope 与 durability 映射。

已经被编码为构造期校验（而不是注释）的不变量：

- 每个 `ToolCall` 恰有一个 `ToolResult`，提交顺序按模型调用顺序稳定；
- write/external/destructive Tool 必须 exclusive 且声明 permission scope；
- 失败 `ToolResult` 必须携带 `ErrorInfo`，成功的必须不携带；
- `ModelDelta`/`ToolProgress` 恒为 transient 且不得携带 stream sequence，
  durable 事件必须携带；`event_type` 必须与 payload 一致；
- Citation 必须落在同一 `ContextPacket` 的 chunk 与相同 document version 上；
- 预算耗尽的 run 报告为 failed + 结构化 `stop_reason`，不伪装成 completed；
- 第三方异常转 `ErrorInfo` 时只保留异常类型名，丢弃消息正文。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 219 passed（其中 domain 新增 159 项）
development/test/production config profile: status=ok
golden 序列化基线：tests/domain/golden/domain_v1.json（13 个聚合）
```

本 PR 不新增依赖，`pyproject.toml` 与 `uv.lock` 未改动，`uv lock --check`
由 CI 执行。

仍不属于 PR-003 的内容：Ports 与 Fake Adapter（PR-004）、CLI 纵向切片
（PR-005）、Runtime 循环本身（PR-006 起）。领域对象存在且有契约测试，
按项目纪律只能标记为 Implemented/Tested，不能标记为 Demonstrated。

## PR-004 Ports + Fakes

状态：**已实现并通过本地测试**。

`src/agent_workbench/ports/`（框架无关 Protocol）：

- `model.py`：`ModelRequest`、`ModelEvent`（text_delta / tool_call / usage /
  completed）、`ModelPort.stream()`；
- `tools.py`：`ToolHandler`、`ToolInvocation`、`ToolBinding`、`ToolRegistry`；
- `agent_executor.py`：`AgentExecutor.run(request, emit, cancellation)`；
- `policy.py`：`PolicyEngine.decide(call, context)`；
- `event_log.py`：`EventScope`、`EventCursor`、`EventLogPort`、`EventSink`；
- `conversation_store.py`：`ConversationSession`、`StoredMessage`、
  `ConversationStore`；
- `artifact_store.py`：`ArtifactStore`；
- `cancellation.py`：`CancellationToken` 协议、`CancellationSource`、
  `NullCancellationToken`。

`src/agent_workbench/adapters/`（无外部依赖的实现）：

- `models/fake.py`：脚本化 `FakeModel`（可重放最后一轮，供死循环测试）；
- `tools/`：`StaticToolRegistry` 与两个无副作用 Tool（`read_document`、
  `text_statistics`）；
- `memory/`：`InMemoryEventLog`、`InMemoryConversationStore`、
  `InMemoryArtifactStore`；
- `policy/envelope.py`：`EnvelopePolicyEngine`（deny-by-default 的过渡实现）；
- `events.py`：`ScopedEventSink`；
- `testing.py`：`fake_stack()` 组合出完整栈，字段类型全部写成 Port，
  由 Pyright 静态验证结构一致性。

本 PR 固定下来的行为：

- **sequence 由 log 分配**，durable 事件在流内单调无洞，transient 事件返回后
  即丢弃且不占用 sequence——SSE cursor 因此是可信的续传位置；
- **跨租户读与不存在完全同形**（同一 `NotFoundError`、同一文案），
  artifact 与 conversation 都有针对性测试；
- `ModelRequest` 只携带 model profile，不含 model id / temperature / key；
- Tool call id 从 provider 原样穿过 proposal → 执行 → tool 消息 → 下一次
  ModelRequest（`test_fake_stack.py` 端到端断言）；
- `EventCursor.decode()` 对畸形输入统一 fail closed，且不解释拒绝原因。

领域侧的唯一改动：新增 `not_found` 错误码与 `NotFoundError`，用于统一
"不存在 / 无权知道它存在"两种情况。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 290 passed（contracts 新增 71）
development/test config profile: status=ok
```

测试用 `asyncio.run()` 驱动协程，**不引入 pytest-asyncio**，
`pyproject.toml` 与 `uv.lock` 保持不变。

按各自工作包推迟的 Port（不在本 PR 冻结）：`IngestionPort`、
`RetrieverPort`、`EmbeddingPort`、`SparseEncoderPort`、`RerankerPort`、
`DocumentStore`、`OutboxPort`（WP03–WP05）；`TaskRegistry`（WP07）；
`ApprovalStore`（WP10）；`TelemetryPort`（WP12）；`SandboxPort`、
`MemoryPort`（更后）。先冻结一个没有实现来校验的协议，正是契约在第一个
使用者出现前就漂移的方式。

## PR-005 CLI Skeleton

状态：**已实现并通过本地测试**。

第一个跑通的纵向切片：输入 → 脚本化模型 → 统一事件 → 输出。

已交付：

- `src/agent_workbench/apps/cli/`：`main.py`（argparse 与退出码）、
  `demo.py`（确定性场景装配）、`rendering.py`（文本与 JSONL 两种渲染器）；
- `agent-cli` console script，CI 用已安装的 wheel 跑 smoke 并与 golden 比对；
- `src/agent_workbench/adapters/agents/single_turn.py`：
  `SingleTurnAgentExecutor`，满足 `AgentExecutor` 协议的 walking skeleton；
- `adapters/events.py` 增加 `ObservingEventSink`（live tee，让订阅者看到
  transient 事件）；`InMemoryEventLog` 与 `fake_stack()` 增加可注入的
  `event_ids`，与既有 `clock` 注入同一目的；
- 领域侧新增 `canonical_arguments()` / `argument_digest()`：事件记录参数摘要
  而不是参数本身，WP10 的副作用 ledger 复用同一份规范形式。

本 PR 固定下来的行为：

- **CLI 只消费统一事件与返回的 `AgentOutcome`**，不触碰 executor、模型
  adapter 或 store 的内部；
- **两个视图取自不同来源**：流式回答来自 live transient delta，时间线来自
  运行结束后对 durable log 的重放。重放拿不到的东西，重连的客户端也拿不到；
- **skeleton 不拥有 tool loop，并且拒绝得很大声**：模型提出工具调用时，先落
  一条 durable `ToolProposed`，再让整个 run 失败。默默丢弃会让模型永远等一个
  不会到来的 ToolResult，正是不变量 1 要防的情况；
- 预算在开工前检查（过期 deadline 时模型调用次数为 0）；
- adapter 异常变成结构化 outcome，且只保留异常类型名——
  测试用 `sk-ant-` canary 断言 provider 异常正文不进入 outcome 与事件；
- `max_tokens` 截断的回答报告为 failed，不伪装成 completed；
- 事件里没有 prompt 正文，测试用 canary prompt 断言 JSONL 中不出现。

demo 模式冻结时钟、用计数器生成事件 ID，因此**同一条命令的输出逐字节可复现**，
由 4 个 golden 文件（文本/JSONL × 完成/拒绝）守护。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 328 passed（cli 19 + 单轮 executor 19）
development/test config profile: status=ok
agent-cli demo / --propose-tool read_document: 退出码 0 / 1
```

`pyproject.toml` 只增加 `agent-cli` 入口，未新增依赖，`uv.lock` 未改动。

范围说明：demo 不加载 Settings。它不连数据库、向量库和任何 provider，要求注入
DSN 才能看一句脚本化回答属于仪式而非安全；从校验过的配置做真实依赖注入是
`bootstrap/container.py` 的职责，与第一个真实 adapter 一起落地。真正的
model-tool 循环仍属于 WP02（PR-006 起），本 PR 没有、也不允许提前实现第二个
循环。

## PR-006 Runtime Serial Loop

状态：**已实现并通过本地测试**。

自研 `ClaudeLikeAgentRuntime` 落地，替换 PR-005 的 walking-skeleton executor
（`SingleTurnAgentExecutor` 及其测试已删除，不与新 Runtime 并存）。

已交付 `src/agent_workbench/runtime/`，只依赖 domain 与 ports：

- `state.py`：把基线 7.1 的状态机写成可执行转移表 + `RunStateMachine`。
  非法转移抛 `InvalidStateTransition`，因此“跳过授权直接执行”这类实现错误
  在第一次发生时就会失败，而不是产出一条看起来合理的运行记录；
- `tool_executor.py`：单次已授权调用的执行原语，负责超时与异常归一化；
- `agent_runtime.py`：串行 `model → tool → result → model` 循环本体。

本 PR 固定下来的不变量：

- **每个已暴露的 `tool_call_id` 恰有一个 `ToolResult`**——未知工具、被拒、
  handler 抛异常、超时、批次中途取消，全部产出结果而不是留空。少一个结果，
  模型就会永远等一个不会到来的回答；
- **提交顺序永远是模型的调用顺序**：串行执行下两者本就一致，仍然强制过一次
  `align_results()`，这样 PR-009 的并行调度器无法通过“谁先跑完”改变模型看到
  的内容；
- **超时属于 executor 而不是循环**：串行循环没有别的办法从一个不返回的
  handler 里恢复，预算约束的是花费不是单次 await 的时长；
- **Tool 只有经过 PolicyEngine 才会执行**：deny 分支下 handler 调用数为 0；
  `allow_with_modified_input` 目前直接拒绝——重写后的参数必须重新做 schema
  校验与再授权，而做这件事的 Tool Gateway 还没到，所以宁可拒绝也不在未校验
  的输入上执行；
- **循环有上限**：`max_steps` / `max_tool_calls` / deadline 在每轮开工前检查，
  一个永远提工具调用的模型会被 `max_steps` 终止（有测试）；
- handler 越权回答别的 `tool_call_id` 时，只让它自己那次调用失败，不会同时
  破坏两个 id 的配对。

CLI demo 随之升级为完整一轮：`agent-cli demo` 现在演示
模型 → `read_document` → ToolResult → 模型 → 回答；`--deny` 演示 deny 分支
（handler 不被调用，模型仍收到该 id 的答复）；`--tool none` 回到单轮文本。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 360 passed（runtime 新增 49）
development/test config profile: status=ok
agent-cli demo / --deny / --tool none: 退出码 0
```

`pyproject.toml` 与 `uv.lock` 未改动。测试套件里有且只有一处等待真实时间：
tool 超时测试固定耗时 1 秒（`ToolSpec.timeout_seconds` 的域下限就是 1 秒），
是有界且确定的，不是靠 sleep 碰运气的竞态测试。

范围说明：本 PR 不含 **schema 校验**与 **Hook 重校验**（两者一起定义“最终参数”
的含义，属于 PR-007 Tool Gateway）、**并行只读调度**（PR-009）、大结果
Artifact 化（WP12）、真实 Anthropic Adapter（WP02-06）。因此基线不变量 2 目前
只实现了授权那一半；demo 中的两个 Tool 自己校验参数类型，`invalid_tool_input`
路径由它们覆盖。

## PR-007 Policy + Tool Gateway

状态：**已实现并通过本地测试**。

把“这次调用能不能跑”收进唯一的 `ToolGateway`，并补上基线不变量 2 与 3
缺的那一半：schema 校验与参数被改写后的重新校验/再授权。

已交付：

- `runtime/schema_validation.py`：一个**明确划定范围**的 JSON Schema 子集
  （type / properties / required / additionalProperties / items / enum /
  minimum / maximum / minLength / maxLength / pattern / minItems / maxItems）。
  超出子集的 schema 在**装配阶段**被拒绝，而不是在调用时被静默跳过——一个会
  忽略 `oneOf` 的校验器，会把每次调用都报成合法却什么都没校验；
- `runtime/tool_gateway.py`：`propose / prepare / authorize / invoke / refuse`
  五个阶段方法 + `advertise()`。Runtime 的批次循环退化成对它的编排，状态机
  仍留在 Runtime；
- `agent_runtime.py` 相应瘦身：不再直接持有 registry / policy / executor，
  只依赖 `ToolGateway`（构造签名随之简化为 `model` + `gateway`）。

本 PR 固定下来的行为：

- **handler 只在“最终参数”同时通过 schema 校验与授权决定之后才运行**。
  “最终”是关键词：策略返回 `allow_with_modified_input` 时，重写后的参数会被
  重新校验并重新提交决定；能在检查之后改参数，就等于同时绕过两道检查；
- **重写循环有界**（默认 3 轮）。一直改参数的引擎会被拒绝，而不是被无限重试；
- **重写成非法参数时调用被拒**，handler 调用数为 0（有测试）；
- **顺序可观测**：schema 校验先于授权，所以参数非法的调用连 `PermissionResolved`
  都不会产生——`tests/runtime/test_agent_runtime.py` 直接断言这条时间线；
- **参数体积上限**（默认 65536 字节，对应 `policy.max_tool_argument_bytes`）在
  解析之前拦截；
- **校验错误只报路径和期望，不报值**：这些消息会进入事件、运维日志和模型自己的
  上下文，用 canary 测试守住；
- Gateway 同时拥有一次调用的审计轨迹（proposal / permission / start /
  completion / failure），事件因此不可能和它实际做的事不一致。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 398 passed（新增 schema 24 + gateway 14）
development/test config profile: status=ok
CLI golden 文件逐字节未变——这次重构对 demo 行为完全等价
```

`pyproject.toml` 与 `uv.lock` 未改动：JSON Schema 子集是自研的，没有引入
`jsonschema` 依赖。工具 schema 都写在本仓库里，为了 8 个关键字引入一个完整
实现并不划算；代价是子集必须显式，扩展它是这个文件里的一次自觉修改。

范围说明：不含 **Hook Bus**（WP02-05，`ports/hooks.py` 与 `runtime/hook_bus.py`
尚未存在）——目前唯一能改写参数的来源是 PolicyEngine，重新校验/再授权的骨架
已经就位，接 Hook 时复用同一条路径。同样不含并行只读调度（WP02-04 / PR-009）、
大结果 Artifact 化（WP12）与真实 Model Adapter（WP02-06/07）。

## PR-008 Runtime Budgets

状态：**已实现并通过本地测试**。

PR-006 已经做掉了循环上限（`max_steps` / `max_tool_calls` / token / cost）与
轮次边界上的取消。本 PR 补的是**时限**：在实施计划 5.2 之前，一个停住不动的
模型流会让整个 run 无限期挂起——预算约束的是花费，不是单次 await 的时长。

已交付：

- `runtime/budgets.py`：纯粹的 deadline 运算。
  `effective_model_deadline()` 返回各层下界的最小值**以及它来自哪一层**；
  `effective_tool_timeout()` 用 run 剩余时间约束单个工具；
- Runtime：单次模型调用套 `asyncio.timeout(有效 deadline)`；流式过程中在每个
  事件边界检查取消；退出时显式 `aclose()` 生成器；
- ToolExecutor / Gateway：`run_budget_seconds` 一路传到执行处。

本 PR 固定下来的行为：

- **有效 deadline 是一个下界，不是几个互不相关的定时器**：
  `min(运行时 envelope, run 剩余时间)`。模型 profile 自己的超时由 Adapter 在更
  内层施加——只有 Adapter 知道 profile 映射到哪个具体模型——两层嵌套，短的先触发；
- **哪一层到期决定了怎么报**：run deadline 到期是预算结果（`budget_exceeded` +
  `stop_reason="deadline"`，run 结束）；超出 envelope 是 provider 问题
  （`provider_error`，`retryable=True`）。两者相等时按 run deadline 报，因为它的
  到期后果更严重。同一个"模型卡住"的场景有两个测试，只有配置不同、结论不同；
- **工具不能活得比批准它的 run 更久**：`min(工具声明超时, run 剩余时间)`，
  且每次调用重新计算——第一个工具跑得慢，第二个能用的就更少；
- **取消在下一个事件边界生效**，并通过关闭生成器传导到 Adapter（测试用生成器
  `finally` 里的标志位断言 `closed is True`）。模型完全静默时由 deadline 兜底，
  所以"有界时间内停止"两条路径都有覆盖；
- 卡住的模型调用**不会**产生 `ModelCompleted`——它确实没有完成。

取消测试不使用 sleep：cancel 是在事件 sink 观察到第一个 `ModelDelta` 时发出的，
因此交错是确定的。三个超时测试各等 50 毫秒真实时间，有界且必然触发。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 414 passed（新增 budgets 12 + runtime 5）
development/test config profile: status=ok
CLI golden 文件逐字节未变
```

范围说明：三层 retry 计数（model / graph node / task）中只有 model 这一层的
deadline 在本 PR 内；node 与 task 层属于 WP06/WP08。真实 Adapter 的
`max_retries` 要等 WP02-06 的 Anthropic Adapter。Hook Bus（WP02-05）与并行只读
调度（WP02-04）仍未实现。

## PR-009 Parallel Reads

状态：**已实现并通过本地测试**。

补上基线不变量 4 与 5：`parallel` 工具可以并发，`write/external/destructive`
必须过 exclusive 屏障；执行顺序与提交顺序彻底分离。

已交付：

- `runtime/tool_scheduler.py`：`plan_tool_batches()` 是一个**纯函数**——
  接收一批已授权调用，返回执行分组。并发 bug 难观察更难复现，所以"谁能和谁
  一起跑"这个决定放在不需要事件循环就能验证的代码里；
- Runtime 执行阶段按组推进：组内 `asyncio.gather`，组间顺序执行。

分组规则只有两条：

1. **保持模型的顺序**：从左到右扫描，连续的 parallel 调用累积成一组，因此
   排在写操作之前的读，绝不会在它之后执行；
2. **exclusive 调用自成一组**：写/外部/破坏性工具在领域层就被强制为
   exclusive（`ToolSpec` 构造期校验），所以"副作用要过屏障"从文档里的一句话
   变成了内存里的形状——它两侧都不会有东西在飞。

本 PR 固定下来的行为：

- **提交顺序永远是模型的调用顺序**，与完成顺序无关。测试用一对握手 handler
  证明：`waits` 必须等 `opens` 打开闸门才能返回，所以完成顺序是
  `[fast, slow]`，而提交给模型的顺序是 `[slow, fast]`；
- **并发是被证明的，不是被假设的**：上面那对 handler 在串行调度下会互相
  等死（由工具超时兜底），因此这个测试同时证明了"确实并发"和"顺序稳定"；
- **exclusive 工具旁边永远没有别人**：探针记录同时在场的 handler 数，
  写工具那一格恒为 1；
- `max_parallel_read_tools`（对应配置同名字段，默认 4）限制同时在场数量；
- 取消发生在组与组之间：**已在飞的组保留真实结果**，尚未开始的组拿到
  cancelled 结果——取消停止未开始的工作，不改写已经发生的事。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 428 passed（新增 scheduler 9 + runtime 5）
development/test config profile: status=ok
CLI golden 文件逐字节未变
```

范围说明：CLI demo 仍然只调用一个工具。M1 验收里"两个只读 Tool"的固定演示
留到评测/演示工作包——golden 文件是逐字节比对的，把它钉在并发执行的事件交错上，
等于承诺一个调度器并不保证的顺序。并发性由上面的握手测试证明，那是更强的证据。

不含 Hook Bus（WP02-05）与真实 Model Adapter（后来确定为 DeepSeek，
WP02-06/07）。WP02 至此
只剩这两项。

## PR-010 Hook Bus

状态：**已实现并通过本地测试**。

补上 WP02-05：部署方提供的 Hook 可以在工具调用被判定之前检查、改写或拦截它。
WP02 至此只剩真实 Model Adapter。

> 编号说明：原计划 §9 的首批 PR 列表把 Hook Bus 留在 WP02 内部未编号，
> PR-010 是 PostgreSQL/Artifact Base。实施时把 Hook Bus 排进 PR-010，其后
> Provider 占用 PR-011～PR-012，持久化与上传车道落在 PR-013～PR-015，
> 计划文档已同步。

已交付：

- `ports/hooks.py`：`ToolCallHook` 协议与 `HookOutcome`（不变 / 改写参数 /
  拦截）。Hook **返回决定，不修改传入的调用**；能改的只有 arguments——
  工具名和 call id 属于模型的请求和那条必须回答它的结果；
- `runtime/hook_bus.py`：按注册顺序跑一遍，每个 Hook 看到的是上一个产出的结果。

本 PR 固定下来的行为：

- **Hook 改写过的参数重新走 schema 校验，然后才进授权**——能在检查之后改参数，
  就等于绕过检查。这条路径和 PolicyEngine 的 `allow_with_modified_input`
  复用同一套 Gateway 逻辑（PR-007 就是按这个形状搭的）；
- **失败即拦截，不是忽略**：Hook 抛异常或超时，被检查的那次调用直接被拦下。
  反过来——把坏掉的 Hook 当成放行——会让部署方写的每条安全规则在出 bug 的
  瞬间静默消失；
- **Hook 有时限**（默认 5 秒），和它守护的工具同样理由：一个无界 await 就能
  让一条慢规则拖住整个 run；
- **只跑一遍**：后面的 Hook 改写不会重新触发前面的 Hook。多轮需要一条收敛
  规则，而说不清的规则等于没人能 review 的规则；
- **Hook 看不到已经失败的调用**：未知工具、参数非法的调用在 Hook 之前就被拒，
  Hook 负责塑形合法输入，不是给坏输入当第二个解析器；
- Hook 异常正文不外泄，只保留异常类型名（`sk-ant-` canary 测试）；
- Hook 名字必须唯一——审计行必须能说清是谁拦的。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 445 passed（新增 hook_bus 12 + gateway 5）
development/test config profile: status=ok
CLI golden 文件逐字节未变（demo 不装 Hook，空 bus 是 no-op）
```

范围说明：Hook 目前在代码里装配，没有对应的 Settings 字段——`runtime.*` 里
没有 hook 相关配置，因此 `config/ownership.yaml` 无需改动。把 Hook 变成可配置
（以及 `after_tool` / 会话级 Hook）要等 Bootstrap Container 能注入它们时再说。
本 PR 只实现 `before_tool` 一个阶段。

## PR-011 DeepSeek Provider Contract

状态：**已实现并通过本地测试**。

模型 Provider 由 Anthropic 改为 **DeepSeek**（OpenAI 兼容协议）。

这次改动最值得记的一点是：**Runtime、Gateway、Scheduler、Domain 一行都没改。**
换 Provider 只动了配置契约和（尚未实现的）Adapter，这正是 `ModelPort` 存在的
理由。相应地，它也不是改一行能完事的——`model.provider` 是被冻结成 `Literal`
的，`configuration.md` 早就写明"新增 Provider 时升级配置 schema，而不是接受
一个无法启动的 provider 字符串"，所以这是一次 schema 迁移。

已交付：

- `model.provider: Literal["deepseek", "fake"]`（`anthropic` 已移除——没有
  Adapter 的 provider 字符串不该是可配置的）；
- 新增 `model.base_url`，默认 `https://api.deepseek.com`；沿用与 Qdrant/OTel
  同一套 endpoint 校验（禁 userinfo / query / fragment）；
- `secrets.deepseek_api_key` 取代 `anthropic_api_key`，同步更新
  `SECRET_LIKE_ENV_KEYS`、脱敏键表、mounted-secret 文件名与 CI 环境；
- `config_schema_version` `1.1` → `1.2`；
- `config/ownership.yaml`：新增 `model.base_url`（owner
  `adapters.model.deepseek`，lifecycle `startup`）与 `secrets.deepseek_api_key`。

顺带修掉了一个**潜伏的快照违规**：Task snapshot 的正向 allowlist 原本写的是
`model.*`，而 `configuration.md` §7 明确规定 endpoint 不得进入恢复快照。加上
`model.base_url` 会让它直接违规，所以 allowlist 收窄为
`model.provider` / `model.main.*` / `model.compact.*`，`run_semantics_snapshot()`
显式剔除 `base_url`。**迁移端点不改变一个在跑的 Task 的语义，恢复旧 Task 也不会
连回它当初的端点**——两条都有测试。

新增的安全规则：

- **`model.base_url` 必须是 HTTPS，除非指向 loopback**。每一次请求都带着
  provider API key，所以这条比 Qdrant 那条更严（Qdrant 只在 `remote` 时强制
  HTTPS，模型端点则是无条件的，只给本地兼容服务开口子）。

2026-07-25 验证证据：

```text
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 452 passed（config 新增 7）
development/test/production config profile: status=ok（config_schema_version=1.2）
CLI golden 文件逐字节未变
```

`pyproject.toml` 与 `uv.lock` 未改动——本 PR 不引入任何依赖。

范围说明：**Adapter 本体不在本 PR 内**。DeepSeek 的流式接口需要一个 HTTP
客户端（`httpx`），也就是需要新依赖与重新生成的 `uv.lock`，而当前工作环境里
没有 `uv`。`tests/architecture/test_dependency_boundaries.py` 已经预先把
`httpx`、`openai`、`deepseek` 加进核心层禁止导入清单，Adapter 落地时不会悄悄
渗进 Runtime。

另注：CI 的依赖许可证 allowlist 目前是
`MIT / BSD-2 / BSD-3 / Apache-2.0 OR BSD-2-Clause / PSF-2.0`，**不含纯
Apache-2.0**。选 HTTP 客户端时需要一并确认许可证，或显式扩展 allowlist。

## PR-012 DeepSeek Model Adapter

状态：**已实现并通过本地测试**。

第一个与进程外世界通信的 Adapter，也是第一个引入外部依赖的 PR。

已交付：

- `adapters/models/deepseek.py`：`DeepSeekModel` 实现 `ModelPort`，走 DeepSeek
  的 OpenAI 兼容 chat completions 流式接口；
- `httpx` 依赖（BSD-3）与重新生成的 `uv.lock`；
- CI 许可证 allowlist 扩展（见下）。

本 PR 固定下来的行为：

- **工具调用攒齐才发**：provider 把一次调用的 JSON 参数拆成多个分片流下来，
  Adapter 按 index 缓冲，直到流声明结束才组装成 `ToolCall`。半截 JSON 绝不能
  进 schema 校验和策略——`ports/model.py` 的契约就是这么写的，现在有实现来兑现；
- **每条流都以 `ModelStreamCompleted` 结尾**，包括失败的那些。让调用方去区分
  "provider 停了"和"adapter 抛了"，就是让它在某处一定弄错；
- **线上的东西一个字都不往回引**：HTTP 错误只带状态码，不读也不引用响应体——
  聊天补全的错误体可能把发出去的 prompt 原样回显，而错误文本会流进事件、日志和
  模型自己的上下文（canary 测试守住）；
- 传输层异常只保留异常类型名（URL 及其 query 可能出现在消息里）；
- **不认识的 finish_reason 报错而不是猜**（例如 `content_filter`）；
- 参数无法解析、缺 id 或缺名字 → 报 provider 错误。猜一个参数等于把模型从没要求
  过的东西送到 handler 面前；
- `stream_options.include_usage` 必开：拿不到 token 账目的 run 没法执行 token
  预算。DeepSeek 的 `prompt_cache_hit_tokens` 也一并映射（其他兼容服务上缺省为 0）。

测试用 `httpx.MockTransport` 喂**真实线格式字节**，CI 依然完全离线。其中一条端到端
测试直接断言 **Runtime 分不出它和脚本化模型的区别**：同样的两轮工具循环，durable
时间线与 `tests/runtime` 里 FakeModel 那条逐项相同。这是 `ModelPort` 这层抽象的
验收标准，不是"我检查过了"。

### 许可证 allowlist 扩展

加 `httpx` 时 CI 的许可证门禁拦下了它的传递依赖 `certifi`（MPL-2.0）。核查后发现
这不是 httpx 的问题——**原 allowlist 与项目自己选定的技术栈冲突**：

| 依赖 | 许可证 | 原 allowlist |
|---|---|---|
| certifi | MPL-2.0 | ❌ |
| asyncpg | Apache-2.0 | ❌ |
| qdrant-client | Apache-2.0 | ❌ |
| opentelemetry-sdk | Apache-2.0 | ❌ |

PR-002 那份清单看起来是照着当时已有的 21 个包写的，是描述而非策略。现在把策略写
明确（见 `docs/compliance.md`）：允许宽松许可证与文件级弱 copyleft，拒绝强
copyleft（GPL/AGPL/LGPL）与未声明许可证。allowlist 同时列 SPDX 与 classifier
两种拼写，因为 `pip-licenses` 只照搬包元数据里写的那种。

2026-07-25 验证证据：

```text
uv lock --check --offline: passed
ruff format --check: passed
ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest: 473 passed（deepseek 新增 21）
dependency license allowlist: passed
CLI golden 文件逐字节未变
```

`uv lock --check` 与许可证门禁是前十个 PR 里唯一只能靠 CI 验证的两项，本次起可以
在本地跑（`uv` 按 CI 固定的 0.11.31 装在一次性 venv 里，未污染项目 `.venv`）。

范围说明：Adapter 尚未接进 Bootstrap——`base_url`、API key 与 profile 目前由构造
参数注入，从校验过的 Settings 组装是 `bootstrap/container.py`（WP00-03）的职责。
LangChain model/tool 互操作 Adapter（WP02-07）仍未实现。**model ID 与工具调用支持
情况请对照 DeepSeek 当前文档确认后再投产**；本 PR 的 contract test 钉住的是线格式
处理，不是某个具体模型的能力。

## PR-013 PostgreSQL/Artifact Base

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。

第一次有了真正的持久化：WP03-01～03（session factory + Alembic、PostgreSQL
ConversationStore、本地 ArtifactStore）。

已交付：

- `adapters/persistence/`：`models.py`（SQLAlchemy **Core** 表，不是 ORM——
  仓储的职责就是把行显式映射成领域对象，identity map 和惰性加载只会再引入一
  套隐式的"何时读"）、`engine.py`、`conversation_store.py`；
- `migrations/` + `alembic.ini`：`0001_conversations`；
- `adapters/artifacts/local.py`：`LocalArtifactStore`。

本 PR 固定下来的行为：

- **位置在锁住会话行的前提下分配**：同一会话的两次 append 在锁上串行，
  `sequence` 因此是**无洞的**而不只是唯一的。数据库另有
  `UNIQUE(session_id, sequence)`——锁一旦被绕过，写入会失败而不是悄悄复用位置。
  有测试并发发 5 条断言拿到 `[1,2,3,4,5]`；
- **消息按领域对象存取**（JSONB，含 schema 版本），读回时过同一个模型。
  用本进程不认识的契约写下的行会在边界上 fail closed，而不是半懂不懂地进入
  模型上下文；
- **`statement_timeout` 设在连接级**而不是每条查询：逐条设置的超时迟早会有人忘；
- **本地 Artifact 先写 blob 再写元数据**——中途崩溃留下的是一个没人引用的
  blob（垃圾），而不是一个指向不存在字节的引用（谎言）；
- **元数据放在 blob 旁边而不是建表**：目前除了按 id 点查没有任何查询，
  只服务点查的表是"伪装成索引的 schema"。等上传落地、artifact 行必须与
  document version 同事务写入时，那张表才有存在的理由；
- 跨租户读与不存在**完全同形**——沿用 in-memory 既有的常量文案（不含 id）。

契约测试重构成**一套跑两种实现**：`tests/contracts/` 的 conversation 与
artifact 套件现在参数化在 `memory`/`postgres` 与 `memory`/`local` 上。一个 Port
有两个实现和两套测试，就等于有两个契约。

`tests/persistence/test_migrations.py` 断言迁移与模型元数据描述的是同一个
schema（`compare_metadata`），并跑一遍 downgrade → upgrade。没人跑过的
downgrade 会在需要它的那次事故里才被发现。**这个测试验证过是有牙的**：给模型
加一列会让它失败。

2026-07-25 验证证据：

```text
本机 PostgreSQL 15.14（专用库 agent_workbench_test）
alembic upgrade head: 通过，schema 与模型零漂移
ruff format --check / ruff check: passed
pyright (strict, src): 0 errors, 0 warnings
pytest（有库）: 498 passed
pytest（无库，等同主 CI job）: 484 passed, 14 skipped
```

CI 新增 `postgres` job：`postgres:16`（按 digest 固定）service，先跑迁移再跑
两个数据库相关套件。主 `quality` job 保持完全离线，数据库参数化用例在那里跳过。

两条安全护栏：

- 测试用的 DSN 变量是 `AGENT_WORKBENCH_TEST_DSN`，**故意不在 `AW_` 命名空间内**
  ——settings 会拒绝任何未登记的 `AW_*`，那道守卫比前缀对称更值钱；
- 该 DSN 指向的库名必须以 `_test` 结尾，否则直接拒绝。这些套件会 TRUNCATE，
  导错 DSN 应该得到一个被跳过的套件，而不是一个被清空的数据库。

范围说明：上传数据面（create-upload / 流式或 presigned / complete）、Outbox、
document/version/ACL 仓储与 S3 Adapter 属于 WP03-04～09，不在本 PR。
`artifacts` 表后来随 PR-014 落入实际迁移 `0002_documents_outbox`。三个 DSN
里本 PR 只建了普通查询用的那一个；guard 与 listener 引擎要等协调工作包，
那时它们的连接规则才开始有意义。

## PR-014 Upload/Outbox

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。

WP03-04～06、08、09：上传用例、document/version/ACL 仓储、ingestion outbox
与竞争领取。

已交付：

- `ports/documents.py` / `ports/outbox.py`：`DocumentStore`、`OutboxPort`
  及其 DTO（PR-004 里刻意推迟冻结的两个 Port，现在有实现来校验了）；
- `adapters/persistence/documents.py`、`outbox.py`；
- `application/uploads.py`：`UploadService`（首个应用服务层）；
- 迁移 `0002_documents_outbox`：`artifacts`、`upload_intents`、`documents`、
  `document_versions`、`document_acl`、`outbox_events`。

本 PR 固定下来的行为：

- **version 与它的 outbox 事件同事务提交**。拆成两次提交会同时制造两种排序
  修不好的故障：一个永远不会被索引的文档，和一条指向已回滚内容的索引项。
  测试用 `monkeypatch` 把 outbox 插入换成会触发 CHECK 约束失败的版本——它在
  document 与 version 行已经写入之后才失败——断言两者都没留下；
- **revision 在锁住 document 行的前提下推进**，因此是**单调的**而不只是互不相同；
- **新建文档用条件插入再加锁**：两个上传竞争创建同一个新文档时，都读不到行，
  普通 INSERT 会让后者撞主键。`ON CONFLICT DO NOTHING` 之后重新加锁，拿到的
  要么是自己插的行，要么是抢先者的；
- **完成是双重幂等的**：同一个 upload 再完成一次返回它已经产出的 version；
  内容与当前版本完全相同时不推进 revision——重发的请求不该让索引重做一遍
  产出完全相同行的工作；
- **完成时两边都不信**：读取已存对象自身的 size 与 digest，与传输发生**之前**
  客户端声明的值比对。传错的字节、或声明了自己没有的摘要，都在这里失败，
  而不是变成一个索引会忠实复现的 document version；
- **outbox 没有指向 documents 的外键**：删除事件必须比它描述的那一行活得更久，
  否则索引永远无法被告知遗忘它；
- outbox payload 携带 `authorized_principals`，索引的过滤条件要靠它；
- `SKIP LOCKED` 领取：两个 worker 并发各领 3 条，合计 6 条、零重叠（有测试）。

2026-07-25 验证证据：

```text
本机 PostgreSQL 15.14
alembic upgrade head（0001 → 0002）: 通过，零漂移
pytest（有库）: 525 passed
pytest（无库，等同主 CI job）: 496 passed, 29 skipped
ruff / pyright (strict, src): 全部通过
uv lock --check --offline / 许可证 allowlist: 通过
```

**并发测试抓到了一个真实缺陷**：最初的实现在两个上传同时创建同一个新文档时会
撞主键失败。这不是测试写错了，是实现漏了竞争窗口——已按上面的条件插入模式修复。

反射式 Port 契约测试（PR-004 建立）也拦了一次：四个新聚合必须补进样例表才能
通过 round-trip / 版本 / golden 三重检查。

范围说明：**没有 HTTP**。FastAPI 上传路由与 2 MiB 控制面 request-limit
中间件（WP03-07）留给独立 PR——它引入的是另一个方向的表面（依赖、应用装配、
中间件、413 语义），和本 PR 的"事实与 outbox 原子性"是两件事。

outbox 的 claim **不是 lease**：worker 死了，它领的事件就一直被领着，这里没有
任何东西回收它。lease 时长、heartbeat、fencing 属于协调工作包；在这里做一半会
得到一个看起来可恢复、实际不可恢复的东西，比一个明显的缺口更糟。S3/presigned
传输与 `document_deleted` / `acl_changed` 两类事件的产生路径同样留待后续。

## PR-015 Upload API

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。WP03 的本地存储与
Upload/Outbox 基线完成；S3/presigned 与完整对象授权仍未完成。

WP03-07：FastAPI 上传路由与控制面 request-limit 中间件。

已交付：

- `apps/api/`：`main.py`（应用工厂 + `agent-api` 入口）、`dependencies.py`、
  `middleware.py`、`identity.py`、`state.py`、`routes/`（uploads / artifacts /
  health）；
- `bootstrap/projections.py`：`ApiRuntimeConfig` 等**窄配置对象**；
- `ArtifactStore.put_stream()`：流式写入，两个实现都补齐。

本 PR 固定下来的行为：

- **两个平面共用一个服务器**。控制请求是描述工作的 JSON，被
  `api.max_control_request_body_bytes` 限制住并在超限时返回 **413**；数据面
  （`PUT /v1/uploads/{id}/content`）豁免——把文档传输限制在控制面大小上，
  等于不支持上传；
- **限制不信任 `Content-Length`**：中间件读到比上限多一个字节为止。缓冲这么多
  正是上限本身允许的量，也是唯一能对"声明了长度却不遵守"的请求做出正确判断的
  办法。有一条测试专门用分块 body 且不声明长度来打这个点；
- **本地存储边读边写**：分块落盘、边写边算哈希与字节数，全程不在内存里持有整个
  对象；在检疫文件名下写完才改名发布，所以失败或超限留下的是一个检疫文件，
  而不是一个别人能读到的半成品；
- **跨租户读与不存在完全同形**：状态码、响应体都一样（有测试直接比对两者）；
- **传输前先读 intent**：未知上传或别的租户的上传在写入第一个字节之前就被拒；
- liveness 不碰数据库（碰了就会在与它无关的故障里报告进程已死，让编排器无故重启
  它）；readiness 碰，但有 2 秒上限。

**配置只有一个入口这条规则被架构守卫强制执行了。** `apps/api` 最初直接依赖
`bootstrap.settings`，`test_raw_configuration_sources_are_confined_to_bootstrap`
立刻拦下，报错信息就是设计说明："inject a narrow configuration object instead"。
于是补上 `bootstrap/projections.py`：API 拿到的是 `ApiRuntimeConfig`，看不到
检索漏斗、协调时序或评测指标，DSN 仍以 `SecretStr` 形式传递、只在构造引擎时
解包——一个配置对象的 repr 因此打不出 DSN。

**身份是接口层的结果，不是请求体字段。** 目前只有一个读 header 的开发用解析器，
`deployment_scope == "remote"` 时会在装配阶段拒绝启动。写这一节时默认 host 还是
`0.0.0.0`，名义上的 local scope 仍可能监听所有网卡，所以这道检查当时不足以阻止
意外暴露；监听地址的强制校验见下方 P0-1 一节（2026-07-26）。

2026-07-25 验证证据：

```text
本机 PostgreSQL 15.14
pytest（有库）: 541 passed
pytest（无库，等同主 CI job）: 496 passed, 45 skipped
ruff / pyright (strict, src): 全部通过
uv lock --check --offline / 许可证 allowlist: 通过
CLI golden 文件逐字节未变
```

CI 的 `postgres` job 现在同时跑 `tests/api`。

**修掉一个会挂住生产的 bug。** 413 中间件缓冲请求后，用一个"永远返回空 body"的
`receive` 替换了原来的。Starlette 的流式响应会在同一个通道上等
`http.disconnect`——它永远等不到，于是 artifact 下载整个挂死。测试跑不完暴露了
它；现在缓冲消息放完之后会**回落到原始通道**。

另一处也是测试抓的：数据面豁免最初写成了路由前缀 `/v1/uploads`，把**声明端点
一起豁免了**，413 门禁形同虚设。改成一个精确谓词
`is_data_plane_path()`——只有以 `/content` 结尾的那一条路由算数据面。

范围说明：Chat / Task / SSE / Approval 路由属于 WP13；S3 presigned 传输、
`document_deleted` 与 `acl_changed` 事件路径仍未实现。真正的身份提供方要等
D0 决策检查点（WP04 前）。

## ADR-012 身份边界（D0 决策点）

状态：**已决**。见 [ADR-012](./adr/0012-identity-boundary.md)。

实施计划要求在检索工作包之前定下：v1 是单用户本地演示，还是接入一个已认证的
Principal Adapter。之所以必须先定，是因为 WP04 的检索要按 ACL 过滤候选，而
ACL 过滤的意义完全取决于 principal 从哪里来。

**决定**：v1 在领域层是多租户的，在部署层是单机的。

到 WP03 为止的事实是：**隔离规则已经做完了，缺的是认证**。这两件事经常被混为
一谈，而它们的失败模式完全不同——隔离错了会泄漏数据，认证错了会让任何人成为
任何人。租户隔离在仓储、应用服务与 HTTP 三层都有测试，跨租户读与"不存在"在
状态码、响应体和错误文案上完全同形；缺的只是"请求头里写谁就是谁"这一层。

否决了两个选项：把 v1 降为单用户演示（会让 WP04 的 ACL 过滤无法验证，而那正是
这个项目在 RAG 方向最值得展示的部分），以及现在就接 OIDC（JWKS 缓存与轮换、
时钟偏移、令牌撤销各自都是能微妙出错的地方，且现在没有任何消费者压力，还需要
一份基线 13.1 尚未覆盖的令牌层威胁模型）。

原 ADR 依赖“开发身份只能随 loopback 监听运行”这一安全前提。2026-07-25 核验
发现当前实现并未强制它：默认 `api.host = "0.0.0.0"`，local scope 不检查实际
监听地址。因此 remote 拒绝装配只能算部分护栏，不能证明缺口无法被意外暴露。
ADR 已补记这一实现偏差，并在 2026-07-26 修复（见下方 P0-1 一节）。

新增 `tests/architecture/test_identity_boundary.py`，把 ADR 里的规则变成可执行
的：`PrincipalContext` 只能在一份显式清单里的模块中构造（API 的解析器、CLI 的
demo、以及定义它的领域模块）。新增一处就要改清单，也就必须有人解释为什么。
**验证过是有牙的**：在 `runtime/` 下放一个构造点会让它按模块名报错。同一个文件
还断言 remote 拒绝装配的那两行仍然在位。

能力表相应更新：tenant-scoped 数据访问标为 Implemented/Tested，**生产级身份
认证明确保持 Planned**。同 tenant 不同 principal 的对象级授权仍有已知缺口，
不能笼统宣称完整租户隔离已完成。README 与简历同样不得升级这一项；`scopes`
目前由调用方在请求头里自述，因此不是权限来源，只是让真实解析器接入时不必改动
下游的形状占位。

## P0-2 审批 fail closed

状态：**已实现并通过本地测试**。核验报告 §4 的 P0-2、§6 修复顺序第 2 项。

`ToolGateway.authorize()` 现在在两个 allow 分支之前检查
`decision.requires_approval`。为真时发出 `PermissionRequested`，然后以
`approval_required` 拒绝这次调用；handler 不再被触及。

这里的判断是：`effect="allow", requires_approval=True` 不是许可，是**尚未裁决**。
在此之前 `requires_approval` 是一个纯粹的只写字段——`domain/policies.py` 定义它、
`EnvelopePolicyEngine` 按风险等级设置它，而整个 `runtime/` 包没有任何一处读它。
于是「需要审批」这个信息在从 Policy 流向执行的路上被无声丢弃了，而丢弃的方向
恰好是放行。

之所以是拒绝而不是挂起等待：审批设施（`ApprovalStore`、恢复入口）属于 WP10，
现在不存在。「需要人来决定，但这里没有人可以决定」只能落到拒绝上。等 WP10 到位
后这一分支改为挂起并等待裁决，改动仍然限于 Gateway——`PermissionRequested` 事件
与 `approval_required` 错误码从 PR-003 起就在 Domain 里定义好了，此前一直没有
写入方，这次只是终于把它们接上。

模型侧拿到的是 `status="error"` 且正文含 `approval_required` 的 tool result，
因此能区分「不被允许」和「尚未裁决」；run 本身继续正常收尾，不会因为一次待审批
的调用而失败。

回归测试 6 条：Gateway 级 2 条（含 `allow_modified` 改写分支同样被拦），Runtime
级 4 条（完整 run 副作用为零、持久事件序列、回灌给模型的错误正文，以及一条对照
组）。**验证过是有牙的**：临时撤掉那个分支后前 5 条全部失败。第 6 条对照组在
撤掉后仍然通过——正是它保证前 5 条不是靠「write 工具一律拒绝」这种过度实现凑出
来的，拒绝跟的是审批要求本身，不是风险等级。

范围说明：只改这一件事。P0-1（loopback 强制）、P1-6（改写绕过字节上限）
与 P1-7（Policy/Hook deadline）都在同一个文件附近，但各自是独立的行为变化，
留给各自的 PR。

## P0-1 监听地址强制 loopback

状态：**已实现并通过本地测试**。核验报告 §4 的 P0-1、§6 修复顺序第 1 项。

`api.host` 默认从 `0.0.0.0` 改为 `127.0.0.1`；`ApiSettings.host` 只接受 loopback
地址；`build_dependencies()` 在选定 Header Resolver 之前再校验一次。

**规则是无条件的，没有以 `deployment_scope` 为条件。** scope 是部署给自己贴的
标签，而决定谁能触达 Header Resolver 的是绑定地址——把标签当作绑定地址的代理，
正是这条缺陷成立的原因。remote scope 拒绝装配的检查保持不变，两者互不替代。

为什么两层都校验：`ApiRuntimeConfig` 可以不经 Settings 构造（测试就这么做），
而装配层正是选定 Header Resolver 的那一层，拒绝把它和一个可达地址配在一起的
判断，属于那里。

不是 `localhost` 的主机名一律拒绝，而不是去解析它：解析在校验时和 bind 时可以
给出两个答案，DNS 在两者之间还能改变，「不确定」的安全答案是否。

新增 `tests/api/test_bind_address.py`，16 条，覆盖提交默认值、Settings、装配和
真实 socket 四层。**验证过是有牙的**：撤掉 Settings 校验失败 6 条，撤掉装配层
校验失败 1 条，只把默认值改回 `0.0.0.0` 失败 5 条，三者全撤失败 11 条。

其中 socket 那两条刻意绕开 `Settings`，直接读 `config.default.toml` 的原始值来
`bind()`——只见过校验器放行的值的 socket 测试，抓不到校验器本身写错，而这一条
要在校验器都被删掉时仍然成立。三者全撤那次，它是靠一次**成功的跨接口连接**抓到
缺陷的，不是靠读字符串。

配套的对照组同样必需：同一条连接在 `0.0.0.0` 绑定下必须连得上。没有它，
「连接被拒绝」既可能说明护栏有效，也可能说明探针指向了一个本来就没人监听的端口。
这正是这条缺陷能活下来的机制——`test_the_api_refuses_a_remote_deployment_scope`
断言的是 scope 标签，读起来却像在守护整个身份边界。该测试保留（scope 那一半它
确实守住了），docstring 已写明它不覆盖监听地址。

范围说明：这挡住的是**意外暴露**，不是认证。反向代理、SSH 端口转发或容器端口
映射仍可以把 loopback 进程送上网络——那是部署方的选择，代码拦不住，也不该假装
拦得住。生产身份认证仍是 Planned，README 与能力表不因此升级。Settings 叶子字段
数不变（231），没有新增配置项。

## P1-1 上传与文档的对象级授权

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。核验报告 §4 的 P1-1、
§6 修复顺序第 3 项的前半。

`DocumentStore` 的每个方法都显式接收 `principal_id`；`UploadService` 与三条上传
路由把接口层解析出的 principal 一路传下去。此前整条链路只往下传 tenant，于是同
一个 tenant 里的任何人只要知道 upload id，就能替 owner 传输、替 owner 完成，或者
把自己的 upload 指向别人的 document、覆盖内容并替换 ACL。

**读和写是两条不同的规则**，这是这一条最重要的判断：

| 操作 | 谁可以 |
|---|---|
| 观察 / 传输 / 完成一个 upload | 声明它的那个 principal |
| 向已存在的文档提交新版本 | 文档 owner，**仅此** |
| 读文档、版本列表、授权名单 | owner **或** ACL 授权的 principal |

把 ACL 同时当作写授权，会让「授权某人查看」悄悄变成「授权某人覆盖」——那是没有
任何人打算给出的权限。所以 `_is_granted()` 只服务读路径，写路径只比 owner。

**授权检查与写在同一把锁下。** `_locked_document()` 现在返回整行（revision、
owner、knowledge base），检查放在 `FOR UPDATE` 之后：先检查再取锁，判断的是一份
可能已经不是被写对象的行。条件插入那条竞态分支同理——检查放在
`ON CONFLICT DO NOTHING` **之后**，对最终握住的那一行做，否则「输掉创建竞态」
反而成了获得写权限的路径。

KB 不一致用新的 `KnowledgeBaseMismatchError` 拒绝（409）。它不是授权失败——调用方
确实是 owner——而是一个与已提交事实矛盾的断言：接受它会让 document 行停在 KB-A，
而 outbox 事件告诉索引 KB-B。沿用 `UploadVerificationError` 的既有做法复用
`invalid_tool_input` 错误码，**没有新增领域错误词汇**。

拒绝一律是 404，与「不存在」「别的 tenant 的」完全同形。

回归测试 17 条。新增 `tests/api/test_upload_authorization.py`（10 条）**固定
tenant、只换 principal**，并且**故意把 upload id 和 document id 交给攻击者**——
那正是要防的处境，id 会出现在日志、URL 和工单里，「难猜」不是授权规则。另有
7 条落在持久化层，因为读规则目前没有 HTTP 面。

**验证过是有牙的**：撤掉 upload owner 检查失败 3 条，撤掉 document owner 检查
失败 3 条，撤掉 KB 检查失败 2 条，三者全撤失败 8/10（通过的 2 条正是对照组），
撤掉读授权失败 3 条。并发竞态那条单独验证过：把写授权挪到条件插入**之前**，
它连续 6 次全部失败。

写这些测试时改掉了自己两条虚的断言：一条下载 owner 原来的 artifact 来证明「内容
还在」——但接管会写**新的** artifact 并把文档指过去，原 artifact 两种情况下都还在，
所以那条断言恒真；改成直接查库断言版本数、revision 与 digest。另一条只断言 404
正文里没有文件名，而放行时正文本来也没有文件名，同样恒真；改成状态码和正文一起断言。

**已知残留**：`document_id` 由调用方指定，因此邻居仍可通过「用别人的 id 得到
404、用新 id 得到 201」区分出某个 document id 是否存在。消掉它要改成服务端铸造
document id，那是 API 形状变更而不是授权变更，不在本 PR。P1-2（Artifact 对象
授权）与 P1-3（相同内容重传忽略 ACL 撤销）仍然开着，各自一个 PR。

## P1-3 相同内容重传时的 ACL 调和

状态：**已实现并通过本地测试（含真实 PostgreSQL）**。核验报告 §4 的 P1-3。

digest 相同的那条提前返回分支现在先调和 ACL：授权集合有变化时，原子地替换 ACL 行、
推进 revision、写一条 `acl_changed` 事件，然后仍然返回既有 version。

**重传相同内容正是表达「同一份文档、换一批读者」的方式。** 此前这条路径直接返回
旧 version，ACL 行一次都没碰，也不发事件——「内容不变、撤销某人授权」于是完全
不生效，而且是静默的：索引会继续把文档答给一个 owner 已经取消授权的人，直到有人
碰巧上传了不同的字节。`acl_changed` 从 PR-013 起就定义在 `OutboxEventKind` 和
数据库 CHECK 约束里，一直没有写入方——和 P0-2 的 `PermissionRequested` 同一种形状。

**授权变更要占一个 revision。** 消费者按每文档一个单调计数器排序事件；ACL 事件与
内容事件若能共用 revision，乱序到达就无法与重复到达区分。代价是 version 行在
revision 空间里变稀疏（`[1, 3]`，2 是一次授权变更），这是对的：version 记录内容，
那次 revision 没有改变内容。因此顺带订正了两处 docstring——原来把「不推进 revision」
写成了无条件的幂等性质。

比较集合而非序列，所以重排授权列表不算变更；内容与 ACL 都没变时仍然什么都不做。

回归测试 7 条，全部在持久化层。撤销的效果目前**没有 HTTP 面可观测**（没有 GET
document 路由，artifact 下载的对象授权还是 P1-2），所以没有放形式上的 HTTP 测试。

**验证过是有牙的，而且是双向的**：撤掉调和失败 4 条；只撤掉「集合未变则直接返回」
那道早退失败 3 条，其中一条是原有的幂等测试。第二个方向是必需的——没有它，
「每次重传都推进 revision 并发事件」这种过度实现同样能让前 4 条变绿。

## P1-2 Artifact 对象授权

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-2，§6 修复顺序第 3 项的后半。

`ArtifactStore.put`/`put_stream` 记录 `owner_id`，`get`/`head` 接收 `principal_id`。
artifact 归存储它的 principal 所有，其他人一律 not found——与「不存在」「别的
tenant 的」三者同形。此前同 tenant 的任何人只要知道 artifact id 就能下载，而 id
会出现在 tool result、事件 payload 和 URL 里；UUID 难猜不是授权。

**owner 放在存储自己的元数据里，不放进 `ArtifactRef`。** 这是这条唯一需要设计
判断的地方：`ArtifactRef` 随消息和事件流转，而「谁能读这些字节」是被存对象的属性，
不是指针的属性——指针若携带它，等于把答案连同问题一起发出去。附带好处是领域模型
没变，golden 基线和 `DOMAIN_SCHEMA_VERSION` 都不受影响。

**没有按审计原文走 PostgreSQL。** 审计写的是「在 PostgreSQL 持久化 owner/对象
关系」。`artifacts` 表虽在 schema 里却至今没有写入方，`LocalArtifactStore` 一直用
blob 旁的 sidecar；把授权搬进数据库会让文件存储适配器依赖数据库引擎，而那张表按
其模块 docstring 的说法还没「挣得自己的位置」。记进存储自身的元数据，对未来的 S3
适配器是同一形状（对象元数据），也不引入这层依赖。这是有意偏离修复方向，不是遗漏。

sidecar 改为带 `format` 标记的信封，不认识的信封视为不存在。

回归测试 20 条：共享契约 6 条 × 2 个 store（in-memory 与 filesystem 都跑，规则对
每个后端都被钉住）、sidecar 专属 5 条、HTTP 面 3 条。

**验证过是有牙的**：同时撤掉两个 store 的 owner 检查失败 10 条（跨 store、跨层）；
撤掉 `format` 兜底失败 1 条；撤掉 `isinstance` 兜底失败 1 条——每道防线各有一条
测试专属于它。写的过程中又抓到自己一条恒真断言：原本用集合收集三种拒绝的文案并
断言集合只有一个元素，但被放行的那次什么都不加，集合照样只有一个元素；改成按顺序
记录三次结果，放行记 `"allowed"`。

**已知残留，是明确的功能缺口而不是疏漏**：artifact 归存储它的 principal 所有，
**文档 ACL 触及不到它**——没有东西能把 artifact 反查回引用它的 document version。
于是被授权读文档的 principal 看得到文档存在，却下载不了字节。fail-closed 且不完整，
顺序如此。已用 `test_a_read_grant_does_not_yet_reach_the_bytes` 把这个限制钉住，
反查落地时必须**有意地**改掉它；补齐需要 `document_versions.artifact_id` 上的索引
和一个新的 DocumentStore 方法，属于独立变更。

P1-4（下载整体读入内存，不是真正的分块流式）仍然开着，就在同一条路径上。

## P1-5 Runtime 预算硬上限

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-5，§6 修复顺序第 4 项。
**审计的四个阻断面到此全部关闭。**

三处独立的行为变化，共同的形状是：**上限必须在花费之前生效**。事后记账的不是上限，
副作用已经发生了——和 P0-2 是同一句话，只是发生在调度层而不是授权层。

**一、tool-call 配额在 dispatch 之前预留。** 此前一轮提出 N 个调用就全部执行、之后
才记账；余量只剩 1 而提出 3 个时，3 个 handler 全跑，账上还写 3，比上限本身还大。
现在按余量计算准入数，超出的立即以 `budget_exceeded` 拒绝——仍然发 `ToolProposed`
（模型确实提议了），仍然回答它的 id（每个 id 都欠一个结果）。**只对准入的调用记账**：
因上限被拒的调用不该自己消耗上限。

**二、每回合合并 usage 之后复检预算。** 循环顶部那次检查在回合之前，看不到这一回合
花了什么。于是 `max_total_tokens=1`、模型上报 120 tokens 时 run 报 `completed`；若这
一回合还提议了工具，工具照跑。复检放在错误/取消分支之后、`if turn.calls` 之前，两种
后果同时消掉。

顺带的行为变化：**步数用尽的那一回合，其工具不再执行**。最后一步给了模型，工具结果
已经没有读者，执行它们等于用真实副作用换取会被丢弃的输出。已用测试钉住。

**三、无法度量的成本上限被拒绝。** `cost_micro_usd` 没有任何生产者，成本预算永远停在
0、永远不触发——和 P0-2 的 `requires_approval` 同一种形状，一个只写字段，沉默的方向
恰好是放行。这里**不实现计价器**（按 model revision 的价格表是独立变更），而是拒绝
接受一个执行不了的上限：调用方要的是保证，静默地不提供比说出来更糟。引入计价器的那个
PR 负责删掉这个分支。

**明确的残留**：token 上限仍然只能在模型调用**之后**核对——一次调用花多少 token，
调用前不可知。所以单次调用可以冲过上限、随后 run 立刻失败。上限约束的是 run 走多远，
不是单次 provider 调用返回多少。计价器仍未实现。

回归测试 10 条（4 + 3 + 1 + 2，含 3 条对照）。**验证过是有牙的**：撤掉配额预留失败
3 条、撤掉回合后复检失败 3 条、撤掉成本拒绝失败 1 条、把记账改回按提议数失败 1 条。

值得记一笔：改完之后既有 610 条测试**一条都没红**。审计说的「预算测试没有覆盖越界
批次」正是这个意思——现有两条上限测试的预算都恰好卡在整数倍上（`max_tool_calls=4`、
每轮 2 个调用），从没构造出越界的那一批，所以这一整类缺陷全程绿灯。

## P1-6 / P1-7 Gateway 的尺寸上限与时间界

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-6 与 P1-7，§6 修复顺序第 5 项的
一部分。两条各自一个提交。

**P1-6：Policy 改写受参数字节上限约束。** `authorize()` 的改写分支从 `_validate()`
改为 `_check()`——与原始参数、与 Hook 改写同一道检查。此前 Hook 改写走完整检查而
Policy 改写只重跑 schema，于是 `max_argument_bytes` 约束的是模型能发多少、不是
Policy 能替换成多少：上限 64 字节时一次改写把 10,000 字节送进 handler，run 正常
完成。schema 说 `query` 是字符串，没说一万个字符不行。3 条测试，撤掉失败 2 条。

**P1-7：Policy 与 Hook 受 run deadline 约束并 fail closed。** 三处：

1. Policy 引擎此前完全无界——部署方提供的代码坐在每次调用的必经路径上，卡住就把
   run 拖过它自己的 deadline。现在界是 `min(gateway 自带 policy 超时, run 剩余)`。
   gateway 保留自带默认值（5 秒），这样调用方忘记传剩余时间时**仍然有界**；默认成
   None 才是同一类 fail-open。
2. Policy 抛出的异常归一化为拒绝。**复现时发现比审计写的更严重**：异常直接逃出
   `authorize()`，调用方拿到 traceback 而不是终态结果，违反 `AgentExecutor` 的协议
   约定；而且正文原样带出——复现消息里就有 `dsn=postgres://u:sk-ant-canary@h/db`。
   现在只有异常**类型名**过界，与 Hook Bus 早已遵守的规则一致。
3. Hook 取 `min(自身超时, run 剩余)`，逐个重算。此前每个 hook 都持有完整超时，只剩
   2 秒的 run 仍可能在一个 hook 里花 5 秒——本该结束 run 的 deadline，恰恰是 run
   唯一管不住的东西。剩余为 0 时不启动任何 hook 或 policy 调用，并拒绝该次调用。

拒绝理由里带上**是哪一个界耗尽的**，因为两种后果相同但指向不同的排查方向——和
`ModelCallDeadline.source` 同一个判断。

9 条测试（gateway 6 + hook bus 3，含 2 条对照）。**有牙验证有一点特别**：撤掉
policy 归一化后，能终止的 3 条失败，另外 2 条**永远挂住**（后台跑 8 秒未结束才强
杀）——无界等待正是缺陷本身，它表现为挂起而不是断言失败。hook 那侧同理：撤掉后
1 条失败、1 条挂 30 秒。

## P1-8 重复 tool_call_id 在派发前被拒绝

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-8。

两处行为变化：

1. **模型 turn 完成后、准备/授权/执行之前检查 ID 唯一性。** `tool_call_id` 就是
   「结果回答的是哪一次调用」本身；两次调用共用一个，就无法说哪个结果属于哪一次。
   此前只有 `align_results()` 会发现，而它跑在 handler **之后**——模型重复一个 ID，
   工具就按重复次数各执行一次，run 再死在记账上。复现确认**两次 handler 调用**。
   现在整轮失败、handler 零调用。唯一性是**每轮**的：ID 只需在同侪之间可区分，
   跨轮复用照常执行（有测试钉住）。
2. **`align_results()` 的失败归一化为终态 outcome。** 复现确认了审计的补充：
   `ToolPairingError` **逃出了 `run()`**，调用方拿到 traceback 而不是终态
   `AgentOutcome`——违反 `AgentExecutor` 协议约定，Graph node 因此拿不到可记录、
   可路由的结果。与 P1-7 里 policy 异常逃逸是同一种形状。

5 条测试，含 2 条对照。**关于那条 backstop 需要说明**：预检让重复 ID 永远到不了
`align_results()`，所以第二处修改在正常路径上**不可达**。不可达而又没人跑过的分支
是主张不是保证，所以测试里放了一个故意坏掉的 gateway（返回带错误 `tool_call_id`
的结果）实际走一遍——生产代码里没有任何东西会这么做。

**验证过是有牙的**：撤掉预检失败 2 条，撤掉 backstop 失败 1 条，两处各有专属测试。

## P1-9 DeepSeek SSE frame fail closed

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-9。

三处行为变化：

1. **无法解码的 `data:` frame 结束整条流**（此前静默跳过）。跳过不是中性的：工具
   参数按片段拼接，从中间丢掉一片，剩下的仍可能拼成一份**完全合法的 JSON**——
   一个模型从未发出的调用，带着没人选过的参数。已复现：插入一个损坏 frame 后，
   handler 拿到 `{"document_id": "doc_SAFE"}`，而模型本意更长。注释行、空行、
   `[DONE]` 仍然跳过，它们本来就不携带数据。
2. **领域校验错误归一化为终态 error。** `BoundedText` 限制单个 delta 为 4096 字符，
   超过时构造 `ModelTextDelta` 抛的 `ValidationError` **直接逃出了 `ModelPort`**。
   provider 自己的限制不是本进程的契约，Port 的调用方不该拿到 Pydantic traceback。
3. **累积工具参数有上限**（默认 256 KiB 字符，构造参数可调）。那是这里唯一随
   provider 发送量增长的东西。不新增配置项，与 gateway 的 `policy_timeout_seconds`
   同一种做法。

**有一条测试把缺陷本身写成了预期行为**：
`test_unreadable_frames_are_skipped_rather_than_fatal`，断言的正是要修掉的东西，
一直是绿的。已替换，旧名字与这段历史记在新测试的 docstring 里。这比 P0-1 那种
「守错对象」更直接——不是守护了错误的对象，而是守护了错误本身。

8 条测试（含 2 条对照：注释与空行仍然跳过、上限之内的参数照常拼装）。**验证过是
有牙的**：撤掉 frame fail-closed 失败 3 条，撤掉 `ValidationError` 归一化失败
1 条，撤掉参数上限失败 1 条。

## P2-1 DeepSeek 可靠性配置接入 Adapter

状态：**已实现并通过本地测试**。核验报告 §4 的 P2-1。

`DeepSeekProfile` 增加 `timeout_seconds`、`max_retries`、`tool_calling_required`，
adapter 实际消费它们。此前三者在 Settings 里有定义、有校验，却没有任何消费者——
部署方可以配一个超时或重试次数，两样都不会发生。

**重试语义是唯一需要判断的地方。** 流式调用一旦吐出过事件就不能重试：调用方已经看到
的字节收不回来。所以只有**第一个事件之前**的失败可以重试——传输故障与可重试状态
（5xx、429）。`_attempt()` 用 `_RetryableFailure` 表达，`stream()` 是外面的重试循环。
400 之类不重试：请求本来就错，再发还是错。退避按次翻倍，立刻重试 429 等于要求被更狠
地限流；`sleep` 可注入，测试不必真等。

`tool_calling_required` 当时只按“有 tools”发 `tool_choice: "required"`——没有工具却
要求必须选一个，是没人能满足的请求。WP14 在真正给 writer 装配 MCP 后又关闭了一个旧
缺口：required 现在只强制开场轮，ToolResult 后回到 auto，否则最终回答轮会被迫再次调用。

Settings → `DeepSeekProfile` 的投影仍不存在，因为 DeepSeek 还没装配进进程
（WP02-06/07）。本条修的是 adapter 侧的配置语义对齐。

9 条测试（含 2 条对照）。**验证过是有牙的**：撤掉 timeout / tool_choice / 可重试状态
信号 / 「已发事件则不重试」守卫，分别失败 1 / 1 / 2 / 1 条。

最后那条一开始**没咬住**：我写的测试用了一个格式损坏的响应体，走的是 P1-9 的坏 frame
路径，到不了那个守卫。换成真正在中途抛 `ReadError` 的响应流之后才成立。
## P2-2 关闭任意可关闭的流

状态：**已实现并通过本地测试**。核验报告 §4 的 P2-2。

`_stream_model` 的 `finally` 从 `isinstance(stream, AsyncGenerator)` 改为一个
`_Closable` Protocol（只要求有 `aclose`）。

`ModelPort` 承诺的是 `AsyncIterator` 而不是 `AsyncGenerator`，所以一个返回其它
可关闭迭代器的 adapter——比如包着一个必须释放的连接的那种——**从来没有被关过**。
这类泄漏表现为压力下连接耗尽，离真正出问题的那行很远。`aclose` 才是协议，
`AsyncGenerator` 只是最常见的满足者。

2 条测试（含 1 条对照：没有 `aclose` 的流不能让 run 出错——关闭是「能则关」，
不是「必须有」）。**验证过是有牙的**：改回只关 `AsyncGenerator` 失败 1 条。
## P1-4 Artifact 分块流式下载

状态：**已实现并通过本地测试**。核验报告 §4 的 P1-4。

`ArtifactStore` 增加 `iter_chunks()`，两个 store 各自实现，下载路由改用它。此前
`Path.read_bytes()` 把最多 100 MiB 读完再包进 `StreamingResponse`——名义流式，
峰值内存是每个并发下载一整个对象。

`iter_chunks()` **不是 `async def`**：授权必须在调用时发生，而不是拉第一块时。
只在首次迭代才拒绝的协程会让路由先承诺 200、再发现无可发送，那对客户端与网络中断
无法区分。

7 条测试（共享契约 5 条跑两个 store，HTTP 面 2 条）。

**HTTP 那条写了三遍才对**，前两遍都是恒真断言，都在有牙验证时被自己抓出来：
内容小于默认分块（一片本就正确）→ 换成大于两个分块；httpx `ASGITransport` 把响应
缓冲成一片（两种实现都报 1）→ 改成直接驱动 ASGI 数 `http.response.body`；数所有
body 消息（Starlette 总补一条**空**终止消息，单次整体 yield 也是 2 片）→ 只数非空片。

**验证过是有牙的**：路由改回「整体读入 + 单片 yield」失败 1 条，撤掉 `iter_chunks()`
授权检查失败 1 条。
## P1-10 Outbox lease 与 fencing

状态：**已实现并通过本地测试（含真实 PostgreSQL 与 Alembic 往返）**。核验报告 §4 的
P1-10，也是审计要求「在真正启用 ingestion worker 之前」完成的一条。

claim 成为**租约**并带 fencing token。Alembic `0003` 给 `outbox_events` 增加
`lease_until` 与 `claim_token` 及按到期时间的部分索引。

**为什么单有到期不够。** 一个只是卡住的 worker（长 GC、网络分区）在租约到期时仍然
活着。它回来后会 ack 一个**另一个 worker 此刻正在处理**的单元，把别人的在途工作标记
为完成，而别人真正做完的结果反而像重复劳动。所以每次 claim 铸一个 token，ack 必须带
当前的那个。

`StaleExecutionError` 从 PR-003 起定义、docstring 明写用途，一直**没有生产者**——
第三个这种形状（前两个是 P0-2 的 `PermissionRequested` 与 P1-3 的 `acl_changed`）。

到期一律读**数据库时钟**：两个 worker 对时间的分歧正是同一租约被握两次的方式。
ack 靠 **rowcount** 判断，因为匹配不到任何行的 UPDATE 是成功的。

迁移会释放迁移前已 claim 未 ack 的行——它们没有租约也没有 token，既不可回收也不可
ack，正卡在这次修复要消除的状态里。释放是安全的：未 ack 的事件本就是欠着的工作，
最坏是被重复应用一次，而 ingestion 侧无论如何必须幂等。

**仍缺 heartbeat**：诚实做慢活的 worker 无法延长租约、会丢掉它。那属于 ingestion
worker 本身；在它存在前，租约应设得比最慢的工作单元更长。已写进模块 docstring。

6 条测试（含 2 条对照）。**验证过是有牙的**：撤掉过期回收失败 3 条，撤掉 fence
失败 2 条。

## PR-016 Dense Retrieval Kernel（WP04 起步）

状态：**已实现并通过本地测试（含真实 Qdrant）**。WP04-04 与 WP04-05 的索引侧；
只开内部 Port，**不注册任何外部 Chat/RAG 路由**——按计划 §9，检索接口要等
PR-017 的 ACL 二次重验落地后才对外可见。

新增 `ports/vector_index.py` 与 `adapters/vector/qdrant.py`：collection schema、
幂等 upsert、带 tenant / knowledge-base / ACL 过滤的 dense 搜索、按文档删除。

三个判断写在代码里：

1. **point id 是 chunk id 的 UUIDv5。** Qdrant 只收无符号整数和 UUID，而
   at-least-once 投递配上生成式 id，等于每次重试都堆一份近似重复，检索会把它们
   一起返回。稳定 id 才让重投是幂等的。
2. **过滤在查询语句里，不在返回之后。** 先取 limit 再在 Python 里筛，会返回比
   请求更少甚至为空的结果——取决于邻域碰巧怎么排——而且把租户边界挪进了调用方。
   边界和计数属于同一条语句。有一条测试专门钉这个：两个不可见 chunk 比可见的更
   靠近查询向量，limit=1 时必须返回那个可见的。
3. **payload 过滤是缩小候选，不是授权。** 模块 docstring 明写：真正的授权在
   PostgreSQL，Qdrant 返回后还要按 document/version 重验（PR-017）。把派生副本
   当权限权威，正是陈旧索引变成数据泄漏的方式。

**没有承诺 revision 顺序保证。** Port 里我一开始写了「旧 revision 不覆盖新的」，
随即改掉了：Qdrant 没有条件写，任何检查都是 read-then-write，会输掉它声称能赢的
竞态。顺序属于 ingestion worker 的单写者锁（WP05-07），这里只记录 revision 供那
套协议比较。

**测试基础设施**：CI 的 postgres job 改名为 `stateful`，加了 Qdrant 服务容器
（按 digest 固定，与其它镜像一致）。Qdrant 镜像不带 shell，容器级 health check
用不了，改在 runner 上轮询 `/readyz`。无服务时跳过 93 项并显式报出，不是静默跳过。

14 条测试，全部对真实 Qdrant 跑。**验证过是有牙的**：撤掉 tenant 过滤失败 1 条，
撤掉 knowledge-base 过滤失败 1 条，撤掉 ACL 过滤失败 2 条，撤掉维度校验失败 1 条。

依赖新增 `qdrant-client`；许可证 allowlist 相应扩了三个既有宽松条款的不同拼写
（numpy 的复合串、protobuf 的 `3-Clause BSD License`），做法与 PR-012 引入 httpx
时一致。

## PR-016b 摄取管线：解析、切块、嵌入

状态：**已实现并通过本地测试（含真实 Qdrant）**。WP04-01/02 与 WP04-03 的确定性
部分；真 BGE-M3 与 PDF 各自独立 PR。

一个文档版本进去，一批可检索的 chunk 出来：解析 → 切块 → 嵌入 → 写入。

**先全部嵌入，再写任何东西。** 模型调用中途失败时，索引里不能留下半个版本——
半索引比未索引更糟：检索会找到存在的那一半，然后当作全部来回答。

**chunk id 是推导的，不是生成的。** 同一版本、同样切法，每次必须落在同样的 id 上，
否则重建索引会在旧 chunk 旁边再写一份。推导里包含决定「一个 chunk 是什么」的全部
东西：版本、序号，以及索引身份（嵌入器 + 切块器）。换了嵌入器，id 就变——因为向量
不再可比，旧的点也不再是答案。

**token 计数器的名字参与索引身份。** 这是这个 PR 里唯一真正的取舍。窗口按 token
度量，而由谁来数决定每一条边界落在哪。同一个名字下存在两种切法，就意味着重建索引
会悄悄移动所有边界，而按旧偏移建立的引用会指向已经不在那里的文本。所以
`ApproximateTokenCounter` 的名字明确写着 approx，将来换成模型自己的 tokenizer 是
一次**可见的重建**而不是缓慢漂移。

**偏移是记录的，不是回头搜索的。** chunk 的字符区间在切的时候就记下来；靠搜索
chunk 文本来定位，遇到重复段落会把每一份都定位到第一处。有测试用重复六遍的句子
钉住这一点。

**解析器拒绝它读不了的东西，而不是凑合读。** 非 UTF-8 用替换字符解码出来的文档，
一样能切块、嵌入、检索得有模有样——那会得到一个建立在没人写过的文本上的回答，而
每一层都报告成功。PDF 没有放进来：它是一个依赖，也是一整类自己的失败模式
（加密、只有扫描图层、生产者对页边界的分歧），塞进 `bytes.decode` 旁边等于把这些
全藏起来。

**架构守卫抓到一次真实的分层违规。** 最初 `application/ingestion.py` 直接 import
了 `adapters/ingestion`，守卫报错。它是对的：解析迟早要带库，所以应该是 Port；
切块是纯逻辑，属于 application；token 计数器是 Port + adapter。重构后
`application` 只依赖 `ports`。

`qdrant-client` 从 `>=1.12,<2` 收窄到 `>=1.12,<1.13`：CI 里按 digest 钉死的服务端
镜像是 1.12.4，而锁文件解析到了 1.18.0，客户端每次连接都会警告主次版本不匹配。
服务端的 digest 钉死是有意的供应链决定，所以对齐客户端而不是反过来——今天只是警告
的不匹配，明天可以是真的协议不兼容。

测试 41 条：切块器 11、解析器 6、确定性嵌入器 9、端到端接真实 Qdrant 9（另有既有
Qdrant 索引测试）。无 Qdrant/无库时 619 passed、102 skipped，跳过数显式报出。

## PR-016c BGE-M3 dense EmbeddingPort

状态：**已实现；adapter 逻辑在所有环境下有测试，真模型契约需要本地权重**。
WP04-03。

真 BGE-M3 dense adapter，放在**可选依赖组** `embedding` 里。

**为什么是可选的。** 运行时加权重合计几个 GB，而除了嵌入契约本身，其余每一道门禁
都不需要它们。所以 CI 不装：跑得快、离线，且计划 §12「离线测试不调用真实 BGE 依赖」
那条门禁继续成立。代价写在下面。

**导入是惰性的。** 模块顶层 `import torch` 会让没装 extra 的人整个包都不能用，所以
导入发生在加载函数内部。缺依赖时报的是一句能照做的话（`uv sync --extra embedding`），
而不是 `ModuleNotFoundError`。

**只做 dense。** BGE-M3 也能出 sparse 和多向量，但暴露那些的库（FlagEmbedding）
接口更宽也更不稳定。sparse 是 WP05-01；为一个还没有消费者的能力现在就引入更重的
依赖，正是一个项目积累起自己解释不了的依赖的方式。

**类型不依赖那个可选库。** pyright 在 CI 里看不到 `sentence_transformers`，所以
adapter 针对一个本地 `SentenceEncoder` Protocol 编写——只声明实际用到的两个方法。
顺带把接口写清楚了：那两个方法一旦变化，就是这个 adapter 必须察觉的变化。

**维度不匹配在加载时就拒绝。** 模型输出宽度与 `rag.embedding.vector_size` 不一致
时，它写不进按那个宽度建出来的 collection。Qdrant 最终也会拒绝 upsert，但在启动时
失败才能指出原因，而不是等到第一个文档。

**`encode()` 放在工作线程里。** 它是同步且计算密集的；直接 await 会让整批嵌入期间
事件循环停摆，在共享进程里意味着其他所有请求排在一个文档后面。真正该用的有界执行器
属于协调工作包，`asyncio.to_thread` 是诚实的过渡，且严格优于阻塞循环。

**测试分两档：**

- **12 条在任何环境都跑**，用一个满足 Protocol 的 stand-in encoder：批大小是否传到
  模型、是否要求归一化、顺序是否保持、空批不调用模型、身份是否含 revision、维度
  不匹配/不报告维度/批大小非正各自被拒（含 1 条对照：宽度正确时正常加载）、缺依赖
  时的报错文案。
- **4 条需要真权重**，由 `AGENT_WORKBENCH_TEST_EMBEDDING_MODEL` 控制：实际输出维度
  等于配置（计划明确列出的门禁）、单位向量、确定性、以及**相关文本比无关文本更近**
  ——最后这条是 stand-in 永远不可能有的性质，也是真跑一次的理由。

跳过时**显式报出原因和条数**，不是静默跳过。

写这些测试时又抓到自己一个问题：最初的 `_load_with` 辅助函数**复制**了 `load()` 的
检查逻辑，等于在测我自己的副本而不是真代码——正是这份文档里反复出现的「守错对象」。
改成给 `load()` 加一个可注入的 `loader` 参数，测试驱动的是真正的检查。stand-in 也
有过一个缺陷：它用 `None` 当「沿用 dimension」的哨兵，导致「模型真的返回 None」这
一路径无法表达，被一条测试的失败暴露出来。

**真模型运行证据（2026-07-26，本地）**

```text
BAAI/bge-m3 @ main, sentence-transformers 5.6.1 / torch 2.13.0, CPU
首次运行（含权重下载）：16 passed in 981.62s
修复后重跑（权重已缓存）：19 passed in 81.61s，无告警
```

四条真模型断言全部通过：输出维度 = 配置的 1024、单位向量、确定性、相关文本比无关
文本更接近查询。

**真跑抓到一个 stand-in 结构上不可能发现的问题。** 首次运行报
`FutureWarning: get_sentence_embedding_dimension 已改名为 get_embedding_dimension`。
依赖范围 `>=3.3,<6` 跨越了两种命名：旧版本只有旧名，新版本两个都有但旧名告警。
假 encoder 只会响应写它时用的那个名字，所以对它测多少次都不会暴露真实库改过名——
这类问题只有真加载一次模型才会出现。已修：新增 `reported_dimension()` 按新名→旧名
询问；`SentenceEncoder` Protocol 里**故意不声明维度访问器**，因为在 Protocol 里点名
其中一个会让另一个版本无法满足它。补了 3 条测试覆盖两种命名与都没有的情况。

**能力表怎么标。** dense embedding 标为 Implemented + Tested，但**证据来自本地而不是
CI**：CI 不装可选依赖，所以那 4 条在 CI 里始终跳过。README 与简历写「BGE-M3 dense
retrieval 已验证」时必须同时说明证据是本地运行的，不能指向一条 CI 链接。

## PR-017 Authorized RAG Slice

状态：**已实现并通过本地测试（真实 Qdrant + 真实 PostgreSQL）**。WP04-06、WP04-10。

**索引负责缩小候选，PostgreSQL 负责授权。** 这是两件不同的事，这个 PR 就是把这个
区别落成代码。点的 payload 记录的是「上次索引这个文档时的 ACL」，而「上次索引」
不等于「现在」——一秒钟前撤销的授权还没到达索引，索引会照样把 chunk 返回给那个
刚被拿走权限的人。

**每个候选在成为 context 之前都要对 PostgreSQL 重验。** 不是排序之后，不是组装
citation 的时候，而是之前——因为下游每一步都是文本外泄的途径：进入 rerank 意味着
被模型读过，进入 citation 意味着被用户看到。

**授权检查两次，第二次不是冗余。** 构造 context 和提交答案之间隔着一次模型调用，
授权可以在这中间被收回。每个文档被授权时的 revision 一路带下去，来源变动的答案
被拒绝而不是交付。

**比较 revision 而不是重问「我还能读吗」**，是因为前者能抓到**被替换过的 ACL**：
撤销再重新授予，重问会说「能读」，而 revision 说 ACL 变过——这正是答案所依据的
东西。P1-3 让授权变更和内容变更一样推进 revision，这条才成立。

### barrier 测试

WP04 的退出条件明确要求：在 Qdrant query 完成后、context 构造前提交 ACL revoke，
被撤权的 chunk 不得进入 rerank、模型上下文、回答或 Citation。

**实现方式：不在生产代码里开测试专用钩子。** 测试包装 `VectorIndexPort`，在
`search()` 里调用真实实现后、返回前执行撤权。这精确等于要防的那个时间窗，确定性
（不依赖 sleep，符合 `deterministic_concurrency_required`），且生产路径完全不知道
自己正在被交错。

9 条测试，含 3 条对照（owner 不受影响、未变更时确认通过、陌生人本来就拿不到）。

**验证过是有牙的**：
- 把重验换成「信任索引的 ACL 过滤」——这是真实会犯的错误写法——失败 2 条，且是
  **断言失败**（被撤权的 chunk 真的进了 context），不是崩溃；
- 撤掉答案提交前的第二次检查——失败 2 条。

第一次尝试有牙验证时我用的破坏方式会让代码 `KeyError` 崩溃，那不是忠实的模拟：
真实的错误是「有人删掉 PostgreSQL 重验、改为信任 payload 过滤」，而那种写法不会
崩。改成忠实的版本后，测试是靠断言抓到泄漏的。

### 尚未包含

Chat 切片（WP04-07/08/09：ChatService、`knowledge_search` Tool、REST/CLI）不在本
PR。按计划纪律，**外部 Chat/RAG 路由要等授权链路完整后才注册**——本 PR 补齐的正是
这个前提，但路由本身属于下一个 PR。

Citation 目前用 chunk ordinal 作为 paragraph 定位。字符级 offset 在切块时已经算出
并保存在 `TextChunk.locator` 里，但还没有穿过索引 payload 传到检索侧；页码需要 PDF
解析（同样未实现）。这是**已知的定位精度缺口**，不是遗漏。

## PR-018 固定 2-step ChatService

状态：**服务层已实现并通过本地测试（真实 Qdrant + 真实 PostgreSQL）；HTTP 路由
未注册**。WP04-07 的服务部分。

一轮：检索一次 → 把 `ContextPacket` 作为**引用材料**交给模型 → 交付前再次核验来源。

**「模型不决定是否检索」写成了权限，不是意图。** 工具清单为空，envelope 是
deny 形状的默认值（空 allowlist 什么都不允许）。这样固定 2-step 和 agentic 两条
路径可以分开评测——同一个问题每次以同样方式检索，答案的变化就只能来自模型或语料。

**检索到的段落不是指令。** context 以引用材料的形式进 prompt，system prompt 明说
这一点：一份写着「忽略你的指令」的文档，是一份**写着**那句话的文档，不是一份**执行**
那句话的文档。

**答案被扣下时，进历史的是拒绝说明而不是答案。** 存下答案会把被扣的文本留在下一轮
读得到的地方。

8 条测试，含 withheld 路径（撤权发生在模型已写完答案之后）与「邻居不能借 session id
向别人的会话提问」。

### 两个前置缺陷，都是做这件事时撞出来的

**会话 IDOR。** `ConversationStore.append`/`history` 只按 tenant 限定，同 tenant 里
任何知道 session id 的人都能读走整段对话、还能注入消息——owner 下一轮会把注入内容
当成自己的历史读回来。这是**会话版的 P1-1**。先修它再谈路由，是 PR-017 同一条排序
纪律：授权链不完整时不发布接口。6 条回归测试跨两个 store。

**我自己在 #22 引入的预算缺陷。** 我把 `stop_reason_for`（「我还能再开一步吗」，
docstring 明写「在 turn 之前评估，绝不在之后」）复用成了 turn 之后的超限检查，导致
`max_steps=N` 在完成路径上表现得像 `N-1`：模型在允许的一步内答完，答案被丢弃、run
报失败。**是写 chat 时撞上的**，因为 chat 的一轮恰好就是一步。

现在两个问题分开：`overrun_reason_for`（严格大于）无条件在 turn 后跑——审计的 token
场景需要它，因为一次调用要花多少 token 在调用前不可知；`stop_reason_for` 保持原义，
只在模型**提了工具**时再问，那才是「即将开始更多工作」。所以答完的 run 完成，想继续
但配额用尽的 run 仍在派发前停（#22 加的行为保留）。各有一条专属回归测试。

### 未包含：HTTP 路由与 CLI（WP04-08/09）

**没有注册路由，也没有假装注册。** 接上路由需要在进程里装配一个模型，而 DeepSeek
Adapter 至今没有进程级装配：`ApiRuntimeConfig` 里没有 model/rag/qdrant 投影，也没有
从 Settings 构造 adapter 的代码。那是 WP02-07，独立的一块——要扩投影、装配 Qdrant
客户端与 embedder、处理 API key，并且会让 API 进程从此需要密钥才能启动。

在低余量下草率塞进这样一块装配变更，正是这份文档里反复记录的那类错误的来源。
`knowledge_search` Tool（WP04-08）同理，它属于 agentic 路径，需要同一套装配。

## PR-019 模型装配与配置投影

状态：**已实现并通过本地测试**。WP02-07 的前半：把 DeepSeek Adapter 变成进程能真正
构造出来的东西。

`ApiRuntimeConfig` 新增 `model` 投影（provider、base_url、api_key、两个 profile 及
其可靠性字段），并新增 `bootstrap/model_factory.py` 从投影构造 `DeepSeekModel`。

**这是配置从「承诺」变成「能力」的地方。** 在此之前每一层校验的都是形状——model_id
是非空字符串、base_url 是个 URL——而这些都不说明进程真能触达 provider。

**一个装配不出可用模型的进程，比一个拒绝启动的进程更糟。** 它能通过健康检查、能接
请求、然后每一个请求都在 provider 那边失败——配置错误于是变成一次事故，而症状离
成因隔了好几层。所以缺 key、空白 key、占位 model_id、没有 adapter 的 provider，
四种情况都在启动时拒绝。

**占位 model_id 在所有环境都拒绝，不只 production。** Settings 层的
`_looks_like_placeholder` 只在 production 生效，而committed defaults 里就是
`not-configured-deepseek-main`——一个开发进程「什么都答不上来」，仍然是什么都答不
上来。这是有意与 Settings 层不同的严格度，不是重复校验。

拒绝信息里点名**是哪几个 profile 没有 pin**：一句「有地方配错了」要靠二分才能行动。
API key 不出现在任何拒绝信息里——启动错误会进日志和工单。

7 条测试：四种拒绝各一条、profile 命名一条、key 不泄漏一条，外加一条对照（配置正确
时确实构造出 `DeepSeekModel` 且满足 `ModelPort`）。

### 未包含：路由注册

装配 Chat 路由还差两块：Qdrant 客户端与 embedder。embedder 是个真问题——唯一的真实
实现在**可选依赖**后面，所以给 API 进程装 chat 意味着要么让 API 依赖那个 extra，要么
在 extra 缺席时拒绝装配 chat。后者与本文件里其他每一处的做法一致（拒绝而不是假装），
但那是下一个变更，不是这一个。

## PR-020 Embedder 装配：缺依赖时缺功能，而不是撒谎

状态：**投影与工厂已实现并通过本地测试；HTTP 路由仍未注册**。

`ApiRuntimeConfig` 补上 `qdrant`、`embedding`、`retrieval` 三个投影；新增
`bootstrap/embedding_factory.py`。

**核心决定：缺可选依赖时返回「没有」，而不是替一个假 embedder。**

真实 embedder 需要几个 GB 的运行时与权重。要求整个 API 都装它，会让上传、artifact、
健康检查依赖一套它们从不触碰的机器学习栈；所以没装的进程照常启动，只是不提供 chat。

而另一条路——塞一个 stand-in embedder 好让路由存在——会造出一个用**毫无意义的向量**
回答问题的 chat 接口：检索返回哈希碰巧放在附近的东西，语气笃定，还附引用。
**缺失的功能是可读的，撒谎的功能不是。**

所以工厂返回 `EmbeddingUnavailable` 而不是抛异常：缺席是预期状态，调用方据此不注册
chat 路由，原因在启动时报一次，而不是每个请求各发现一遍。

**宽度不匹配是例外，它抛异常。** 那不是缺席，是配置自相矛盾——照常启动会建出一个
谁都写不进去的 collection。这不是任何人选择的状态。

6 条测试（含 2 条对照）。loader 可注入，与 `BgeM3Embedder.load` 同一个模式：测这几个
分支不该先下载两个 GB——加上之后这组测试从 48 秒降到 0.36 秒。

### 顺带修掉我自己提交的两个游离文件

`git add -A` 在 #35 里扫进了 `bge 2.py` 和 `test_bge_embedder 2.py`——编辑器在我做
`reported_dimension` 修复**之前**留下的副本。

**重复的测试文件才是问题所在**：pytest 收集了它，16 条全部通过——它们测的是旧的访问器
命名，而现在的代码对旧名做了回退。于是没有任何东西报错，测试总数被报成 781，而实际
只有 772 条不同的测试。**一个会跑、还会同意的副本，比一个会坏的副本更糟**，因为它掩盖
了树里存在同一文件的两个版本。

新增守卫拒绝 `thing 2.py` / `thing copy.md` / `thing (1).py` 这类名字，并配一条测试
确认该模式匹配这些形状、且不误伤 `test_2fa.py` 这样的正常命名，再配一条确认被扫描的
清单非空。放回一个副本会让它失败。

## PR-021 Chat 装配进 API 进程（路由仍未注册）

状态：**装配已实现并通过本地测试；HTTP 路由未注册**。

`build_dependencies` 现在组装完整的 chat 栈——embedder、DeepSeek 模型、Qdrant 索引、
会话存储、`ChatService`——并把结果放在 `ApiDependencies.chat` 上；装不出来时放
`chat_unavailable`，写明原因。

**急切加载是个显式参数，不是惊喜。** 装配 chat 会加载嵌入模型。对服务器来说急切
加载是对的——第一个提问不该付掉后面每个提问都不必付的四十秒。但对只跑上传或健康检查
的东西就是错的，所以 `build_dependencies(config, with_chat=...)`。

这一点是**被自己的测试套件抓出来的**。诊断跑给出了确切数字：修复前 `tests/api`
单目录耗时 **900 秒**，修复后 1.9 秒。
因为我本地装了那个可选 extra，于是每一个构造依赖的 API 测试都在真加载 BGE-M3。
CI 没装那个 extra，所以 CI 不会暴露这个问题——只有装了 extra 的开发者会踩到。

### 又一次停在路由前，这次的原因不同

写到路由时发现：`ChatService.ask` 需要一个 `EventSink`，而**只有内存事件日志存在**，
没有 PostgreSQL 的 EventLog 适配器。接上路由等于让 run 的**持久事件写进一个随进程
消失的内存日志**——而基线明确承诺 durable 事件是持久的。

这和 embedder 那个决定同级：要么先做持久事件日志（属于 WP06/WP12 的事件流与 SSE），
要么明确记下「Chat 的审计线索目前不持久」并接受它。我不在余量不足时替这种决定拍板，
所以停在这里，把它记下来。

3 条装配测试：没有嵌入运行时时进程照常装配（上传、artifact 仍在）、chat 缺席且原因
可读、**不替换任何 stand-in**。

## PR-022 持久事件日志（WP06 的存储层）

状态：**已实现并通过本地测试（真实 PostgreSQL）**。路由的最后一块前置。

新增 `events` 与 `event_streams` 两张表（Alembic `0004`）与 `PostgresEventLog`。

**序列在流行锁下分配，不用 identity 列。** 差别是**空洞**：identity 值被一个回滚的
事务消耗掉就永远不会被写入，于是流是唯一的但满是洞——而**订阅者从 cursor 恢复时，
分不清一个洞和一个还没到达的事件**。持有行让同一个流的追加串行化，这才让
`(stream_id, sequence)` 真的意味着「到此为止的全部」。

**流行在首次追加时创建**，不需要单独声明。一个要求生产者先声明流的日志，其失败模式
只在竞态下出现，而修法反正就是这个条件插入。两个追加同时创建同一个流时，双方都条件
插入、再各自锁住最终那一行——和 document store 同一个形状，理由也相同。

**transient 事件返回但不存储**。给它一个位置，就会让 cursor 跳号；存下来又不给位置，
就无法按序重放——两种都是让 cursor 的含义少于它的字面承诺。

重放时**通过写入它的同一个模型校验回来**，所以一行来自本进程不认识的契约时在边界
上 fail closed，而不是半懂不懂地进入某人的重放。`read` 的 limit 自带上限：重放是
客户端发起的请求，不设限就等于允许别人随时让服务器把整条流读进内存。

时钟可注入，与内存实现一致——和墙钟赛跑的测试会在慢机器上失败。

9 条契约测试**同时跑内存与 PostgreSQL 两个实现**（共 18 条）。**验证过是有牙的**：
去掉 `FOR UPDATE` 后，并发无洞那条连续三次全部失败，且只失败那一条。

**明确没做的**：`event_streams` 没有 tenant 列。流的租户不在 `EventScope` 上，而用
`stream_id` 之类推导出来的值去填，是把占位包装成决定——**存一个错的值比不存更糟**。
等 scope 携带租户时，在需要它的那个变更里加。SSE 端点与 `Last-Event-ID` 恢复属于
WP06 的传输层，不在这里。

## PR-023 Chat HTTP 路由（WP04-09）

状态：**已实现并通过本地测试（真实 Qdrant + 真实 PostgreSQL）**。

三个端点：开会话、提问、读历史。**这是这个项目第一次注册外部 RAG 路由**——按计划
的排序纪律，它等到授权链完整（PR-017）、模型可装配（#35）、embedder 决定做完
（#36/#37）、持久事件日志到位（#38）之后才发布。

**路由只在进程能回答时挂载。** 存在但答不了的路由比缺席更糟：缺席是客户端检测一次
的 404，另一种是每个请求一个 500，外加一串「助手为什么坏了」的工单。

**会话显式创建，不由第一个问题隐式打开。** 隐式创建会让「这是哪段对话」取决于顺序，
而一个重试超时请求的客户端会发现自己在第二段对话里、里面装着一半历史。

**一条 stream 对应一个 session，一个 run 对应一轮**：订阅者跟着对话走、从断点恢复，
而每一轮在其中仍可识别。持久事件因此写进这条 stream——这正是路由等待事件日志的理由。

`withheld` 如实上报，不转成 403。问题是被允许的；变的是某个来源在模型写答案期间不再
可读。能区分这两者的客户端可以提议重新提问，而不是为一个被允许的问题显示访问错误。

12 条 API 测试。**未挂载与已挂载两侧都覆盖**——只测「缺 embedder 时是 404」会是守错
对象：一个从来就不工作的 router 也能让那些断言全绿。已挂载那侧用确定性 embedder 与
脚本模型装配出真的 `ChatService`，不下载任何权重、不联网。

**验证过是有牙的**：把条件挂载改成无条件，「缺 embedder 时路由不存在」失败；撤掉会话
owner 检查，「邻居不能提问」与「邻居不能读历史」两条失败。

**仍未做**：SSE 订阅端点与 `Last-Event-ID` 恢复（WP06 传输层）、CLI chat 切片、
`knowledge_search` Tool（WP04-08，属 agentic 路径）。

## PR-024 SSE 订阅与断线恢复（WP06 传输层）

状态：**已实现并通过本地测试（真实 PostgreSQL）**。

`GET /v1/chat/sessions/{id}/events`：按 SSE 推送该会话的持久事件，客户端用
`Last-Event-ID` 从断点续上。

**订阅就是一次还没结束的重放。** 客户端发回它看到的最后一个 id，服务端发送其后的
全部，然后继续发。所以没有单独的「追赶」路径——重连只是一次从更靠后位置开始的订阅。

**cursor 就是 SSE 的 event id**，浏览器因此不需要客户端自己写任何恢复逻辑。它之所以
成立，完全依赖序列无洞：订阅者分不清一个洞和一个还没到达的事件，有洞的日志会让
「n 之后的全部」这个问题无法回答。这正是 #38 那样实现的理由。

**畸形 cursor 从头开始，而不是拒绝连接。** 它来自可能跨过一次部署的浏览器；拒绝会让
那个客户端完全无法重连，而且无从得知清掉它就好了。**指向别的 stream 的 cursor 被忽略**
——沿用它的数字会静默跳过本流的事件。

延迟由轮询间隔决定。配置里的 `wakeup_backend = "postgres_listen_notify"` **仍然没有
消费者**——这是本仓库第五次出现「定义了却没接上」的形状，所以我在这里明说而不是让
行为去暗示它。`catchup_poll_seconds` 与 `replay_page_size` 都真的被用上了。

11 条测试。**验证过是有牙的**：忽略 `Last-Event-ID` 后，恢复那条失败。

**过程中又抓到自己一条守错对象的测试**：`test_transient_events_are_never_sent` 名字
指向流，实际验证的是日志——`read()` 按契约只返回 durable 事件，所以流里那个过滤是
**不可达的**，删掉它一条测试都不会失败。已改名为
`test_a_transient_event_never_reaches_a_subscriber` 并在 docstring 里写明这是由日志
而非流保证的；生产代码里那个 guard 保留（类型上 sequence 可为 None），但注释直说它
不可达、也没有测试覆盖它。

**仍未做**：LISTEN/NOTIFY 唤醒（延迟仍受轮询间隔约束）、CLI chat 切片、
`knowledge_search` Tool。

## PR-025 检索评测：固定语料、gold set、metric 注册表

状态：**已实现并通过本地测试；真实数字已产出（本地）**。WP05-05 的检索部分，
补齐 WP04 退出条件里的语料与 gold questions。

`evals/rag/` 下是 6 篇固定语料与 **23 条 gold questions**（退出条件要求 ≥20），
每条指名回答它的文档。`src/agent_workbench/evaluation/` 下是 metric 注册表与运行器。

**运行器不知道自己在测哪个检索器。** 这正是消融的意义：dense、hybrid、hybrid+rerank
由同一段代码在同一批问题上打分，两份报告的差异才是检索的差异，而不是计数方式的差异。

**报告记录是什么产生了它**——index identity、gold set 摘要、题目数。没有这些的百分比
与任何东西都不可比；两次跑出不同数字只有在测的是同一件事时才有意义，而这恰恰是一个
裸百分比说不出来的。gold set 的摘要尤其重要：**两次运行之间改 gold set，是最容易凭空
造出「改进」的办法**。

**这里不评判答案。** faithfulness 和引用正确率需要模型在环、属于另一个运行器和另一套
证据；混在一起会让检索回归和生成回归看起来一样。

### 真实数字（本地，2026-07-27）

```text
BAAI/bge-m3@main + approx-word-v1-512-64，23 条 gold questions
recall_at_1  0.957     recall_at_3  1.000
mrr          0.971     中位检索延迟  49.9 ms
```

`scripts/run_rag_eval.py` 产出，报告存于 `evals/rag/reports/dense.json`。**CI 不跑它**：
CI 没有嵌入运行时，而用确定性 embedder 跑出来的报告是在测一个哈希函数。

### 差点交出去一组恒真的指标

第一版用 `top_k=10`，报出 `recall_at_5 = 1.0`、`recall_at_10 = 1.0`。查下来发现：语料
只有 6 篇、每篇 1 个 chunk，**top_k=10 每次都把全部 6 篇取回来**——那两个数字报的是
「10 ≥ 6」这个算术事实，不是检索质量。

这与本文件里反复出现的恒真断言是同一类错误，只是这次出现在**指标**里，而指标恰恰是
用来判断其他一切好不好的东西。已改为 `top_k=3`，并把注册表的 recall 上限停在 3，注释
写明理由：**k 不小于语料规模的 recall 是算术不是测量，一个不可能失败的指标也不可能
报告回归**。

10 条 metric 测试全部对着手算得出的数字，21 条运行器测试含两条对照（完美检索器得 1、
盲检索器得 0）。

## PR-026 BGE-M3 sparse encoder（WP05-01）

状态：**已实现；结构测试在所有环境下跑，真 lexical weights 已本地验证**。

`SparseEncoderPort` 与 `BgeM3SparseEncoder`，走 **FlagEmbedding**（BGE 官方库），
加入既有的 `embedding` 可选依赖组。

### 差点走错的捷径

`sentence-transformers` 5.x 提供了 `SparseEncoder`，项目已经依赖它，看起来不必再引库。
动手前实测：

```text
BGE-M3 词表大小:        250002
SparseEncoder 输出维度:   4096
模块链:  Transformer → Pooling → Normalize → SparseAutoEncoder
```

模型卡没声明稀疏头时，它接了个 `SparseAutoEncoder`——把 1024 维 dense 向量压成 4096 维
稀疏码。**那是对 dense 向量的有损重编码，不是词汇匹配。**

危险在于它不报错：Qdrant 存得下、RRF 融得了、评测出数。那条 sparse 支路根本不做 term
matching，现象只是「hybrid 好像没什么用」——一个会被归因为「这语料上 dense 已经够好」
的结论，而不是「sparse 是假的」。架构基线 8.1 点名警告过这件事，我在 #32 里也写过
「sparse 是 WP05-01，届时引 FlagEmbedding」——**我差点用一个看起来现成的 API 绕过自己
写下的判断**。记为 [ADR-013](./adr/0013-bge-m3-sparse-encoder.md)。

### 唯一能区分真假的断言

加载时强制校验**稀疏维度等于分词器词表大小**。别的宽度都会产生 Qdrant 收得下、融合
用得了、却一个词都不匹配的向量，所以这是唯一能判断这条支路是不是真的的检查。

真模型验证（本地，2026-07-27）：12 passed in 53s，其中三条只有真权重在场才跑——维度
实测 250002；真实词项有正权重且索引都在词表内；两段无关文本（「reciprocal rank
fusion」与「preheat the oven」）加权的词项集合不同，**这是 term matching 才有的性质，
4096 维那个假货不可能满足**。

### 许可证门禁抓到一个真问题

`FlagEmbedding` 的包元数据**没有声明许可证**，pip-licenses 报 `UNKNOWN`——而这道门禁
存在的理由正是拦住未声明许可证。

查证：它的 wheel 里附了 MIT LICENSE，只是缺 License 分类器。**没有把 `UNKNOWN` 加进
allowlist**——那会让未来每一个未声明许可证的包自动通过，门禁就废了。改成用
`--ignore-packages` 按包点名豁免，并把证据写进 CI 注释里：一个包、一行、一个必须有人
写下来的理由。其余新增的都是宽松许可证的不同拼写，照常加进 allowlist。

### 已知重复

dense 走 `sentence-transformers`，sparse 走 `FlagEmbedding`——**这是已知重复，不是设计**。
基线要求的是一次前向同时产出两种表示。若 FlagEmbedding 的 dense 输出被验证与现有结果
一致，dense 应当合并过去；在验证之前不动，因为换 embedder 会改 index identity，那是一次
全量重建索引。

**未做**：Qdrant 单次 dense+sparse RRF 融合（WP05-02）、reranker（WP05-03/04）、
对照 dense 基线（`recall@1 0.957 / mrr 0.971`）的消融报告。

## PR-027 Qdrant 单次 dense+sparse RRF 融合（WP05-02）

状态：**已实现并通过本地测试（真实 Qdrant）**。

collection 现在同时声明 dense 与 sparse 具名向量，`search_hybrid` 用一次
`query_points` 发两条 prefetch 加一个 `FusionQuery(RRF)`。

**融合发生在 Qdrant 内部，这个进程从不同时持有两份排序表。** 基线把融合所有者锁定为
Qdrant Query API，并禁止适配器再做一次 relative-score fusion——而「不做第二次」最可靠
的保证方式，就是让这一侧根本拿不到可以再融的两份列表。

点没有稀疏权重时**不写空稀疏向量**：写了会让它「匹配所有稀疏查询、权重为零」，而不是
「不匹配」。collection 因此可以在 sparse 铺开期间同时容纳两种点。

`_narrowing` 抽成共享定义：dense 与 hybrid 两条路必须用同一份过滤器，两份拷贝就是两次
让某一条路授权得不一样的机会。

### 同一个错误，连续两个 PR

第一版 8 条测试**全绿地通过了「删掉整条 sparse prefetch」**。原因与上一个 PR 那个恒真
指标一字不差：collection 里只有 2 个点，而 `dense_limit=10`——**dense 自己就把两个点都
取回来了**，`doc_lexical` 出现在结果里跟 sparse 毫无关系。

改成每条支路 `limit=1`（小到只够够到自己那个点）后，撤掉 sparse prefetch 失败 2 条。

**教训是同一条**：一个不可能排除任何东西的 limit，也不可能证明任何东西。上个 PR 出现在
评测指标里，这个 PR 出现在测试夹具里。

### 一处写下但未验证的断言，已订正

代码注释里我写过「过滤器必须放在 prefetch 上，否则未授权点会占掉名额」。实测：移到融合
查询上，8 条测试**全部照过**——Qdrant 会把外层过滤器下推。

那句话是推测不是事实。已改为如实陈述：两种写法在这里等价；仍按 per-arm 写，是**不想让
授权保证依赖查询优化器的行为**，但明说了测试区分不了这两种写法。

顺带订正一条旧断言：`test_an_unauthorized_principal_gets_nothing_from_either_arm` 写错了
对象——一个对谁都不返回结果的索引也能满足它。已改成同时断言该 principal **能**看到什么。

8 条测试。**未做**：把 sparse encoder 接进摄取与检索链路，以及对照 dense 基线
（`recall@1 0.957 / mrr 0.971`）的消融报告。

## PR-028 把 sparse 接进摄取与检索链路

状态：**已实现并通过本地测试（真实 Qdrant + 真实 PostgreSQL）**。

`IngestionService` 与 `RetrievalService` 各自新增可选的 `sparse_encoder`。

**没有 sparse encoder 时走 dense，而且不假装自己是 hybrid。** `RetrievalService.mode`
如实返回 `"dense"` 或 `"hybrid"`——评测报告因此不可能把一次 dense 运行标成 hybrid，
消融也不可能把一个检索器和它自己比。这是「缺席要可读」在这条链路上的形态。

**sparse encoder 进入 index identity。** sparse 改变的是**一个点是什么**，不只是它额外
携带了什么：一个半稀疏的 collection 会用一条支路给一部分点排序、用两条给另一部分排序。
identity 不同则 chunk id 不同，两者永不共享同一个点，重建索引因此是看得见的，而不是一次
静默覆盖。

**没有权重时不写空稀疏向量**——空向量会「匹配所有稀疏查询、权重为零」，而不是「不匹配」。

**sparse 与 dense 同样是全或无**：先整批编码再写入。失败若留下「前几个 chunk 匹配词项、
其余不匹配」的版本，那个版本会自己和自己竞争排序。

**两条支路各自拿完整候选数**，不各减半：RRF 的职责就是把两份完整候选收敛成一份，
让它在两份已被截断的列表之间选，是另一个检索器，不是被评测的那个。

8 条新测试。**验证过是有牙的**：摄取侧丢掉权重失败 1 条；sparse 不进 identity 失败 2 条；
`mode` 恒返回 hybrid 失败 1 条。

**未做**：消融报告本身。`scripts/run_rag_eval.py` 还需要接上 sparse encoder 并按 `mode`
命名报告，才能对照 dense 基线（`recall@1 0.957 / mrr 0.971`）回答「hybrid 有没有用」。

## PR-029 dense vs hybrid 消融（WP05-08）

状态：**已跑出真实数字（本地）**。完整报告见
[`evals/rag/reports/ABLATION.md`](../evals/rag/reports/ABLATION.md)。

| 指标 | dense | hybrid |
|---|---|---|
| recall@1 | 0.957 | 0.957 |
| recall@3 | 1.000 | 1.000 |
| MRR | 0.971 | 0.971 |
| 中位延迟 | 52.7 ms | 38136.2 ms |

**结论是「测不出差别」，不是「hybrid 没用」。** 三个质量指标逐位相同。但这份语料只有
6 篇文档、每篇 1 个 chunk，问题措辞与原文高度重合，dense 已经 23 题错 1——**留给 sparse
的空间只有那一题**。持平既可能说明 sparse 无增益，也可能说明语料测不出差别，**用现有
数据区分不了**。把它写成「hybrid 不值得做」，是拿一个测不出差别的实验去支撑关于方案的
结论。

**那个 38 秒衡量的是当前实现，不是方案。** 每次查询重跑一遍 BGE-M3 稀疏编码，CPU 上
无预热、无批处理、无常驻——基线 8.1 要求的这些我都没做。可以用它说「当前实现不能用于
交互式查询」，不能用它说「hybrid 太慢」。

报告里写清了下次消融需要语料补什么：罕见标识符、精确短语、错别字、缩写与全称并存——
**现在这份语料里一个都没有**，所以在补进之前，对比只会继续持平且继续什么都不说明。

脚本改为一次跑两种模式、各自独立 collection（sparse 改变 index identity，共用会让 dense
读到为 hybrid 建的点），报告按模式命名。

## PR-030 扩充语料，并订正上一次消融的归因

状态：**已跑出真实数字（本地）**。报告见
[`evals/rag/reports/ABLATION.md`](../evals/rag/reports/ABLATION.md)。

语料 6 → 10 篇，gold set 23 → 38 题。新增 4 篇各带一类 dense 会漏、sparse 该命中的
把手：罕见标识符（`AWB-4471`）、逐字短语、真实工单里的错拼（`Qrdant`、
`rag.embeding.model_id`）、缩写歧义（RRF / ACL / MRR）。问题按**用把手提问**写，
不用改述——改述提问 dense 照样能对齐，测的就还是原来那件事。

**结果是负的：hybrid 劣于 dense**（recall@1 0.947 → 0.868，MRR 0.961 → 0.917）。

在具备 sparse 该擅长条件的语料上仍然更差，说明问题在 sparse 一侧而非语料。**但这不
等于 sparse 无用**——可能是编码、写入或融合权重，目前没有证据区分，所以报告里不写
诊断。下一步是逐题查 sparse 单独检索的命中情况。

### 我自己写下警告、又亲手违反了它

上一次消融（6 篇）的持平，我归因为"语料太小"。**那个归因不完整**：评测脚本当时给两条
支路各只有 `TOP_K = 3` 个候选，让 RRF 在两份已截断的列表间融合——而
`RetrievalService` 里我写过注释明令禁止这种做法。

6 篇语料下 `limit=3` 几乎是全库，截断的伤害看不出来；扩到 10 篇才显现成负增益。修复后
延迟从 30.3 秒降到 184 毫秒（快 165 倍）——**每条支路只取 3 条却要跑 30 秒，那个数字
当时就该让我起疑**，我却把它归到了「hybrid 的实现代价」上。

两个教训是同一个：一个跨文件重复的策略，注释拦不住，只有让它无法被分别设定才拦得住。

## PR-031 sparse 诊断：查到一半，并撤回一个错误结论

状态：**未定论**，记录见
[`evals/rag/reports/SPARSE-DIAGNOSIS.md`](../evals/rag/reports/SPARSE-DIAGNOSIS.md)。

追查 hybrid 为何劣于 dense。绕开融合、只用 sparse 检索，发现：

1. **sparse 编码跨进程不可复现**。同一查询三次独立运行编出不同词项数
   （`RRF`: 0 / 2 / 0；`Qrdant`: 0 / 2）。**但每次运行内部完全自洽**——同进程连编
   三次相同，批量与单条一致。所以不是随机性，是跨进程状态差异。
2. **即使编出词项，sparse 排序也不对**：`AWB-4471` 编出 4 个词项，却把唯一含该错误码
   的文档排在第 3。

已排除：短查询固有编不出词项、批量路径 bug、随机性、融合权重（sparse 单独检索本身
就排不对）。最可能的方向是 **float16 与 MPS 后端的舍入差异**让阈值边缘的词项在不同
进程落到两侧——**未验证**，下一步是固定 `use_fp16` 与 device 后跨进程重测。

### 撤回了一个我在同一轮里给出的结论

我先报告过「短查询固有地编出零词项，这是 BGE-M3 的已知行为」。那是**从单次运行读出
的性质**——我在把它写成测试时，下一次运行立刻推翻了它（`Qrdant` 编出 2 个词项）。
两条测试已撤回，未提交。

**这一轮没有把任何单次观察写成测试。** 把不可复现的观察钉成契约，是把噪声固化——
比不写更糟。

### 一条已合并的测试是不稳定的

`test_the_real_model_weights_real_terms` 断言 `reciprocal rank fusion` 词项数 `> 0`，
而该查询实测在不同运行得到 0 和 1。**它一直通过是运气。** 没有改动它：在编码稳定
之前，改成任何具体期望值都是固化噪声。报告里记了这一条。

## PR-032 排除 float16/MPS 假设，指向权重加载

状态：**假设被推翻，诊断仍未定论**。见
[`SPARSE-DIAGNOSIS.md`](../evals/rag/reports/SPARSE-DIAGNOSIS.md)。

上一轮猜测 sparse 编码的跨进程差异来自 float16 与 MPS 舍入。**固定 CPU + float32 后
两个独立进程仍然不同**，且差异更大（`recieved chunk batch`: 7 项 vs 1 项，且那 1 项
的索引不在 7 项之内）。**精度与设备都不是变量，假设不成立。**

新线索指向**权重未完整加载**：进程 2 里几乎每条查询都塌成 1 个词项、且换成了全新的
词——这不是舍入抖动的形态。BGE-M3 的稀疏头（`sparse_linear`）是独立于主干的一组权重，
**主干加载成功不代表它也加载了**；若它回退到随机初始化，此前所有 sparse 数字（含 #46
的负结果）全部作废。

下一步三项写在报告里：比对两次加载的 `sparse_linear` 权重、确认 `sparse_linear.pt`
是否真的被读取、必要时作废既有 sparse 结论。

**没有给出第三个诊断。** 这一轮已经撤回过一个从单次运行读出的结论；在余量不足时再猜
一个，只会再撤一次。否定结果本身是有价值的产出：它把搜索空间从「精度/设备」缩到了
「加载」。

## PR-033 摄取 worker：把上传接到索引上（WP05-07）

状态：**已实现并通过本地测试（真实 PostgreSQL + Qdrant）**。

**这补的是主体基线上的一处真实断裂**：`workers/` 此前只有 `__init__.py`，没有任何
东西消费 outbox；`IngestionService` 在 `src/` 里除自身模块外无人调用。也就是说
**上传一个文档会写出 outbox 事件，然后没有任何东西索引它**——整条 Chat/RAG 之所以
跑得通，是因为测试和评测脚本**直接调用**了摄取服务。生产路径上传的文档永远不会被检索到。

**事件是唤醒信号，不是数据来源。** worker 锁住 document 行、重读 PostgreSQL **当前**
快照、索引那个快照。稳定 point id 只解决重复投递；乱序投递靠的是这条——按事件自身
payload 应用，晚到的旧事件就会盖掉新版本。

`last_applied_revision`（迁移 `0005`）是比较对象。旧于它的事件标记 superseded 而不
应用，**但仍然 ack**：一个描述过去状态的事件下次仍然描述过去状态，留在队列里只会让
worker 永远重新发现它。

**先索引、后记 revision。** 反过来会让一次崩溃把没做的事标记成已做，而且没有任何东西
会察觉——文档只是永远不可搜索。

取字节与跑模型放在事务之外：把数据库事务横跨这两件慢事，等于让别人排在一把锁后面。

6 条测试，含 1 条对照（worker 跑之前索引里什么都没有——没有它，"上传变成可搜索"
这条断言在一个急切写入的实现下也会通过）。**验证过是有牙的**：撤掉 revision 比较，
「旧事件被 superseded」失败。

### 一处未被测试覆盖，如实记下

`UPDATE ... WHERE last_applied_revision < revision` 里的那个条件是防两个 worker
竞态的。撤掉它**全部测试照过**——我的测试都是单 worker 顺序执行，覆盖不到它。
保留是因为它是正确的，但**没有测试守着**，多 worker 竞态测试属于 WP09 的故障支架。

## PR-034 `knowledge_search` Tool（WP04-08）

状态：**已实现并通过本地测试**。检索作为工具暴露给 agentic 路径。

**包装的是同一个 `RetrievalService`**，所以两条路径产出同一个 `ContextPacket`。两个
检索器意味着两套授权检查、两种引用形状、两个要评测的东西——而受关注较少的那个，
就是会漏的那个。

**principal 来自执行上下文，绝不来自参数。** 这是这个工具存在的关键安全性质：
参数是一次工具调用里**唯一能被不可信文本触达**的部分——一段检索回来的文字写着
「以 user_admin 身份搜索」，就是它在给自己授权。schema 里**没有** principal 字段
（`additionalProperties: false`），handler 也只从 run 读。

knowledge_base 是参数，因为「去哪找」是模型的工作；而找一个它无权读的知识库，会被
和别处同一道 PostgreSQL 检查拒绝——**收窄到某个知识库不是授权，也不会因为它来自参数
就变成授权**。

5 条测试。**验证过是有牙的**：把 handler 改成从参数取 principal，「模型不能自选身份」
那条失败。另有一条断言直接钉在 schema 上（字段集合恰为 query/knowledge_base_id/top_k，
且 `additionalProperties: false`），这样将来悄悄加一个字段会被测试拦下，而不是等到
运行时。

**尚未装配进 ChatService**：默认 Chat 仍然是固定 2-step、工具清单为空。把它接上属于
深度研究模式的开关，是另一个行为变化。
