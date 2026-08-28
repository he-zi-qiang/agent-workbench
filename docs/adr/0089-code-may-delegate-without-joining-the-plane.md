# ADR-089：Code 可以委派，而不必加入协调面

- 决策点：2026-08-28 的用户反馈——「我希望 code 模式也可以使用（多 agent），而且我把
  code 作为最通用的 agent，是权限和功能最全面和强大的」。查下来这句话在代码里只错了
  一半：Code 确实拿着这台机器上最宽的工具面（项目目录读写、`project_run`、沙箱、网页
  检索），但它是唯一**不能把活分出去**的那一个——委派整条挂在 Task Worker 上，
  API 进程里一行都没有
- 状态：**接受**。委派接进 Code：`CODE_SUB_AGENTS`（`explorer` 只读项目目录、
  `analyst` 无工具）、`FencedDelegationChannel`、`ApiRuntimeConfig.multi_agent`，
  组装全部落在 `apps/api/dependencies.py`。
  **明确不做**：不给 Code 任何协调面（无 lease、无 reaper、无 checkpoint、无 task
  registry）、不让子代理持有工作集工具、不让子代理写任何东西、不动
  `config_schema_version`
- 日期：2026-08-28
- 影响：`adapters/delegation.py`（`FencedDelegationChannel`）、
  `application/sub_agents.py`（`EXPLORER`、`CODE_SUB_AGENTS`）、
  `bootstrap/projections.py`（`ApiRuntimeConfig.multi_agent`）、
  `apps/api/dependencies.py`（全部接线）、`config.code-local.toml` /
  `config.demo-local.toml`
- 关联：[ADR-082](./0082-a-delegation-is-a-run-not-a-new-loop.md)（委派是一次运行）、
  [ADR-0078](./0078-a-file-you-have-not-read-is-not-yours-to-overwrite.md)（读回执）、
  ADR-072/073（项目目录）

---

## 1. 头条：那两个架构测试挡的不是委派

`test_code_has_no_coordination_plane.py` 的禁止导入清单是：

```
ports.task_registry            adapters.persistence.task_registry
ports.execution_guard          adapters.persistence.execution_guard
ports.approvals                application.approvals
adapters.langgraph
```

**委派一个都不在里面**，而且不是疏忽——把委派的五个模块逐个查过依赖，它们只用
`domain/`、`ports/agent_executor`、`ports/cancellation`、`ports/event_log`、
`adapters/events`，全是 Code 本来就在用的。

原因在 ADR-082 的形状里：**一次委派是一次运行，不是一个新的循环。** 子运行同步跑在父
运行的那次工具调用**里面**，不排队、不落检查点、不拿租约，父运行返回它就结束了。它随
进程一起死——而「随进程一起死」正是 Code 那段模块 docstring 里逐条列出来的前提本身。

那份 docstring 说得很清楚，每一个协调面部件都是「一件在崩溃之后必须能**释放**某样东西
的设施，而有一个的代价是全都要有」。委派不释放任何东西，因为它不持有任何东西。

## 2. 真正的危险，和它藏的地方

有一处会破，而且是那种不会有人在 review 里看见的破法。

`CodeSessionService` 的签名里写着 `sink: ProcessOnlySink`，**写在签名里而不是注释里**，
理由 `answer_release.py` 自己讲了：这样「把一个普通 sink 递给这个服务，类型检查就不
过」。它挡的是三个「答案已发布」事件——一个 Code 会话的流上出现 `AnswerCommitted`，
会告诉那条流的每一个读者、以及下游每一个消费者，有东西越过了一道**根本不存在**的栅栏。

而 `EventDelegationChannel.sink_for_child` 返回的是：

```python
return ScopedEventSink(log=self.log, scope=EventScope(...))
```

**一个裸的普通 sink。** 于是在 Code 会话里，父运行发不出答案事件，**子运行可以**——
而那个为了「不可能被忘记」而存在的类型，从头到尾没有被咨询过一次。

`FencedDelegationChannel` 就是为这一条存在的：**子运行继承父运行的栅栏。** 一行，放在
唯一一个「委派执行器能拿到的东西」上，所以没有第二条路可以忘记它。宣告事件
（`AgentDelegated`/`AgentCompleted`）原样转发——它们不是答案事件，而且落在**父**运行
上，父运行已经过了它自己那道栅栏。

## 3. 子代理：`explorer` 只读，而只读是性质不是起点

`EXPLORER` 持有 `project_read` / `project_grep` / `project_list`，**没有写**。两条理由，
都不是胆小：

* **ADR-0078 让一次读成为一张回执**——没读过的文件不归你覆盖——而回执归**做了那次读**
  的运行。子代理读过的文件，父运行仍然写不了，可模型看起来像是读过了。
* **两个子代理同时往一个目录里写，没有版本可以串行化。** 工作集那一侧直接禁掉
  （`WORKSPACE_TOOL_NAMES`），项目这一侧没有 manifest 可以拿来拒绝，所以拒绝只能是
  **工具箱本身**。

`WORKSPACE_TOOL_NAMES` 那条禁令**继续生效**：工作集工具不归子代理持有，理由是
`domain/agents.py` 原来那段——委派是另一次 invocation，而版本钉死是按 invocation 的。

## 4. 为什么组装在 `dependencies.py`，而且是每回合一次

**两件事，都不是风格。**

**落在 `dependencies.py`**：`test_code_has_no_coordination_plane.py` 扫的是
`application/code_*.py` 这几个 glob。把委派接线写进 `CodeSessionService` 会让那些文件
里出现委派的 import——即使它今天不违规，也是把一个组装细节搬进了被扫描的那片地方。
`executor_for` 是一个 `Callable`，交给它一个**已经包好**的执行器，Code 那一侧一个字
都不用改。

**每回合一次**：Task Worker 的委派对象是进程级的，因为它的子栈对每个 Task 都一样。
Code 的不一样——子运行走的 gateway 带着 `code_approvals.gate_for(scope)`，而那个 scope
是**某一个会话的**。一个进程级的 `DeferredExecutor` 只能绑到某一回合的审批门上，然后被
其余每一回合使用；`bind()` 拒绝第二次调用，正是为了让这种事变响。

每回合一次也就是 Code 本来的样子。这里没有任何东西活过这一回合，而那句话和「无租约、
无 reaper、无检查点」是同一句话。

## 5. 两个注册表，一个名字

`CodeSessionService` 拿一个 registry 只做一件事：

```python
return {spec.name: spec.risk for spec in self.tools.specs()}
```

**只读 spec，从不经它执行。** 所以委派工具在这里有两个绑定：

| 绑定 | 用途 | 生命周期 |
|---|---|---|
| `_code_delegate_spec_binding` | 供 `code_risk_ceiling` 读 `risk` | 进程级 |
| `_code_runtime` 里那个 | 真正执行，持有这一回合的子栈 | 一回合 |

**能跑东西的那个是被限定在一回合里的那个。** 而 spec 那个必须存在，否则反过来会坏：
信封里有一个名字、registry 里没有它的 spec，`code_risk_ceiling` 会拒绝推导天花板，
整个回合在开始前就死了。

`delegate_agent` 声明为 `read`，所以它的出现不抬高任何天花板。

## 6. 没有账本，而这是前提

Task 那一侧的子栈裹着 `BudgetedAgentExecutor`，它对着 Task registry 记账。**Code 这里
没有，也不许有**——那正是第 1 节那张禁止清单的第一行。

所以兜住一次 Code 委派的是 `max_children_per_run` 与 `max_delegation_depth`，由
`DelegationScopingExecutor` **在内存里**数，随进程消失。这跟 Code 的其它每一条前提是
同一种东西：崩溃之后没有东西需要被释放，因为没有东西被持有。

子栈有自己的信号量，理由照抄 Task Worker：`BoundedParallelExecutor` 在整个 invocation
期间占着槽位，所以一个在工具调用里等待的父运行会一直占着一个，而一个在等只有父运行
返回才能释放的槽位的子运行，就是一个死锁。

## 7. 被拒绝的方案

| 方案 | 拒绝理由 |
|---|---|
| 复用 Task Worker 的 `_delegation_channel` | 它把普通 sink 递给子运行，也就是第 2 节那个洞 |
| 让子代理也能写项目目录 | 读回执（ADR-0078）归做了读的那个运行；且两个子代理同写一个目录没有版本可串行化 |
| 让子代理持有工作集工具 | `WORKSPACE_TOOL_NAMES` 已经禁了，理由（版本按 invocation 钉死）在 Code 这边一模一样 |
| 把接线写进 `CodeSessionService` | 会让被架构测试扫描的那几个文件里出现委派 import |
| 进程级的委派对象 | 子栈的 gateway 带着一个会话的审批门；`bind()` 拒绝第二次正是为了让这件事变响 |
| 给 Code 一个 `BudgetedAgentExecutor` | 那要一个 task registry，而那是禁止清单的第一行 |

## 8. 不变量

1. **Code 仍然没有协调面。** 无租约、无 reaper、无检查点、无 task registry。
2. **子运行继承父运行的栅栏。** 一个 Code 子运行发不出答案事件。
3. **子代理不写。** 项目目录只读，工作集工具一件都没有。
4. **一次 Code 委派的上限在内存里**，随进程消失——因为没有账本，也不该有。
