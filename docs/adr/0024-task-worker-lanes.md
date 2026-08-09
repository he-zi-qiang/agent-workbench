# ADR-024：一个 Worker 进程可以同时跑多个 Task，因为围栏已经建好了

- 决策点：Task 并发执行的拦路石是什么；`worker_concurrency` 还该不该被钉死在 1
- 状态：**接受**
- 日期：2026-08-09
- 影响：`TaskWorkerRuntimeConfig.worker_concurrency` 由 `Literal[1]` 变为 `int`；`project_task_worker` 移除 `!= 1` 拒绝；`TaskWorkerRunner` 增加 lane。config schema **不变**

## 背景

试用时提出的要求：**Work 的任务要能并发，不要排队。**

现状是排队的，而且理由写得很明确。三处代码异口同声：

| 位置 | 原文 |
|---|---|
| `config.default.toml` | "Multi-worker claiming waits for the lease/fencing work package rather than being implied by a config number." |
| `bootstrap/settings.py` | "The registry/checkpointer have **no lease epoch or fencing yet**." |
| `bootstrap/projections.py` | 启动即拒："lease/fencing-based multi-worker coordination is not assembled" |

按这个口径，答案应该是"这是一个未建成的工作包，要先做 lease/fencing"。

## 问题

那三句话在**写的时候**是真的。现在不是了。

去查实现，E1/E2 两半都在：

- 认领是 `FOR UPDATE SKIP LOCKED`（`task_registry.claim_next`）；
- 租约带**单调 epoch**，Registry 的每一次生命周期写入都用
  `(task_id, status='running', lease_owner, lease_epoch, lease_until > now())`
  做谓词（`_live_lease_conditions`）；
- **checkpointer 同样有围栏**：`PostgresCheckpointSaver._assert_fence` 在每次
  checkpoint 写入前，用 `FOR UPDATE` 重查同一条 owner-epoch-expiry 谓词，失败抛
  `StaleCheckpointWriteError`；
- 而且它在生产装配里是**强制**的：`composition.py` 构造
  `PostgresCheckpointSaver(engine, require_fence=True)`。

`task_registry.py` 的模块 docstring 还写着 "Fencing the LangGraph checkpointer is
E2 work"，而 E2 已经落地了。**那三处注释描述的是一个已经不存在的系统**，这正是本项目
反复清除的那类缺陷——只不过这次它挡住的是一个功能，而不是放行了一个错误。

所以真正的问题不是"要不要建 fencing"，而是：**在围栏已经建好的前提下，还剩什么挡着并发。**

答案是一个 `while` 循环：

```python
while not shutdown.is_set():
    outcome = await self.run_once()      # 一次一个，串行
```

## 决策

### 一、并发是**进程内的 lane**，不是多进程

`TaskWorkerRunner` 起 `concurrency` 条相同的循环，每条各自 claim-execute-settle。

这一条要和被拒绝的那件事分清楚。原注释说的是 **multi-worker / 多进程**认领——两个
进程抢同一个 Task，靠 epoch 分胜负。本决定**不做那件事**，也不声称它可用：没有任何
测试让两个 Worker 进程互相竞争。

而进程内 lane 根本碰不到那个竞态。`SKIP LOCKED` 保证同一时刻一个 Task 只落到一条
lane 手里，几条 lane 跑的是**不同的 Task 行**——"同一个 Task 两个执行者"在这里连
构造都构造不出来。**围栏是安全网，不是本决定依赖的机制**；它依赖的是 `SKIP LOCKED`。

### 二、`TaskWorker` 本来就可重入，这是本改动能这么小的原因

不是运气，是三条既有设计各自成立：

- `TaskWorker` 是 frozen dataclass，**全类没有一处 `self.x = `**，`run_once` 只用局部量；
- `TaskExecutionScope` 是 **`ContextVar`** 实现的。asyncio 每个 task 拿到 context 的
  独立副本，所以 `scope.executing(lease)` 在并发 lane 之间天然隔离——一条 lane 里的
  图节点读到的永远是自己那条 lease。这是全套设计里最关键的一处：如果它是个普通属性，
  A 任务的节点会读到 B 任务的 claim，而且**不会报错**，只会算错；
- guard 是 `task_pinned` 的，每个 Task 自己获取自己的会话级 advisory lock。

三条里任何一条不成立，这个改动都会变成重写执行器。所以本 ADR 记下它们，是因为
**它们现在成了并发的承重结构**，下次有人想把 scope 改成普通属性时，得先看到这里。

### 三、上限是 guard 连接预算，而且**不在这里再查一遍**

每条并发 lane 会钉住一条自己的 guard 连接，所以 lane 数不能超过
`database.guard_connection_budget`（默认 4）。

这条规则 `Settings.validate_architecture_and_environment` **已经在查了**。因此
`project_task_worker` 里那个 `!= 1` 拒绝被删掉之后，**没有用新的拒绝替换它**：
在投影层再写一遍，就是同一条规则的第二份拷贝，而按本仓库自己的说法——
"a rule restated is a second copy of that rule, and the copy is the one that
keeps running after somebody edits the first"。

### 四、默认仍然是 1

放开的是上限，不是默认值。每加一条 lane 就多一条并发的 provider 调用，钱和限流都是
真的。默认留在 1，谁要并发谁显式写。

### 五、`concurrency == 1` 走单 lane 分支，不走"只有一条的 TaskGroup"

`TaskGroup` 把异常重新抛成 `ExceptionGroup`。绝大多数部署是单 lane 的，它们应该拿到
执行器**自己**抛的那个异常，而不是被包一层——所有照着旧循环写的调用方和测试因此不必
改。这不是性能优化，是保持常见路径的形状不变。

## 后果

- **计费面变了**：N 条 lane 意味着最多 N 个 Task 同时在调模型。这是要的效果，也是要
  付的钱；
- 多**进程** Worker 仍然未取证。epoch 围栏为它而建、看起来也够用，但"看起来够用"和
  "测过"是两件事，本 ADR 不把后者写进能力表；
- 一条 lane 抛异常会经 `TaskGroup` 取消其余 lane。这是刻意的：旧循环遇异常即死，
  新实现要是每次只掉一条 lane，Worker 会**悄悄退化成单 lane**还继续服务；
- 三处过期注释与 `task_registry.py` 的模块 docstring 一并改正。它们不是顺手改的——
  正是它们让"并发"看起来像个未建成的工作包；
- config schema **不动**。`worker_concurrency` 一直存在且一直是 `int`，被钉死的是
  投影层的 `Literal[1]`；没有新增、删除或改名任何配置字段。

## 重审条件

如果要让**两个 Worker 进程**同时认领，本 ADR 不覆盖那件事：那才是 epoch 围栏真正要
挡的竞态，届时至少需要一组让两个进程真的互抢同一个 Task 的测试，以及对 `worker_id`
唯一性的要求。届时也该重新审视 `require_same_physical_session`。
