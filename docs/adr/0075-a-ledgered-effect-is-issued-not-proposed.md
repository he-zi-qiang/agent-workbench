# ADR-075：账本记的是被发起的效果，不是被提议的效果

- 决策点：[ADR-025](./0025-mcp-adapter.md) §2.7 把 `retryable_effects = false` 的
  server 挡在 Task 之外，理由是重放；同一节又给自己留了一句重开条件——「真正的
  exactly-once 需要远端幂等键，或者一个能持久化并重放完整 `ToolResult` 的账本，
  留作单独的工作包」。ADR-070 落地 computer use 时照此声明了 `false`，于是六个屏幕
  工具在 Task 这条路上**两端都进不来**。这条重开条件到底该不该兑现
- 状态：**接受**。拒绝保留，并从「两个 `continue` 撞在一起的后果」变成一条有名字、
  有护栏、有测试的决定。同时**收窄 ADR-025 §2.7 与 §5 的重开条件**：那句重开条件
  兑现了也解不开这把锁，因为**卡住的是键，不是载荷**
- 日期：2026-08-23
- 影响：`domain/runs.py`（`TraceContext.lease_epoch`）、
  `workflows/task_handlers.py`（`_context_for` 收下租约）、
  `runtime/agent_runtime.py`（转发 epoch；接受端校验 offered 集合）、
  `runtime/tool_gateway.py`（`advertise` 拒绝带 `operation_key` 的绑定）、
  `apps/task_worker/composition.py`（装配期拒绝启动一个把进账工具写进 profile 的
  部署）、新增 `tests/config/test_local_computer_profile.py` 与
  `tests/apps/test_task_worker_ledgered_profile_guard.py`。**不抬配置 schema**：没有任何
  一份配置能要求的东西发生变化，`retryable_effects` 语义不变、仍然必填无默认
- 依赖：[ADR-025](./0025-mcp-adapter.md)（本 ADR 收窄它 §2.7 的重开条件与 §5 的
  被拒方案，**不推翻 §2.7 的决定**）、
  [ADR-015](./0015-export-authorization.md)（业务键的成形
  规则——变化的内容进参数、不进键——本 ADR 正是被这条规则挡住的）、
  [ADR-070](./0070-a-permission-is-about-a-window-not-an-application.md)（computer
  use；它第三道检查「动作发生前重读最前面的是谁」也独立否掉了提交期信封的替代方案）、
  [ADR-059](./0059-a-retryable-failure-is-released-not-settled.md)（可重试失败被
  释放而非了结——它让节点重放成了例行事件，不只是崩溃恢复）

## 1. 背景：这条拒绝此前没有主人

ADR-025 §2.7 写下的是一条关于**绑定形状**的规则：只有明确声明「重放安全」的 server
才进 Task 图。ADR-070 给 computer use 声明了 `false`，并在配置文件里写清了理由。

于是今天的行为是这样的——两处，各一个 `continue`：

```
bootstrap/projections.py:155     configured_mcp_tool_names()  → 名字不进授权信封
apps/task_worker/composition.py:871  → 记 mcp_server_skipped_nonretryable，不建绑定
```

2026-08-23 实测：Worker 跑在 `config.computer-local.toml` 上，注册的 MCP 工具数是
**零**。

这个结果是对的。问题是它**没有主人**：跑在这份 profile 上的断言一条都没有，删掉任何
一个 `continue`，六个能移动光标、能按键的工具就会进入这份 profile 下每一个新 Task 的
授权信封，而且一条测试都不会红。一条正确的拒绝和一个凑巧的疏漏，在代码里长得一模一样。

## 2. 决策：卡住的是键，不是载荷

ADR-025 §2.7 给自己留的重开条件有两半：**远端幂等键**，或者**一个能持久化并重放
完整 `ToolResult` 的账本**。前一半在这里直接不适用——屏幕没有幂等键可给，一次
点击不是一个可以带着 key 重放的请求，远端就是那台机器本身。所以只剩后一半，
而那句话诊断的是账本的**载荷**——`succeeded` 行只存状态和一个可选 artifact id，拿它挡
第二次调用会把行内结果永久丢掉，模型的上下文重建不出来。诊断没错。

但把载荷补上，锁还是开不了，因为账本查行用的是 `(task_id, operation_key)`，而**这个
键在模型提议的调用上推导不出来**。

推导函数看得见的全部东西就这些：

```
ToolCall        tool_call_id, tool_name, arguments, model_call_id
ExecutionContext principal, envelope, agent_run_id, policy_identity,
                 task_id, workflow_thread_id, graph_node_id, lease_epoch
```

一个都不能用来区分「同一个意图被重放」和「一个长得一样的新意图」：

- `tool_call_id` 每轮重铸。禁止用它的话不在 ADR-015 里，在它落地的那份端口上：
  `ports/tool_executions.py` 的模块注释写着「键是**业务**键，不是 `tool_call_id`」，
  理由就是重试会为同一个意图铸一个新 id；
- `agent_run_id` 每次节点执行都重铸，**包括每次 resume**；
- `lease_epoch` 每次回收都变，它是栅栏本身，不是键的配料；
- `graph_node_id` 是**每节点**一个字面量（`"work"`），该节点里每一次调用共用它。

于是只剩两种键，两种都是错的：

**参数派生的键**会把合法的第二次点击吞掉。`runtime/agent_runtime.py` 的
`MAX_IDENTICAL_CALLS = 3` 是**故意**允许同一 (工具, 参数) 在一次 run 里出现三次的；
键若来自参数，第二次点击会命中第一次的 `succeeded` 行，账本回答「已经做过了」，**那一
下没有点**。模型收到一个成功，屏幕上什么都没发生。

**位置派生的键**——「本节点第 n 次进账的调用」——设计过，并且是按**正确性**否掉的，
不是按工作量。它毁掉的正是账本的重试同一性：

> `left_click(400,300)` 在 epoch 3 的位置 5 记下 `intended`，Worker 在派发中途死掉。
> epoch 4 重放，模型被重新调用，这一次它先做了别的，同一个点击落在**位置 6**。
> 位置 6 是一个**新键**，账本没见过，于是**这一下真的又点了一次**。

正是账本存在的意义所要防的那个重复。位置键把「这次派发到底发生没发生」这个问题，换成
了「模型这次会不会把它排在同一格」——而模型不保证。

还有一条独立的、更早生效的理由：**模型没有视觉通路**。
`domain/messages.py:83` 的 `ContentBlock` 是 `TextBlock | ToolUseBlock | ToolResultBlock`，
没有 image 成员；`map_remote_result` 把每一个 `RemoteBinaryBlock` 都送进 artifact。
所以即便只放行只读的那一半，`screenshot` 递给模型的是一串分辨率文字。那不是「让 Agent
看屏幕」，那是让它**蒙着眼睛开车**。

最后是账本再完美也修不了的那件事，`config.computer-local.toml` 自己早就写着：**重放的
点击落在此刻光标底下的东西上**。exactly-once 消除的是重复，它不能让重放的坐标重新有
意义——检查点里没有那块屏幕。

### 2.1 替代的重开条件

ADR-025 §2.7 那句「持久化并重放完整 ToolResult」，本 ADR 用下面这句替换：

> 一个 `retryable_effects = false` 的 MCP 工具进入 Task，只能经由一个**自己发起该
> 调用的确定性节点**——`adapters/tools/task_export.py` 就是这个形状——而不能由模型
> 提议。若要让模型提议，还需先有一条今天不存在的视觉通路。

`export_artifact` 之所以能带 `operation_key`，不是因为它的载荷特殊，是因为它的**意图
在图上是唯一的**：键就是 `export:{task_id}`，一个 Task 一次，由图的构造保证。它不需要
区分「第几次」，因为它没有第几次。

## 3. 护栏：把疏漏换成决定

补上这条拒绝的过程中，发现运行时**自己**把账本关掉了，而且和 computer use 无关。

`ClaudeLikeAgentRuntime._execution_context` 建的上下文里没有 `lease_epoch`；
`ToolGateway._invoke_ledgered` 对说不出 epoch 的调用一律拒绝。也就是说：**任何模型提议
的进账工具都派发不了**，账本——`docs/configuration.md` §3 里那条「始终开启」的不变量
——从工具循环里根本够不着。没人发现，因为仓库里唯一的进账工具由确定性节点发起，那条
路自己建上下文、自己把 epoch 填进去（`task_handlers.py:1098` 的注释写着为什么）。

这是个缺陷，修了：`TraceContext` 收下 `lease_epoch`，`_context_for` 从**认领时的租约**
取它（不是从 Registry 行取——丢了租约的 Worker 会从行里读到它继任者的 epoch，然后拿着
它通过下游每一道栅栏），运行时转发它。

但修好它会顺手**拆掉一道护栏**：epoch 到位之后，一个被放进 profile 的进账工具，就只凭
模型一句话就能派发了。那道护栏此前是靠疏漏立着的。所以同一批把它换成明写的：

```
ToolGateway.advertise  →  binding.operation_key is not None  →  UnknownToolError
                          "this tool records an external effect and is issued by a
                           graph node, never offered to a model"
```

拦在 `advertise` 而不是拦在派发，因为**给模型看过又拒绝的工具，模型会绕路去找**。
它落地即空转——今天没有任何 profile 写着进账工具的名字——这正是想要的形状：
一道在事情发生前就已经站好的护栏，而不是出事后补的。

抛 `PolicyDeniedError` 而不是 `UnknownToolError`，两个码分得清两件事：进程没注册的
名字是 `unknown_tool`，注册了但不给模型的名字是 `policy_denied`。第二种报成第一种，
会把来查问题的人送去找一个「漏注册」，而那里什么都没有。

**它拦的只是「profile 写了这个名字」这一种走法。** 模型凭空说出一个进账工具的名字，
`advertise` 根本看不到——那条路上把它挡住的是 §3.1 的 offered 集合校验，而不是这道
护栏。两道加起来才闭合：一道管**给出去的**，一道管**收回来的**。

`export_artifact` 不受影响：它从不走 `advertise`，它直接驱动
`propose/prepare/authorize/invoke`。

### 3.1 顺带补上的第二个缺陷：offered 集合此前不具约束力

`permitted_tools` 用 Task 信封去收窄 profile，注释写着「子 Agent 的权限不可能超过它
所属的 Task」。那个交集此前只作用在**给模型看什么**上。接受一次调用时走的是
`EnvelopePolicyEngine.decide`（`adapters/policy/envelope.py:38`）：它用**整个 registry**
解析名字，然后问**Task 级**信封准不准。

于是一个同时注册了两个 audience 工具的 Worker 里，researcher 的节点调用 writer 的工具
会**通过**——信封说准（Task 确实准），offered 说什么没人问。两道检查都报告自己满意。

现在接受端也校验：不在 `request.tool_names` 里的调用被拒，`policy_denied`，并且告诉
模型用它拿到的那些工具。拒绝而不是抛错，因为模型提了这个调用，它欠模型一个能据以行动
的答复。

## 4. 后果

**六个屏幕工具仍然进不了 Task，这不再是暂时状态。** 想改的人现在要先推翻本 ADR，而不是
删掉一个 `continue`。`tests/config/test_local_computer_profile.py` 的 11 条把它钉住了，
其中最要紧的两条是：屏幕工具不进授权信封，以及这份 profile 的风险上限仍是 `write` 而
不是被抬成 `external`——放行它们不只是多六个工具，是把这份 profile 下**每一个** Task
的上限抬高，包括那些根本不碰屏幕的。

**computer use 的能力阶梯不动。** 服务器本身可用、可验证（`scripts/dev.sh computer-check`，
本机），但那是「用 MCP 客户端直连」这条路，不是 Task。没有证据支持更高的一格，所以
`docs/architecture-baseline.md` §17 里不给它加行。

**账本从工具循环里够得着了，而且立刻被护栏挡住。** 这看起来像原地打转，其实不是：此前
是「因为一个字段没传，所以碰巧没人能用」，现在是「因为它是被发起而不是被提议的效果，
所以不给模型」。两句话在代码里长得一样，在下一个人手里不一样。

**这类接线错误当场停住进程，不是每个 Task 失败一次。** `advertise` 由每一次 agent run
调用，它抛的 `PolicyDeniedError` 被 `ClaudeLikeAgentRuntime` 接住，变成一次**失败的
run**。单靠它，一个把进账工具写进 profile 的部署会起得来、看着健康，只是某个节点每个
Task 都挂——读起来像工具坏了，而不像一份根本不该被写出来的 profile。所以装配期另有
一道：`apps/task_worker/composition.py` 的
`_assert_no_profile_offers_a_ledgered_tool`，在 registry 和 roster 都到手之后跑，命中
就不让进程起来。和 `ToolGateway.__init__` 拦「注册了进账工具却没给账本」是同一个形状
——够不上协议的进程不该启动。

gateway 自己做不了这件事，理由没变：装配它的时候没有人把 profile 交给它，profile 属于
`workflows/agent_profiles.py`。能做的地方是 Worker 的组装根，那里两半都看得见。

两处并存不是重复，各自看得见对方看不见的东西：

- 装配期这道读的是**加宽之后**的 profile——profile 自己的 `tool_names`，加上本次组装
  真正拿到的 dynamic 目录。只读声明会放过一台 Worker：profile 一个字没改，是它拿到的
  目录里多了一个进账工具。它还走**每一份 roster**，不只走这台机器会注册的那些，因为
  `graphs` 会在嵌入运行时没加载时收窄到 v2，一道跟着它走的检查会让同一个接线错误在
  这台机器上起得来、在下一台上起不来；
- `advertise` 那道留着，因为装配期看不见不经组装根加宽的 profile，也看不见自己建
  request 的调用方。一道管**部署**，一道管**运行**。

两处都得故意把情形造出来才测得到——ADR-025 §2.6 给 MCP 绑定钉死了
`idempotency="safe"`，`ToolBinding` 拒绝把它和 operation key 配在一起，仓库里唯一的
进账工具由 `export` 节点发起、不进任何 profile。
`tests/apps/test_task_worker_ledgered_profile_guard.py` 钉住判断本身（含「不是这个
profile 声明的 dynamic 源不算它的事」和「工作区工具照旧放行」这两条边界），
`tests/apps/test_task_worker_entrypoint.py::test_a_ledgered_tool_in_a_profile_stops_the_process_starting`
钉住它确实拦在进程起来之前。**一道拦不到任何东西的护栏，正是它赶在错误之前到场的
样子。**

## 5. 被拒绝的方案

**位置账本**（键 = 节点内第 n 次进账调用，配一个能重放完整 `ToolResult` 的账本）。
设计完整、能答上 ADR-025 §5 的两条反对，仍然按 §2 那个 epoch 3 位置 5 / epoch 4 位置 6
的走查否掉：它把「效果发生过没有」换成了「模型会不会排在同一格」。

**只放行只读的两个**（`screenshot`、`request_access`）。`request_access` 不是只读的
——它写会话级 allowlist；而 `screenshot` 在没有视觉通路的今天递给模型的是一串文字。
放行它等于在能力表上写下一件做不到的事。

**提交期信封替代 ADR-070 的第三道检查**。直接违背 ADR-070 §2：tier 是在**动作发生前**
对着此刻最前面的应用重算的，从不缓存。冻结在提交期的信封永远替代不了它，门禁留在
computer MCP server 进程里。

**让 Task 崩溃后不恢复（fail-closed 重观察）**。这条最接近对的——它正面回应了「屏幕已经
变了」这件账本修不了的事。否掉它的不是它错，是它要引入一个新的非终态 `TaskStatus`，
而那种改动要横穿 Registry、两处 CHECK 约束、恢复路径与前端；换来的能力仍然建立在
模型看不见屏幕之上。等视觉通路存在了，它是第一个该被重新拿出来的方案。
