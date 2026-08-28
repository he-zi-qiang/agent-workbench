# ADR-082：一次委派是一次运行，不是一个新的循环

- 决策点：这个仓库里有两个东西都叫 "sub-agent"。一个是 `workflows/agent_profiles.py`
  的图节点——写图的时候就定好了几个、叫什么、能看什么。另一个只存在于**词表**里：
  `TraceContext.parent_agent_run_id`、`AgentDelegated`/`AgentCompleted` 两个 durable
  事件、`BudgetUsage.merged` 那句 "Aggregate a child agent's usage into the parent's"、
  前端 `workTimeline.ts` 里现成的中文标签"子代理已委派"——它们描述的是**一次运行中途
  派生出的另一次运行**，而且**一个写入者都没有**。要不要有后面这一种；如果要，它靠什么
  执行，以及"自研 runtime 拥有唯一 tool loop"这条铁律怎么算
- 状态：**接受**。新增 `delegate_agent` 工具（`risk="read"`），它的 handler 调用
  **同一个 `AgentExecutor`**，产生一次新 `agent_run_id`、同 `stream_id` 的子运行。
  默认**关**（`multi_agent.delegation_enabled = false`）。
  **明确不做**：不写第二个 tool loop，不做后台（fire-and-forget）派生，不做递归（深度
  默认钉 1），不做可写子 agent，不做 agent 之间投递消息，不做 mailbox / teams /
  observer，不动 `multi_agent.topology`，不给图加节点，不让父运行的 token 上限看见子运行
- 日期：2026-08-26
- 影响：新增 `domain/agents.py`、`ports/delegation.py`、`application/delegation.py`、
  `application/sub_agents.py`、`adapters/delegation.py`、`adapters/tools/delegate.py`。
  `bootstrap/settings.py` 的 `MultiAgentSettings` 新增四个叶子并给
  `validate_agent_budget` 加一条指数校验；`bootstrap/projections.py` 的
  `MultiAgentConfig` 新增四个字段、`task_authorization_envelope()` 新增
  `delegation` 参数；`workflows/agent_profiles.py` 的 `DynamicToolSource` 新增
  `"delegation"` 取值、`researcher_internal` 订阅它；
  `apps/task_worker/composition.py` 装配目录、注册工具、命名 runtime、建**第二个并发池**；
  `config/config.default.toml` 与 `config/ownership.yaml` 各四行；
  `tests/architecture/test_dependency_boundaries.py` 新增
  `test_the_model_tool_loop_has_exactly_one_owner`。
  **不动配置契约**：`config_schema_version` 保持 `1.18`——既有 `[multi_agent]` 段下新增
  带默认值的叶子不抬版，规则见 `docs/configuration.md` §2 的版本表，ADR-080 的
  `context_window_tokens` 与 ADR-081 的 `context_compaction_enabled` 是同一类先例

---

## 1. 背景：一套完整的父子词表，和一个都没有的写入者

和 ADR-081 遇到的是同一种局面——协议先于实现落地，然后没有人发射过它：

- `domain/runs.py:314` 的 `TraceContext.parent_agent_run_id`，全仓唯一一处出现就是这行
  字段声明本身；
- `domain/events.py:644` 的 `AgentDelegated` 与 `:651` 的 `AgentCompleted`，都是
  **durable**，都在事件联合类型里，都带 `child_agent_run_id`，**零发射点**；
- `domain/runs.py:142` 的 `BudgetUsage.merged` 的 docstring 写着 "Aggregate a child
  agent's usage into the parent's"——而它现有的八个调用方做的全是另一件事：把一个节点
  的几次运行加起来；
- `web/src/features/work/workTimeline.ts:79` 早就有 `AgentDelegated: "子代理已委派"`，
  一个后端从来没发过的事件的中文标签。

`docs/known-gaps.md` C-03 把这件事记成"动态 supervisor、spawn、mailbox 未实现"。这份
ADR 只接其中一件——**spawn**，而且是它最小的那一版。另外两件在 §5 明确拒绝。

## 2. 决策：七件事一起动，每一件都先否掉了显然的做法

### 2.1 "唯一 tool loop" 约束的是循环有几份，不是调用有几层

这是整份 ADR 的支点，所以先把它证伪掉再往下走。

权威表述在 `ports/agent_executor.py:1-11`：

> "Exactly one component **owns** a model-tool loop, and in this project it is the
> custom runtime. A graph node **calls** this protocol; it does not run a second loop
> of its own, and no third-party agent executor is registered behind it."

这句话里"图节点调用这个协议"是**合规**的。委派 handler 做的是同一件事：它调用
`AgentExecutor.run`，落到同一个 `ClaudeLikeAgentRuntime` 实例上。子运行是**同一个循环
被第二次进入**，不是第二份循环的实现。配置侧那几个单值 `Literal`（`runtime.executor`、
`workflow.runtime_loop_owner`、`multi_agent.worker_executor`）说的也都是"循环归谁写"，
没有一条提到调用深度。

| 显然的做法 | 为什么否掉 |
|---|---|
| handler 自己持 `ModelPort`，走一遍"调模型 → 解析工具 → 执行 → 再调模型" | **这才是第二个循环。** 而且它会重新实现——或者悄悄丢掉——预算核算、取消契约、`tool_call_id` 配对、上下文天花板这四件 `agent_runtime` 已经做对的事 |
| 在 ADR 里写一段话说明"我们没有第二个循环" | 一段话不会红。上一次同类断言（LlamaIndex 的 agent executor）是靠 `tests/architecture` 里的 import 扫描守住的，不是靠散文 |

所以这条不变量在这份 ADR 里获得了它的**可执行版本**：
`tests/architecture/test_dependency_boundaries.py::test_the_model_tool_loop_has_exactly_one_owner`。
两半，缺一半都能绕过去：

1. **按形状**，只扫 core：AST 找 `async for` 迭代 `.stream(...)` 的模块（并跟随被赋值的
   中间变量，因为 runtime 正是先把迭代器存进变量再迭代的），断言集合恰好是
   `{runtime/agent_runtime.py}`。只扫 core，是因为这个短语在外层有别的意思——
   `apps/api/routes/uploads.py` 迭代的是 HTTP 上传体，模型 adapter 迭代的是 provider 的
   SSE 响应去**生产**事件。
2. **按词表**，扫全树：`agent_workbench.ports.model` 的导入者必须落在
   `MODEL_STREAM_OWNERS` 白名单里。这一半抓得住用 `while` 加手写 `__anext__` 写出来的
   循环，而第一半完全看不见它。

### 2.2 子 agent 的定义类型必须在 `domain`，不能复用 `AgentProfile`

显然的做法是复用 `workflows/agent_profiles.AgentProfile`：它已经把提示词、上下文天花板、
工具天花板表达完整了。**否掉，而且不是审美原因**：

- `tests/architecture/test_dependency_boundaries.py::test_a_tool_binding_does_not_reach_into_the_workflow_layer`
  禁止 `adapters/` 导入 `agent_workbench.workflows`（只豁免 `adapters/langgraph`）。而委派
  handler **必须**在 `adapters/`——它持有一个 `AgentExecutor` 实例。
- `AgentProfile.node` 是 `TaskNodeId`，是写进 checkpoint 元数据的。一次派生出来的运行
  **不坐在任何节点上**，这个字段要么填一句谎话，要么为所有人变成可选。

所以 `domain/agents.py` 里是一个**兄弟类型** `SubAgentDefinition`，而
`workflows/agent_profiles.permitted_tools` 一行不动。两处各写一遍交集，共享的是**方向**
而不是代码——这跟 `WorkspaceScope` 当年从 `workflows` 搬到 `application` 是同一段历史。

### 2.3 handler 拿到的不是 sink，是一个保证有终点的作用域

第一版这个 port 写成了两个动词：`delegated()` 和 `completed()`。它是最自然的形状，**而且
是错的**，错在一条手工测不到的路径上。

`runtime/tool_executor.py:286` 用 `async with asyncio.timeout(limit)` 包住每一个 handler。
超时触发时，`CancelledError` 被抛在 handler **当前的 await 点**——也就是等待子运行的那
一行。写成两个动词的 handler 永远走不到第二个，事件流里于是留下一条
`AgentDelegated` 和它后面的空白，而这正是这个 port 自己写明不许出现的状态：读的人分不清
一个崩掉的子 agent 和一个还在跑的子 agent。

所以终态事件成了 `__aexit__` 的职责，而不是 handler 要记得做的事。这跟
`domain/agents.py` 对深度上限的说法是同一条：**计数器是要信任的东西，缺席的工具是可以读
出来的东西**——一个必须记得写的 `finally` 是要信任的东西，一个 `__aexit__` 是结构。

但光有 `finally` 不够，这里有两个不同的时刻要各自守住，而且**各有一条测试**：

| 时刻 | 守它的东西 | 钉住它的测试 |
|---|---|---|
| 取消**已经**抛出来了（超时途中）。`finally` 里任何裸 `await` 会立刻再抛，什么都发不出去 | `asyncio.ensure_future` 把这次发射**脱钩**成独立 task，不需要这个协程活着 | `test_a_child_cut_off_by_the_tool_timeout_is_still_reported` |
| 取消在这一行**等待慢写入时**才到（Worker 关机，PostgreSQL 写到一半） | `asyncio.shield`。直接 `await` 那个 task 会把取消**级联**进去 | `test_a_cancellation_arriving_mid_write_does_not_lose_the_ending` |

**并且不吞掉取消。** 第一版在 shield 外面套了 `contextlib.suppress(CancelledError)`，
看起来无害，实际上会让一个被取消的 handler **正常返回一个 `ToolResult`**——取消就此丢
失。写那条 shield 测试的时候它当场暴露了（`DID NOT RAISE CancelledError`）。现在
`CancelledError` 照常传播；让传播变得安全的正是 shield 本身，因为发射那一侧已经脱钩了。

### 2.4 名额是同步占住的，不是事后数出来的

`delegate_agent` 声明 `risk="read"`，于是 `validate_risk_consistency` 允许它
`concurrency="parallel"`，于是 `tool_scheduler.plan_tool_batches` 把同一回合里的几个委派
攒进一组，于是 `agent_runtime` 用一个 `asyncio.gather` 同时起它们。

而"检查名额"和"花掉名额"之间隔着**整个子运行**。读 `len(spawned)` 的 handler 在这一批
里每一个都读到 0，每一个都放行。`max_children_per_run = 4` 的部署，一个回合可以起八个。

`DelegationContext.reserve()` 把名额**同步**占住，返回一个 `Reservation`。它刻意不是
`async`：检查与自增之间没有 `await`，而 asyncio 是单线程的，所以没有任何协程能观察到中
间状态。这是这个仓库里最便宜的原子性，不需要锁。失败路径 `release()` 把名额还回去——包括
被取消的那条路径，否则一个还在跑的运行会被一个并不存在的子 agent 占着额度。

**否掉的做法**：把工具改成 `concurrency="exclusive"` 来回避。那会杀掉"一个回合并行派出
几个只读子 agent"这个主要用例，而那正是 `risk="read"` 特意保住的东西。

### 2.5 深度写在工具表上，不写在计数器上

`permitted_child_tools` 在 `child_depth >= max_depth` 时，把**委派工具本身**从返回的清单
里剔掉。孙子拿不到那个工具，所以"这一代不能再派"是一件读两份清单就能看出来的性质，而不
是一件要相信计数器被正确传递了的事。计数器（`DelegationContext.depth`）仍然存在，但它只
是这条剔除规则的输入，不是唯一防线。

配套的启动期校验：`max_children_per_run ** max_delegation_depth` 必须
`<= max_agent_invocation_attempts_per_task`。最坏情况是指数的，而它可以从操作员敲进去
的两个数当场算出来——算在启动，而不是等到跑了一半才发现账单从来没被那个声称在管它的数
管住过。

### 2.6 两个并发池，不是一个可重入的池

这是第一版实现最可能踩的雷，而且它**没有错误消息**：症状是 Task 变慢，不是 Task 出错。

`workflows/task_handlers.py:347` 的 `BoundedParallelExecutor` 用一个信号量包住**整个
`AgentExecutor`**，槽位在整次调用期间一直被握着。父运行在工具调用里等子运行时，它握着一
个槽；子运行排队等一个只有父运行返回才能释放的槽。默认
`max_parallel_agent_invocations = 3`，图自己的 fan-out 就能把池填满，第一次委派挂到运行
截止为止。

**否掉的两种做法**：

| 做法 | 为什么否掉 |
|---|---|
| 让 handler 直接持裸的 `ClaudeLikeAgentRuntime` | 绕过 `BudgetedAgentExecutor`，子运行从此不进账。而 `task_handlers.py` 那段注释正警告过这件事："a later fan-out gets counted without anybody revisiting this file"——绕过去就把这句注释变成谎话 |
| 把信号量改成按深度可重入 | 让"同时有几个模型调用在飞"这个真问题的答案取决于调用栈深度 |

**采用**：第二个池，`multi_agent.max_parallel_child_invocations`（默认 2）。代价被写出来
而不是藏起来——一个开了委派的部署，最多同时跑
`max_parallel_agent_invocations + max_parallel_child_invocations` 个模型循环。

`tests/workflows/test_delegation_pools.py` 有两条测试：一条断言两个池能在耐心之内跑完，
另一条**故意**把它装配成一个池并断言它超时。第二条是第一条不成为同义反复的原因，也是这
个失败模式唯一的自述。

### 2.7 工具是部署事实，所以走 `DynamicToolSource` 而不是静态 profile

`ToolGateway.advertise` 对没注册的工具**抛异常**。所以一个在 `AgentProfile.tool_names`
里静态写死 `delegate_agent` 的 profile，会在绝大多数（没开委派的）部署上，把一个关掉的开
关变成一个**每个 Task 都失败的节点**。

这个坑 `profile_with_dynamic_tools` 的 docstring 早就写过了，为的是 MCP 服务器没起来的
情况。委派是第四个 audience，在完全相同的意义上：它是否存在是部署事实。开关关着的部署，
catalog 是空的，`profile_with_dynamic_tools` 原样返回同一个 profile 对象。

同一条规则还要再往下走一层，而这一层是最先漏掉的：`permitted_child_tools` 求交的对象是
**信封**，不是注册表。信封在提交时冻结，所以它可以点名一个本进程没装配起来的工具，而子
运行的 `advertise` 会因此抛 `UnknownToolError`——子运行在第一轮之前就死了。所以
`SubAgentCatalogue.narrowed_to(registered)` 在装配期做两件事：把没注册的工具从定义里删
掉；**把一个所声明的工具全都不在的定义整个删掉**。一个搜不了东西的 `researcher` 不是
researcher，把它摆在模型面前只会让模型花掉一次委派来被告知这件事。

## 3. 被拒绝的方案

| 方案 | 拒绝理由 |
|---|---|
| 在 `ToolGateway._dispatch` 里把 `EventSink` 塞进 `ToolInvocation` | 改动最小——sink 已经在那一层了。但那等于把整个事件词表交给**每一个** handler，包括只读一个文件的那些：它可以在调用它的运行上发 `AnswerCommitted`。ADR-068 为进度上报拒绝过一次，理由没变 |
| 给 `ToolResult` 加 `usage` 字段做预算回流 | `ToolResult` 会进消息历史给模型看，子运行的账单不是写给模型读的；而且会污染一个被两百多处构造的领域类型 |
| 加宽 `ExecutionContext` 来携带 `stream_id` / 预算 / 深度 | 那个类型是"**策略决策可以依赖的事实**"。`stream_id` 不是任何规则该分支的东西，而一旦它在那里，就会有规则分支到它 |
| 复用 `workflows.AgentProfile` 作为子 agent 定义 | 架构测试当场红（§2.2），且 `node` 字段对派生运行没有真值 |
| 把子 agent 做成第 7 个图节点 | `static_agent_node_limit = 6`，进程直接起不来。更根本的是："派生"的意思就是提交时不知道要派几个 |
| 给子运行开独立 `stream_id` | `apps/api/sse.py` 只认 `stream_id`，鉴权归路由。独立 stream 要新增一整条鉴权路径，前端成本差一个数量级 |
| 让 `runtime` 在 `_run` 开头看 `parent_agent_run_id` 并自行发 `AgentDelegated` | 需要子运行的 sink 能写父运行的 scope，与"一个 sink 一个 scope"冲突；而且 `child_agent_run_id` 这个字段名本身就说明这句话是父在说 |
| 从目录扫描 `.md` 发现子 agent 定义（Desktop 的做法） | 会让"这次运行被允许派生谁"变成一个关于哪个配置文件写在最后的问题。这正是 `ProjectionInput` 与 `DynamicToolSource` 保持封闭词表所拒绝的那件事，而它必须对一份几个月前写下的事件流仍然可答 |

## 4. 不变量

1. **子运行的权限恒为父运行权限的子集。** `permitted_child_tools` 只做交集，
   `child_envelope` 只降不升，`denied_tools` 与 `approval_required_risks` 原样下传。没有
   任何参数能反向放宽——这是签名的性质，不是调用方的纪律。
2. **本 tier 子运行的风险上限恒为 `read`。** `child_envelope` 的 `risk_ceiling` 默认
   `"read"` 且与父取更低者。写工具或需审批工具进入子运行的那一天，**先**要有一条能穿过委
   派的审批路径——父运行此刻卡在 `executing_tools`，而那个状态的唯一出边是
   `recording_results`。
3. **宣布过的子 agent 一定有下文。** `AgentDelegated` 与 `AgentCompleted` 成对，在取消
   路径上也成对（§2.3 的两个机制各守一个时刻）。没有"宣布了然后没有然后"的状态。
4. **深度上限的执行靠工具缺席，不靠计数器。** `child_depth >= max_depth` 时委派工具不在
   子运行的清单里；`may_delegate()` 是第二道，不是第一道。
5. **名额在子运行开始**之前**被占住，而不是结束之后被数出来。** 检查与占用之间没有
   `await`。
6. **一次委派在父运行的账本上只值一次工具调用。** 父的 `max_total_tokens` /
   `max_cost_micro_usd` **看不见**子运行花的任何 token。见 §5。
7. **默认关，且关着的时候什么都不变。** 工具不注册、信封不含它、catalog 为空、
   `profile_with_dynamic_tools` 原样返回同一个对象、`DelegationScopingExecutor` 不入栈。

## 5. 这买到了什么，没买到什么

**买到了**：一次运行可以把一段可隔离的工作交出去，拿回一份报告，而这件事在事件流里留下
一棵可读的树——同一个 `stream_id` 下两个 `run_id`，父说它委派了、子说它开始了、父说它结
束了并带上子花了多少。前端**零改动**就能显示"子代理已委派 / 子代理已完成"两行，因为标签
两个月前就写好了。上下文也真的省了：八次搜索的中间结果留在那个用完就结束的运行里，回到
父运行的只有答案。

**没买到**：

- **不是合并预算。** `_RunLedger` 是 `agent_runtime` 私有的，`ToolResult` 不带 usage。
  一次运行的花费上界因此是 `parent.budget` **加上** 各个子运行整除后的份额之和，而不是
  `parent.budget`。`DelegationContext.spent()` 提供的是**审计数字与拒绝点**，不是合并上
  限。任何把 `max_cost_micro_usd` 读成"这次运行连同它派出去的一切最多花这么多"的说法都
  是错的。
- **不是可写的子 agent。** 不变量 2。
- **不是并发的深度。** 默认 `max_delegation_depth = 1`：一个人要求的运行可以委派，它委派
  出去的不能再委派。
- **不是子运行**之间**的合并上限。** 每个子运行拿到的是父预算除以 `max_children`
  的一份，`max_children` 又限制了份数，所以总量由**构造**就有界了——这里没有再加一道
  聚合闸，因为它买不到任何新东西。`DelegationContext.spent()` 是审计数字，不是天花板，
  它的 docstring 就是这么写的。

  写下这条时顺手纠正了一处口径：`docs/known-gaps.md` 的 C-01 当时仍写着这条上限"只有
  配置，没有账本"，而
  `adapters/persistence/task_registry.py:398` 早已在同一条 UPDATE 里比较并自增、超额抛
  `AgentInvocationBudgetExhaustedError`，`workers/task.py` 判 `dead_letter`，
  `tests/persistence/test_agent_invocation_budget.py` 12 条打真实 PostgreSQL。计数落在
  `task_runs` 行上，所以"跨 retry 与 reclaim"是行本身的性质。这份 ADR 让这个数**更**
  要紧：每个子运行都经过 `BudgetedAgentExecutor` 记一笔，语义从"这张图有几个节点"变成
  "含子代理的总调用数"。C-01 已改判为关闭。
- **不是对着真模型验证过的。** CI 的 `quality` job 离线，本仓没有任何测试打到真实
  DeepSeek。能力梯子停在 **Implemented + Tested**：`delegate_agent` 的工具描述能不能让模
  型在该委派的时候委派、`analyst` 的提示词写出来的第二意见有没有价值，在有一份实测转录之
  前都不得描述成 Demonstrated。

## 6. 为什么默认关

上面每一条不变量描述的都是**结构**：谁能拿到什么工具、谁的风险上限是多少、哪个事件一定
成对。它们全部与"这样做值不值"无关。

一个还没看过自己的 agent 拿现有那张图做了些什么的部署，没有任何东西可以用来判断委派的收
益和它的代价（多一次完整运行的钱、多一层难读的时间线、一个模型自己决定的分支）。所以开
关默认关，而且关着的时候**什么都不变**（不变量 7）——不是"关着的时候会拒绝"，是关着的时
候那条路径根本不存在。

## 7. 怎么验证

| 测试 | 抓住什么 |
|---|---|
| `tests/architecture/test_dependency_boundaries.py::test_the_model_tool_loop_has_exactly_one_owner` | §2.1。这份 ADR 唯一一条关于整个仓库的断言 |
| `tests/domain/test_agents.py::TestTheIntersectionOnlyNarrows` (3) | 不变量 1 的交集半边 |
| `tests/domain/test_agents.py::TestDepthIsWrittenIntoTheToolbox` (3) | 不变量 4，含 1..5 × 1..4 的全扫 |
| `tests/domain/test_agents.py::TestTheEnvelopeOnlyDescends` (4) | 不变量 1、2，含"denial 不会在下传时被丢掉" |
| `tests/domain/test_agents.py::TestTheCatalogueAnswersAtAssembly` (6) | §2.7 的两条收窄规则 |
| `tests/application/test_delegation.py::TestTheChildCountIsTheRunsNotTheCalls` (7) | 不变量 5 |
| `tests/application/test_delegation.py::TestTheBudgetIsCutFromWhatTheParentHasLeft` (6) | 预算的四条不同切法，含 min 的全扫 |
| `tests/adapters/test_delegate_tool.py::TestEveryPathAnswersExactlyOnce` (6) | 每条路径恰好一个 `ToolResult` |
| `tests/adapters/test_delegate_tool.py::TestAnAnnouncedChildAlwaysGetsAnEnding` (2) | 不变量 3，超时路径 |
| `tests/adapters/test_delegate_tool.py::TestTheTerminalEventSurvivesAShutdown` | 不变量 3，慢写入途中被取消 |
| `tests/adapters/test_delegate_tool.py::TestSiblingDelegationsShareOneAllowance` | 不变量 5，真的用 `gather` 并发两个 |
| `tests/workflows/test_delegation_pools.py` (2) | §2.6，含**故意装配错**的那条控制 |
| `tests/config/test_delegation_settings.py` (9) | 不变量 7，以及启动期指数校验 |

**测试不是装饰的，当场验了三次**（做法见 `docs/status.md` 同批那一节）：

1. 删掉 `permitted_child_tools` 的委派剔除、`child_envelope` 的风险钳制、
   `EventDelegationChannel` 的 `AgentDelegated` 发射三行 → **5 failed, 43 passed**。
2. 删掉 `reserve()` 的 `outstanding += 1` → **7 failed**。
3. 把 `await asyncio.shield(emitting)` 换成 `await emitting` → 慢写入那条**单独**红；
   再把整个 `await` 去掉 → 另外 **3 条**红。两个机制各守一个时刻，各自单独可证伪。
