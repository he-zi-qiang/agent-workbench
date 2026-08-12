# ADR-040：调用额度先扣后花，扣光的 Task 是 dead_letter

- 决策点：一次 agent invocation attempt 的物理边界是什么、记账点落在装饰器链的哪
  一层；跨 retry 与 reclaim 的持久计数器落在 `task_runs` 的一列还是一张台账表；
  崩溃重放算不算一次新花费；执行时读的是 Task 自己快照里的上限还是 Worker 当前
  进程的配置；额度用尽之后 Task 是 `failed` 还是 `dead_letter`、谁来写；同一条
  fenced `UPDATE` 匹配不中时三种意思怎么区分
- 状态：**接受**，兑现 [ADR-030](./0030-working-nodes-are-governed-by-cost.md) §3
  点名的那道未装的闸；与 [ADR-022](./0022-tool-ceiling-closes-the-toolbox.md)
  方向相反，差别写在 §8
- 日期：2026-08-11
- 影响：`task_runs` 新增 `agent_invocation_count`（`Integer`，`not null`，
  `server_default '0'`）并并入既有的 `task_runs_lease_counters` check；新增迁移
  `0025_task_agent_invocation_count`，alembic head 由 `0024_document_ingestion_failure`
  变为 `0025_task_agent_invocation_count`；`TaskRegistry` 端口新增
  `reserve_agent_invocation` 与 `mark_dead_lettered` 两个方法；executor 装饰器链
  在 `BoundedParallelExecutor` 外新增一层 `BudgetedAgentExecutor`；新建
  `application/multi_agent/attempt_ledger.py`；API 的 Task 详情响应 DTO 增加一个
  只读整数字段。**零新增配置字段、零 `ownership.yaml` 变动、配置 schema 保持
  `1.14`、零 `Literal` 变动。**`multi_agent.max_agent_invocation_attempts_per_task`
  的字段名、默认值、取值范围一字不改——变的是它第一次真的被执行。
- 依赖：ADR-022（工具额度用尽 = 收走工具、不结束 run）、
  [ADR-024](./0024-task-worker-lanes.md)（Task Worker lanes 与 executor 装饰器链
  的位置）、ADR-030（per-invocation 的成本/时限闸）、
  [ADR-034](./0034-a-structured-node-asks-once-more.md)（纠正轮会多一次 resolve）、
  [ADR-039](./0039-a-metric-name-is-a-promise.md)（`config_schema_version` 的 pin
  机制与抬版判据）

## 1. 背景：一个声明了四年的上限，从来没有第二个读者

`multi_agent.max_agent_invocation_attempts_per_task` 声明在
`bootstrap/settings.py:499`，默认 12，`ge=1 le=100`。对着代码逐项核：

- `src/` 里**唯一**读它的地方是同一个文件 `settings.py:521` 的
  `validate_agent_budget`，而它只做一件事：拒绝
  `max_parallel_agent_invocations > max_agent_invocation_attempts_per_task` 的
  配置。除此之外没有第二个读者。
- `tests/` 下 grep 不到 `validate_agent_budget` 或 `max_agent_invocation` 任一
  字符串。所以连这条交叉校验本身"会红"这件事，今天也只由 pydantic 的运行时行为
  保证，没有测试证明。
- `bootstrap/projections.py:730` 把 `multi_agent` 的另外五个字段投影进
  `MultiAgentConfig`，唯独跳过它，并在 `projections.py:425` 的 docstring 里给了
  理由。
- `config/ownership.yaml:350-356` 已经把它单独成组，owner 写着
  `application.multi_agent.attempt_ledger`、lifecycle 是 `task_snapshot`——而
  这个 owner 指向的模块**不存在**。名字先占了坑。

`docs/configuration.md:539`、`docs/architecture-baseline.md:1104-1106`、
`docs/implementation-plan.md:1165` 三处都写着同一句承诺："invocation attempt
计数必须持久化并受 fencing 保护，retry/reclaim 后不归零"。代码里：没有这个
计数器，没有读取点，没有超限行为，也没有对应的端口方法。

### 1.1 今天真实的花费上界，比配置里那个 12 大一个数量级

`task_runs.attempt_count` 是已有的持久计数器（`task_registry.py:330` 每次
`claim_next` 加一），但它数的是**领取次数**，不是调用次数。`reclaim_expired`
（`task_registry.py:435`）在 `attempts >= max_attempts` 时写 `dead_letter`。
checkpoint 里的 `agent_outcome_refs` / `budget_usage` 会累计 agent run，但**没有
任何上限读它们**，而且崩溃时最后一个未 checkpoint 节点的花费会整块丢失。

于是今天不存在任何 per-Task 的跨调用总闸。已有的四道闸各管各的：

| 闸 | 管的量纲 |
|---|---|
| ADR-030 的 `max_cost_micro_usd` / `max_seconds` | 一次 invocation |
| ADR-022 的 `max_tool_calls` | 一次 run 内部 |
| `multi_agent.static_agent_node_limit` | 编译期的图形状 |
| `coordination.max_attempts` | 一个 Task 被领取的次数 |

而 v2 的 work↔review 循环里 `max_revisions` 由**提交方**给
（`apps/api/routes/tasks.py:51`，`ge=0 le=20`），一次 claim 就能跑 40+ 次调用，
再乘 `coordination.max_attempts=5` 次领取。配置里写着 12。

今天最终兜住这件事的，是 `domain/tasks.py:216` 上 `agent_outcome_refs` 的
`max_length=256`——跑到第 257 次调用的 Task 会在 `TaskState` 校验处崩溃，而不是
干净地"超预算"。一堵没人当预算的隐性硬墙。所以这不是"上限暂时没接、反正差不多"。

### 1.2 这个数字其实已经 per-Task 持久化了，只是无人读回

`settings.py:1385-1413` 的 `run_semantics_snapshot()` 按 section 整块取
`public_config()`，`public["multi_agent"]` 整段在内；这份快照在提交时写进
`task_runs.run_semantics_snapshot`（`models.py:962`）并且从不重解析。

也就是说，**这个上限今天已经随每一个 Task 冻结在库里了**。全仓核对
`run_semantics_snapshot` 的引用：`settings.py` 构造它，`projections.py:398/651`
携带它，`application/tasks.py:195/268` 与 `ports/task_registry.py:135/172` 传递
它，`models.py:962` 存它，`apps/api/dependencies.py:330` 深拷贝它——**零个读回
其中某个键的地方**。`projections.py` 那句 "It stays in settings, unprojected"
对 `MultiAgentConfig` 成立，但读者很容易据此以为这个数字没有任何持久形态。

## 2. 决策

### 2.1 一次 attempt = 一次 `AgentExecutor.run`

物理边界定死为一次 `AgentExecutor.run`：一个现铸的 `agent_run_id`
（`task_handlers.py:1172` 的 `_context_for`）、一条 durable 的 `RunStarted`
事件、一份按次给的 `RunBudget`（token / cost / deadline）、一个完整的模型-工具
循环。

**不是一次模型往返。** 那与 `RunBudget.max_steps` 量纲重复；默认 12 步会让"上限
12"在单个节点内就用光，这条闸等于没装。

**不是一次 Task run。** `task_runs.attempt_count` 数的是领取次数，一次领取可以跑
十几次调用；用它当闸等于让上限沉默。

### 2.2 记账点是装饰器链上新的一层，不是 `resolve`

新增 `BudgetedAgentExecutor`，包在 `BoundedParallelExecutor` 外
（`apps/task_worker/composition.py:641`）。`BoundedParallelExecutor`
（`task_handlers.py:346`）本身就是"每次 invocation 都过一遍"的现成位置，
`max_parallel` 就装在那里；它是 Task 侧所有模型调用的唯一必经之路。

**必须写下为什么不是 `TaskNodeInvocationProvider.resolve`**，因为下一个人一定
先想到它——它在 `task_handlers.py:278` 已经做了 `registry.get` 加
status / lease_owner / lease_epoch 三条 fence 复核，看着就是天然记账点。它不是，
因为它与 `executor.run` 不是 1:1：

- `research_internal`（`task_handlers.py:708`）会 `resolve` 却不跑模型；
  `export`（950 行）同理；
- ADR-034 的纠正轮（`task_handlers.py:615`）会**多 resolve 一次**再多跑一次
  `executor.run`——那是第二笔真实花费，但它证明 resolve 与 run 的比例不是常数。

按 resolve 记账会既多算（不跑模型的节点）又与真实花费脱节。唯一的 1:1 choke
point 是装饰器链。

### 2.3 计数落 `task_runs` 的一列，不建台账表

新增 `task_runs.agent_invocation_count`（`Integer`，`not null`，
`server_default '0'`），并进 `task_runs_lease_counters` 那条 check
（`models.py:1081`，今天写的是 `lease_epoch >= 0 AND attempt_count >= 0`）。

不建 `(task_id, agent_run_id)` 唯一的台账表。理由要写成结论而不是偏好：
**`_context_for`（`task_handlers.py:1172`）每次重放都现铸一个新的 `agent_run_id`**，
所以那个唯一键换不来任何跨重放的幂等——它只换来一张表、一次 `count(*)`、以及一套
新的跨表加锁顺序（`adapters/persistence/tool_executions.py:94` 的文件头写死了
"先锁 `task_runs` 再写从表"）。台账表买到的是**可诊断性**，不是幂等性；该老实
承认它买不到它名字暗示的东西。

**绝不放进 checkpoint。** 崩溃时未 checkpoint 的那次调用的花费会整块丢失，而那
正是毒任务循环发生的地方。`docs/configuration.md:539` 的原话是"不能只放在可能
回滚的 checkpoint 中"。

### 2.4 先占后花

`reserve_agent_invocation(lease, *, agent_run_id)` 在 `executor.run` **之前**、
在一个独立的短事务里提交。写路径复用 `_live_lease_conditions`
（`task_registry.py:775`）那五条 fence 谓词——`task_id` / `status = 'running'` /
`lease_owner` / `lease_epoch` / `lease_until > now()`——**不发明第二个 fencing
token**（`coordination.fencing_token_strategy` 是冻结的单值 `Literal`）。

"花完再记"在一个每次都恰好在记账前崩溃的循环里，永远推不动计数器——而那正是这条
闸要挡的场景。

代价明写在这里而不是留给读者发现：**崩在"占额度成功"与"真正发起调用"之间的窗口
会多算一次。**方向是 fail-closed，真实上界比配置值小一点而不是大一点。

### 2.5 重放要再付一次钱，而这不是对幂等提交开的例外

这是整份 ADR 里最容易被实现反的一条，所以正面回答。

at-least-once + 幂等提交这条不变量管的是**外部效果**：`tool_executions` 的
`(task_id, operation_key)` 让同一次外部写只发生一次，而
`runtime/tool_gateway.py:561` 命中已提交结果时 `refuse` 的是**那次工具调用**——
提出它的那一轮模型仍然真花了钱（token 真的烧了，供应商真的收费了）。

所以 reclaim 之后重跑同一个节点，是一次**全新的、真实的**花费，必须计。
`docs/architecture-baseline.md` 那句"命中已提交幂等结果则不重复计数"只能读作
"**计数器自身的写**要幂等"。

因为 §2.3 选了列不选表，这份幂等由 `BudgetedAgentExecutor` 这**一个调用点**保证，
不由唯一索引保证。这是选列的真实代价，必须配一条测试证明"一次 `run` 只
`reserve` 一次"。

### 2.6 上限从 Task 自己的快照读，不从进程配置读

`BudgetedAgentExecutor` 读的是那一行自己的 `run_semantics_snapshot` JSONB 里的
`multi_agent.max_agent_invocation_attempts_per_task`，**不是** Worker 当前进程的
`Settings`。

理由与 `wants_report` / `export_requires_approval`（ADR-038 §2.3）同一条：
**提交时冻结**。反过来读进程配置，会让运维改一次配置追溯改变所有在飞 Task 的账。

落地是在同一条 fenced `UPDATE` 里从行自己的 JSONB 取上限，不多一次读；也**不把
上限塞进 `ExecutionLease` 或 `TaskExecutionScope`**——那两个是权限令牌，不该携带
预算。

`projections.py:425` 那段 docstring 要跟着改两处：一是"Three fields"已经是五个
字段（ADR-030 又加了 cost 与 seconds），文字比代码旧；二是"until the repository
that can honour it exists"要换成新理由——那个 repository 现在存在了，但这个字段
**仍然不进 `MultiAgentConfig`**，因为执行时读的是 Task 快照而不是进程配置。

### 2.7 三种拒绝必须可区分

同一条 fenced `UPDATE` 打空有三个意思，它们的处置完全不同：

| 情形 | 异常 | 处置 |
|---|---|---|
| (a) 刚失去 lease | `StaleExecutionError` | 停手，一个字都不写，交给接手的 Worker |
| (b) 额度用尽 | `AgentInvocationBudgetExhaustedError` | 走 `dead_letter` |
| (c) 快照里没有这个键 | 可区分的配置缺陷错误 | 走 `failed`，**不**走 `dead_letter` |

(c) 单列出来，是因为"说不出自己能花多少"是**这个部署的缺陷**，不是一个毒任务。
把它打成 `dead_letter` 会让一次配置事故变成一批不可复活的 Task。

实现：`UPDATE` 打空时，在同一个事务里再 `SELECT` 一次那行来判读。只在少见的 miss
路径上多一次读，正常路径仍然是一次往返。

### 2.8 超限写 `dead_letter`，不是 `failed`

`failed` 说的是"这次没做成"，`dead_letter` 说的是"再试也没用"。额度用尽正是后者
——下一次 claim 读到同一个已满的计数器，只会再拒一次。

dead-letter 基础设施**已全部就位**，不需要新设计：`TaskStatus` 里有它、
`task_runs_status` check 约束里有它、`TERMINAL_STATUSES` 与 `EXPLAINED_STATUSES`
里有它、`running → dead_letter` 在 `domain/task_registry.py:74` 的
`ALLOWED_TRANSITIONS` 里合法、`TaskDeadLettered` 事件存在、CLI 认它为终态、
reclaim 时不 notify。

唯一缺的是一个**持 lease 的写入方**：今天只有 `reclaim_expired` 会写
`dead_letter`。所以新增 `TaskRegistry.mark_dead_lettered(lease, *, reason)`，
必带 `status_detail`（`task_runs_status_detail` check 强制），且它写的那句话
**必须与 `reclaim_expired` 写的那句可区分**——后者写的是
`f"lease expired after {attempts} attempts"`（`task_registry.py:435-440`）。运维分不清
两个作者时，这条闸就是在悄悄毁掉 Task。

分层不动：节点只抛异常，Worker 在 `_execute` 的 `except` 里认它并选终态，与现有
`_fail` 并列。这保住了"节点说条件、Worker 选状态"这条既有分层。

### 2.9 实现切三刀

1. **纯 schema**：迁移 + `models.py` 双写，零行为变化。
2. **只计数且看得见，绝不拒绝**：端口方法、PG 与内存两份实现、装饰器只加一、
   API 详情响应上暴露一个只读整数。
3. **开始拒绝**：装饰器读上限、超限抛异常、Worker 写 `dead_letter`。

中间那一刀是本 ADR 对"一个事前完全不可见、只在用尽那一刻突然把 Task 打成终态的
闸，运维体验上跟悄悄毁掉只差一层窗户纸"这个问题的正面回答。

## 3. 为什么不是 ADR-022 的那个形状

ADR-022 的决议是：**工具额度用尽 = 收走工具、让模型用手上的东西作答，不结束
run**。这条闸的行为必须相反：**额度用尽 = 拒绝开始这次 invocation**。

写死这条差别，否则下一个人会照 ADR-022 的形状把它实现成"最后一次调用不给工具"。
那是错的，因为**没有"少一次调用地把活干完"这种降级**——一个节点要么跑要么不跑，
没有中间状态。ADR-022 能降级，是因为它管的是一次 run 内部的工具预算，而那次 run
本身已经在跑了、手上已经有材料了。

与 ADR-030 则是正交，量纲不同：ADR-030 的两道闸管**一次 invocation**，deadline
每次尝试现盖（`settings.py:515` 明写"崩溃后重放是新尝试、重新拿满额度"）；这条闸
管**一个 Task 的一生**，跨进程跨 reclaim 不归零。二者不能互代：把 per-invocation
成本闸调紧只会让每次调用更早失败，然后再调一次。

## 4. 后果

### 4.1 得到的

- `max_agent_invocation_attempts_per_task` 从"声明但无人读取"变成真的会拒绝。
- 系统第一次有 per-Task 的跨调用总闸（§1.1 那张表里的四道闸，一道都不是这个量纲）。
- `ownership.yaml:351` 那个提前占坑的 owner 名 `application.multi_agent.attempt_ledger`
  第一次名副其实。
- `dead_letter` 第一次有一个持 lease 的写入方。

### 4.2 代价，逐条写在明处

1. **【最需要写在明处的一条】** §2.4 的先占后花在"占额度成功但进程立刻在调用前
   崩"的窗口会多算一次。方向是 fail-closed，但反复在这个窗口崩的部署会**更快**
   把 Task 打成 `dead_letter`，而 `dead_letter` 在 `ALLOWED_TRANSITIONS` 里
   **无出边**、本批不提供人工复活路径。
2. 上限从快照读 ⇒ **放宽配置只对新 Task 生效**，运维改配置救不了一个卡住的在飞
   Task。反过来读进程配置会让改一次配置追溯改变所有在飞 Task 的账。两害相权取
   快照，但要接受第一种投诉。
3. **迁移当时在跑的 Task 从 0 起算，既往花费不计。** 理由与 0024 同源：回填等于
   替从未被观察过的尝试编造观察。这句写进迁移的散文 docstring。
4. `run_semantics_snapshot` 从此有第一个运行时读者——它今天是纯只写的（§1.2 已
   核对），而它是一个**没有 schema 的 JSONB 列**。任何未来给 `multi_agent` 改名
   或搬家的改动，会**静默改变在飞 Task 的预算读数**。
5. 选列不选表 ⇒ "这个 Task 的第 7 次调用发生在哪个 node、哪个 epoch"回答不了。
6. `validate_agent_budget` 校验的两个数从此可能来自不同年代：`max_parallel` 来自
   进程投影，ceiling 来自 Task 快照。**启动校验不再蕴含运行时关系。**后果无害，
   但必须说出来，因为那条校验今天的全部意义就是防这种形状。
7. 因 `gateway.advertise` 失败或 cost ceiling 无价格而在第一次模型调用前就早退的
   run（`runtime/agent_runtime.py:344` 与 371）**也计一次**。仍然是一次尝试；而且
   这两条早退是配置错误、会稳定复现，计数反而更快把毒任务打成 `dead_letter`。
8. **【本批只做一半，另一半写成没做】**"已用 / 上限"本批**只**只读地暴露在 API 的
   Task 详情响应上（既有 DTO 加一个整数字段），**web 前端不显示**。所以对着 UI
   的运维仍然看不见这道闸，只有查 API 才看得到。

### 4.3 本批明确不做的

- 不建 `(task_id, agent_run_id)` 台账表（§2.3）。
- 不回填既有 Task 的历史调用次数。
- 不给 `dead_letter` 提供人工复活路径。
- 不在 web 前端显示预算。
- **不改 `TaskState.agent_outcome_refs` 的 `max_length=256`**（`domain/tasks.py:216`）。
  它是一堵没人当预算的隐性硬墙；本批落地后它退到 12 之上很远，但仍然存在，且超了
  是校验崩溃不是超预算。写在这里，不改。
- **不清理 `models.py` 里 `task_runs` 那两条重复定义的 `CheckConstraint`**
  （`task_runs_resolved_index` 在 1039 与 1053 行、`task_runs_resume_reference`
  在 1048 与 1062 行）。本批会挨着它们加新 check，但清理是独立一刀、会动
  `compare_metadata` 的对账面。记在这里的用处是：**迁移对账莫名不过时，先看这
  两条。**

## 5. 配置影响：不抬 schema 版本

零新增配置字段、零 `ownership.yaml` 变动、`app.config_schema_version` 保持
`1.14`。`multi_agent.max_agent_invocation_attempts_per_task` 早已在
`config/ownership.yaml:350-356` 单独成组（owner=`application.multi_agent.attempt_ledger`、
lifecycle=`task_snapshot`，已核对）。本批让这个名字落在 `BudgetedAgentExecutor`
与 Registry 那对方法上，**owner 名不动 = 零架构测试 churn**。

同时承认名字与落地之间的张力：**owner 名说的是"台账"（ledger），落地是一列计数
器。**§2.3 解释了为什么，但这个名字会继续暗示一张不存在的表。

不抬版的判据引用 [ADR-042](./0042-blocking-belongs-to-the-adapter.md) §配置影响。
本条还有一个特殊之处需要单独说明：**字段名、默认值、取值范围全不变**，一份 `1.14`
的配置文件在新旧两个二进制上都照常加载。变的是同一份配置在新二进制上第一次真的被
执行——第 13 次调用不再发生。

现有先例里没有对应的一条：ADR-039 是"新二进制拒绝老文件"，ADR-036 是"新增整段
配置"，ADR-038 是"新增带默认值的叶子"。所以本 ADR 明确表态：
**"第一次执行一条一直声明着的语义"不构成抬版理由。**

同时承认 pin 机制抓不到这个真实的运维意外：一个部署升级二进制后，它的 Task 会
开始在第 13 次调用时变成 `dead_letter`，而配置文件一个字都没改、版本串也没变。
处理办法不是抬版（版本串抓不到它，因为文件确实兼容），而是把实现切成"先可见、
后咬人"两刀（§2.9）。这句要补进
`tests/config/test_settings.py::test_the_configuration_schema_version_is_pinned`
的 docstring——那份 docstring 是版本串唯一的理由清单。

## 6. 什么会让这条决定重来

**有人需要回答"第 N 次调用发生在哪个 node"。** §2.3 明说了列买不到这个。真的
需要按节点归因时，正确的动作是加一张台账表**并保留这一列**（列是闸，表是账），
而不是把闸搬进表里——闸的一次往返和表的一次 `count(*)` 不是同一个性能量级。

**`dead_letter` 需要人工复活。** 本批把"预算用尽"接到一个无出边的终态上，前提
是没有人需要救它。一旦需要，要动的是 `ALLOWED_TRANSITIONS` 和一条新的授权路径，
那是一份新 ADR——因为"谁有权把一个已判定为毒的 Task 放回队列"是一个授权问题，
不是一个预算问题。

**`multi_agent` 这个 section 在快照里改名或搬家。** §4.2 第 4 条说的静默失效会
在那一刻发生。那时该做的是给快照读取加一条显式的"缺键 ⇒ 配置缺陷错误"路径的
测试（§2.7 的 (c) 分支已经预留了这个形状），而不是给 JSONB 补一个 schema。
