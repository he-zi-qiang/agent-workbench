# ADR-041：迟到的心跳没有资格续租（本批不做 watchdog）

- 决策点：一个事件循环停摆了但又回来了的 Worker，凭什么还能告诉 PostgreSQL 自己
  活着；WP08-12 要的 daemon thread 探针、warn/abort 两级阈值、abort 三件事，本批
  做哪几件、哪几件明确不做、为什么；那条启动校验 `abort_lag + lease_grace <
  lease_duration` 写在哪一层才可能真的红；watchdog 的 abort 与 lease/epoch
  fencing 会不会打架
- 状态：**接受**，收窄 `docs/implementation-plan.md` WP08-12 的 watchdog 部分；
  更正 `adapters/telemetry/event_loop_lag.py:59-63` 那段自陈理由里的一个事实错误，
  见 §3
- 日期：2026-08-11
- 影响：`workers/task.py` 的 `_heartbeat_loop` 在续租前自查迟到，超过判据不续租
  而是抛 `StaleExecutionError`；唯一的装配面改动是给心跳传入一个由
  `TaskConfig.heartbeat_seconds` 派生的 `abort_lag`。**零新增配置字段、零
  `ownership.yaml` 变动、配置 schema 保持 `1.14`、零迁移、零 `Literal` 变动、
  零线程、零新端口方法。**`adapters/telemetry/event_loop_lag.py` 一行不改；
  ingestion worker 一行不改。
- 依赖：[ADR-024](./0024-task-worker-lanes.md)（Task Worker lanes：heartbeat 是
  独立 asyncio task 的形状，以及 `coordination.heartbeat_execution =
  "independent_task"` 这条冻结 `Literal` 的落点）

## 1. 背景：仓库里坏掉的不是"缺一个 watchdog"

`docs/implementation-plan.md:898-933` 的 WP08-12 把这块工作描述成"缺一个
`EventLoopLagWatchdog` 的 abort 半边"。对着代码逐项核之后，结论不同：**真正坏掉
的是另一件事，而且它跟 watchdog 无关。**

`src/agent_workbench/workers/task.py:449`：

```python
async def _heartbeat_loop(self, lease: ExecutionLease) -> None:
    while True:
        await asyncio.sleep(self.heartbeat_seconds)
        await self.registry.heartbeat(lease, lease_seconds=self.lease_seconds)
```

`sleep` 完就续租，**一个字都不检查自己迟到了多久**。

而续租那条写走的 fence 是 `_live_lease_conditions`
（`adapters/persistence/task_registry.py:775`），它对时间的唯一要求在 791 行：
`task_runs.c.lease_until > func.now()`。

于是有这条可达路径（默认值 `lease_duration_seconds=90`、
`heartbeat_interval_seconds=20`）：

| 时刻 | 发生了什么 | `lease_until` |
|---|---|---|
| t=0 | `claim_next` 设租约 | 90 |
| t=20 | 第一次心跳，续租 | 110 |
| t=20…80 | **事件循环冻住 60 秒** | 110 |
| t=80 | 循环回来，`sleep` 迟到返回 | 110 |
| t=100 | 心跳照常发出续租；`100 < 110`，fence 通过 | **170** |

一个**死了 60 秒**的进程就这样保住了独占权，而且在这期间没有任何别的 Worker 能
reclaim 它——因为 `reclaim_expired` 找的是 `lease_until` 已过期的行，而这一行的
`lease_until` 一直被推着走。

`docs/implementation-plan.md:925` 写着「Watchdog 不能替代 heartbeat，也绝不能替
Worker 续租」。**今天违反这条规矩的不是 watchdog，是 heartbeat 自己。**

### 1.1 现有的那个 watchdog 装错了进程

顺带核对：`EventLoopLagWatchdog`（`adapters/telemetry/event_loop_lag.py`）是一个
frozen dataclass，字段只有 `telemetry`、`interval_seconds=1.0`、
`warn_threshold_seconds=10.0`；`run_forever` 是 `loop.time()` → `await sleep(1)`
→ 减出 lag，超过 10s 就 `logger.warning` + 两条指标。只有一个 `>` 分支，没有第二
级阈值，**没有任何副作用**。

`src/` 里唯一的构造点是 `apps/api/main.py:160`。API 不持 lease、不 claim，停摆的
代价是请求变慢；Task Worker 持 lease、会续租、停摆的代价是重复执行，**却一行都
没接**。而且 Task Worker 进程连 `Telemetry` 都没有——`build_telemetry` 全仓只在
`apps/api/dependencies.py:342` 调用一次，`TaskWorkerRuntimeConfig` 没有
observability 字段。

`docs/status.md` 全文 grep「看门狗 / watchdog / 停摆」零命中。所以这也是这块工作
第一次被记账。

## 2. 决策：本批只做一件事——心跳自查迟到

`_heartbeat_loop` 在 `await asyncio.sleep()` 前后各取一次 `loop.time()`。实际
耗时超过 `heartbeat_seconds + abort_lag` 时，**不发续租**，直接抛
`StaleExecutionError`。

它落进 `workers/task.py:340` 那条既有的 `except StaleExecutionError`：cancel
execution、gather、然后由上层处置。这复用的是 `_GuardLostError`
（`workers/task.py:255-259`）已经走通的纪律——**不再写 checkpoint、heartbeat、
lifecycle**——而不是新造一条停机路径。

**处置权本来就该在心跳手里。**心跳是唯一那个「停摆期间还在等、循环恢复时第一个
知道自己迟到了多久」的东西。一个 daemon thread 能更早**检测**到停摆，但它无法在
不持有数据库句柄的前提下**阻止**那次续租；而让线程持有数据库句柄，距离"线程自己
替 Worker 续租"只差一次编辑。

零配置字段、零迁移、零线程、零新端口方法、一个文件。

## 3. 阈值：`abort_lag = heartbeat`，判据 40 秒；并更正一段自陈理由

`abort_lag` 由**装配处**从 `TaskConfig.heartbeat_seconds` 派生（不是新配置字段，
是派生值），取 `abort_lag = heartbeat`。默认 20 + 20，所以判据阈值是 **40 秒**。

这保留了 `adapters/telemetry/event_loop_lag.py:59-63` 那段自陈的**结论**——阈值
是 heartbeat 的性质，不是部署旋钮——但**推翻它的机制**。那段原文写的是：

> 不从配置读是故意的：一个字段要登记进 `ownership.yaml` 并过投影，而这个阈值是
> heartbeat 的性质，不是部署旋钮。代价是它不会跟着 `heartbeat_interval_seconds`
> 走——**改它的人得同时改这里**。

这里错的是最后半句。它假设存在一条会提醒人的链接，而**那条链接不存在**：
`tests/adapters/test_event_loop_lag.py:362` 的
`test_the_defaults_follow_the_heartbeat_derivation` 断言的是**手写的字面量**
`heartbeat_interval_seconds = 20.0`，它**不读** `config/config.default.toml`。
改 TOML 里的 heartbeat，这条测试照样绿，没有任何东西会提醒任何人。

这个错误在 warn 级只是日志灵敏度不对；在会真的拒绝续租的判据上，它是「误杀健康
Worker」或「永不拒绝已死 Worker」。所以本批取派生值而不是常量。

同时**正面拒绝把它做成配置字段**：那等于给部署一个能把 fencing 不变量调坏的
旋钮——把 `abort_lag` 调到大于 `lease_duration`，这条自查就永远不会触发，而配置
校验挡不住它（§8 说明为什么）。

## 4. 只装 task worker，不装 ingestion worker

`workers/ingestion.py` 是同一形状的 heartbeat + guard_lost，同样持 lease，同样
有这个缺陷。本批**不修它**。

原因写死：ingestion 的循环上还有**本批不挪走的合法阻塞源**——`TextDocumentParser`
的同步 `extract_text`（全仓只在 `apps/ingestion_worker/composition.py:160` 构造），
以及 `LocalArtifactStore` 的同步写。给它装自查，会把一次**正常的**大文档摄取判成
失去 lease。

这是本批内部的一条顺序依赖，写进 ADR 而不是留在某个人脑子里：**给 ingestion
worker 装心跳自查，必须先把它循环上的阻塞源挪走。**

## 5. abort 与 lease/epoch fencing 不打架——但围栏在这里保护不了你

方向相同，不打架：一个失去续租资格的 Worker，接下来的 fenced 写都会失败，
`reclaim_expired` 会接手。前提是 **abort 之后一个字都不写**。

**但真正的危险恰恰相反**，这一条必须写清楚：暴露窗口正好是
`stall < lease_duration`（默认 90 秒）。**在这个窗口内租约仍然有效，任何 fenced
写都会成功。**围栏此时不会拒绝任何东西。

所以「什么都不写」只能靠**纪律**实现——落进 `workers/task.py:255-259` 那个空
handler——不能指望围栏挡住。任何一次"顺手把状态写清楚"的重构都会静默破坏它。

**特别是不能走 `_fail`。** `mark_failed(lease, ...)` 在这个窗口内**会真的写
成功**，产出一个假的 `failed`——一个循环只是卡了一会儿的 Task，被记成"这次没
做成"，而它其实还能被 reclaim 后跑完。

超过窗口之后围栏才接手：续租失败 → `heartbeat.result()` 抛 `StaleExecutionError`
→ `run_once` 什么都不写地返回 → Task 交给 `reclaim_expired`。「一个走掉的 Worker
该怎么办」本来就是 reclaim 的职责，本条不抢它的活。

## 6. 本批明确不做 watchdog，这不是打折

WP08-12 要的 daemon thread + `call_soon_threadsafe` 探针、warn/abort 两级阈值、
把它接进 Worker 进程——**本批一件不做**。三条理由：

**(a) 边际正确性价值接近零。**§2 已经关掉了正确性洞。探针只多买到「循环再也没
回来」这一种情况的可观测性，而一个再也没回来的循环意味着进程已死、lease 自然
到期、reclaim 正常接手。它买的是知情，不是正确。

**(b) 让探针有地方可报，要先做另一项行为变化。**Worker 进程今天没有
`Telemetry`（`build_telemetry` 全仓只在 `apps/api/dependencies.py:342` 调用一次），
要让 `EVENT_LOOP_LAG` / `EVENT_LOOP_STALLED` 在 Worker 里出现，得先给
`TaskWorkerRuntimeConfig` 加 observability 投影。按一 PR 一变化的纪律，那是它
自己的一刀。

**(c) 引入线程就得先立一条只能靠架构测试守的规矩。**单调时钟的读数只准用来
**拒绝**，永远不能用来**断言**任何 `coordination.lease_time_source =
"postgresql_clock"` 拥有的事实——租约的死活只由 PostgreSQL 说了算。落地成规矩就
是「daemon thread 不许持有任何数据库句柄」。本批不引入线程，就不必先立这条规矩。

整块推到下一批，**写成没做**：「循环再也没回来」这种停摆本批仍然测不到，
`adapters/telemetry/event_loop_lag.py` 一行不改，
`tests/adapters/test_event_loop_lag.py:362` 那条钉着手写 `20.0` 的假链接原样留着
——本 ADR 只把 §3 那个事实记成下一批的入口。

## 7. abort 三件事逐条说明为什么做不到

不写一句「暂不实现」，因为那三件事各有各的拦路石，下一个人需要知道是哪一块。

### 7.1 「原子标记 Worker `unhealthy + draining`」——没有落点

- `adapters/persistence/models.py` 里**没有 workers 表**；
- `task_runs.lease_owner` 是一个字符串，不是外键，指不向任何 Worker 实体；
- Worker 进程没有 HTTP 面，外面问不到它的健康状态；
- **`'draining'` 这个词已经被占用了**：`models.py:886-898` 里它是
  `qdrant_index_generations.status` 的一个取值，与 Worker 无关。命名要避开碰撞。
- 而一个**进程内 bool** 上的「原子」没有指称对象——原子相对于谁？

要做，就得先给 PostgreSQL-only v1 引入 worker 身份、注册、回收。那是 v1 → v1.1
的协调面改动，需要一份更前置的 ADR。**本条声明放弃**，并承认「原子」这个词在本
批没有指称对象。

### 7.2 「停止新 claim」——它会制造一个新故障模式

`TaskWorkerRunner._lane` 的 `while not shutdown.is_set()`（`runner.py:81`）是
现成的开关，但那个 Event 今天是 `apps/task_worker/main.py:56` 里 `serve()` 的
**局部变量**，要交给别人就得改装配顺序。

更要紧的是：一个停摆后恢复、从此不再 claim 的 Worker，在**没有健康探针的部署**
里不会被重启，等于永久少一个 Worker。那是新故障模式，不是修复。要它成立，前提是
§7.1 里那个不存在的健康面。

### 7.3 「取消所有 active run」——前提是另一项独立的行为变化

前提是把 `apps/task_worker/composition.py:679` 的 `NullCancellationToken` 换成
per-Task 的 `CancellationSource`（`ports/cancellation.py:48` 已存在，全仓无人在
Worker 里构造）。runtime 里 5 处 `cancellation.cancelled` 检查点，今天都在等一个
**永远为 `False`** 的 token。那是它自己独立的一项行为变化。

而且**停摆期间 `call_soon_threadsafe` 投不进去**，取消要等循环回来才生效——恰好
在最需要它的时刻做不到它承诺的事。

**本批「abort」的唯一后果是：这个 Worker 不再被允许说自己活着。**

## 8. 那条启动校验不写，并解释为什么它永远不会红

WP08-12 要求一条启动校验：`abort_lag + lease_grace < lease_duration`。

`abort_lag = heartbeat` 时（§3），它就是
`heartbeat + lease_grace < lease_duration`。而
`CoordinationSettings.validate_timing`（`settings.py:337`）已经要求：

```
safety_floor = heartbeat * (max_missed_heartbeats + 1) + lease_grace
lease_duration > safety_floor
```

在 `max_missed_heartbeats >= 0`（字段本身 `ge=0` 保证）时，
`heartbeat * (max_missed + 1) >= heartbeat`，所以现有那条**严格蕴含**要加的
这条。

写成 `Settings` 的第二个 validator，它**永远不会红**；测试只能靠先禁掉第一条
才让它红。按本仓「必须实测有牙」的纪律，那是一条不合格的校验——一条从不报的校验
和一条不存在的校验无法区分。

**本批不写它**，把这条蕴含关系写在这里，免得下一个人再来加它。本批连装配处的
版本也不写，因为本批**没有**一个可以手工传入 `abort_lag` 的 watchdog 构造点。

顺带把一个与简报假设相反的事实写清楚：**`coordination.lease_grace_seconds` 已经
存在**（`settings.py:317`，默认 10、`ge=0`）**并且已经登记**
（`config/ownership.yaml:184`，owner=`workers.task.lease_manager`，
lifecycle=`live`）。所以那条启动校验就算要写，也不需要新增任何配置字段。本批既然
不写它，**连投影都不必动**：`TaskConfig`（`projections.py:391`）不加
`lease_grace_seconds`，留到下一批做 watchdog 时再加。

## 9. 为什么这也算一份 ADR

按 `docs/status.md:251-255` 的判据——零 `Settings` 叶子、零 `Literal`、零
ownership 变动、schema 不变、零迁移 ⇒ 不写 ADR——本条**不需要** ADR。

写它有两个理由。一，它改的是**存活性 / fencing 的语义边界**：一个 Worker 从此会
因为一个此前不存在的原因失去 lease。二，这是这块工作**第一次被记账**（§1.1：
`docs/status.md` 全文 grep 零命中），而本批同时决定了**不做**其中的大部分——
那些"不做"如果不写在一处，会在下一批变成"上一批做过了吗"。

## 10. 后果

### 10.1 得到的

一个事件循环停摆超过 40 秒（默认 heartbeat 20 + abort_lag 20）的 task worker
不再能续租；lease 到期后可被正常 reclaim。`docs/implementation-plan.md:925` 那条
规矩第一次真的成立。

### 10.2 代价，逐条写在明处

1. **引入了一种此前不存在的失去 lease 的原因。**默认 40 秒的调度抖动几乎只可能
   是真停摆，但这是一次真实的行为扩面，CI 高负载下仍可能偶发。所以判据取
   `heartbeat + abort_lag` 而不是 warn 级的 `heartbeat / 2`，测试用注入时钟而不是
   真 `sleep`。
2. **【最容易被忽略的残留风险】** task worker 的循环上仍然有一个本批不挪走的同步
   阻塞源：`apps/task_worker/composition.py:272` 构造了 `LocalArtifactStore`，而
   [ADR-042](./0042-blocking-belongs-to-the-adapter.md) 只把它的**只读**路径
   `get` / `_read` 挪进有界池，**非幂等的 `put` / `put_stream` 留在循环上**。
   所以「先做 ADR-042 再做本条就不会误判」这个说法**不成立**——ADR-042 并没有
   清空 task worker 的循环。真正的防误判靠两件事：判据是 40 秒不是 20 秒，以及
   本地盘的一次 100 MB 写远在这个量级之下。这句写在这里，不留给读者自己推。
3. **ingestion worker 仍然会替停摆的自己续租**——与本批修掉的 task worker 侧是
   同一个缺陷，本批不修（§4）。
4. **本批没有 watchdog**：「循环再也没回来」这种停摆仍然测不到，Worker 进程仍然
   没有 `Telemetry`，停摆在 collector 上看不见、只有日志。**能力表上不许写成
   已实现。**
5. **abort 三件事一件没做**（§7）。本批「abort」的唯一后果是「这个 Worker 不再
   被允许说自己活着」。
6. 暴露窗口内（`stall < 90s`）的 Task 不再被这个 Worker 推进，**也不被它写成任何
   终态**，要等 `reclaim_expired` 接手。比今天的「悄悄续租、悄悄重复执行」慢，
   但正确。

## 11. 什么会让这条决定重来

**给 ingestion worker 装上心跳自查。** 那要求先把它循环上的同步 `extract_text`
与同步 artifact 写挪走（§4），而 `DocumentParserPort.parse` 改成 async 是一次跨层
port 契约变更。做那一刀时，本条的判据（`heartbeat + abort_lag`）要重新算一次：
ingestion 的合法慢比 task 侧长一个量级。

**abort 三件事里任意一件真的要做。** §7 各自的拦路石都不是"没时间"，是缺一个更
前置的东西（worker 身份表 / 健康面 / per-Task `CancellationSource`）。哪一件先
落地，本条的 §7 对应小节就该被那份 ADR 收窄。

**有人把这个判据做成配置字段。** §3 明确拒绝了它。要推翻，需要先回答：一个能把
`abort_lag` 调到大于 `lease_duration` 的部署，凭什么不算把 fencing 不变量调坏了；
以及那条约束要写在哪里才真的会红（§8 说明了它写在 `Settings` 里不会）。
