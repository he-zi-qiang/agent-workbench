# ADR-093：控制台可以读到「下一个任务会被允许什么」，读不到「已经提交的那个跑在什么上」

- 决策点：提交表单要告诉人「这台部署允不允许委派、允许几个」，而这四个数在每个
  profile 里都不一样——`config.default.toml` 是关的、四个孩子，`config.code-local.toml`
  与 `config.demo-local.toml` 是开的、六个。前端此前读不到它们中的任何一个：`apps/api`
  下没有 `/v1/config`、`/v1/capabilities`、`/v1/meta` 这一类路由，13 个 router 里
  一个都没有，`multi_agent` 在 `routes/` 下零命中。于是表单只有两条路——写死一份数
  （在三分之二的部署上是错的），或者什么都不说。要不要把进程配置投影成 HTTP 事实；
  如果要，答的是**哪一份**配置，以及为什么不顺手把子代理目录一起答了
- 状态：**接受**。新增 `GET /v1/tasks/capabilities`，只返回一件事：
  `delegation` 的五元组（`enabled` 与四个上限），来自
  `dependencies_of(request).config.multi_agent`，也就是**这个 API 进程启动时投影出来
  的那一份**。
  **明确不做**：不投影子代理目录（§3）、不投影 `max_agent_invocation_attempts_per_task`
  （§2）、不接受任何提交级的委派覆盖（§5）、不给这条路由加轮询或推送（它答的是一个
  进程生命周期内的常量）
- 日期：2026-08-29
- 影响：`apps/api/routes/tasks.py` 新增 `DelegationCapabilities`、
  `TaskCapabilitiesResponse` 与 `GET /capabilities`（**声明在 `/{task_id}` 之上**，
  与 `/triage` 同一条理由，§6）；`web/src/api/types.ts` 新增两个接口；
  `web/src/api/client.ts` 新增 `getTaskCapabilities`；
  `web/src/features/work/DelegationScope.tsx` 新增；
  `web/src/features/work/WorkPage.tsx` 在「高级设置」里挂上它；
  `web/src/styles/app.css` 新增 `.aw-delegation-scope` 一族；
  新 `tests/api/test_task_capabilities_api.py`（5 例，**不需要 DSN，进 CI**）；
  `web/src/features/work/WorkPage.test.tsx` 新增 3 例。
  **不动配置契约**：没有新增任何配置叶子，`config_schema_version` 保持 `1.19`
- 依赖：[ADR-040](./0040-a-task-pays-before-it-calls.md)
  （每个 Task 冻结自己的 `run_semantics_snapshot`——本 ADR 的整个边界就是从它划出来的，
  见 §2）、
  [ADR-082](./0082-a-delegation-is-a-run-not-a-new-loop.md)
  （委派本身，以及「派给谁、派几个是模型在运行途中决定的」这条事实，见 §5）、
  [ADR-089](./0089-code-may-delegate-without-joining-the-plane.md)
  （把 `MultiAgentConfig` 挂到 `ApiRuntimeConfig` 上的那一次；本 ADR 只是给它开了一个
  出口，没有新增任何它没投影的字段）

---

## 1. 缺口：一个只能靠猜的数

「这台部署会派几个子代理」在服务端有确定答案，在浏览器里没有任何取法。

实测（2026-08-29）：

| profile | `delegation_enabled` | `max_children_per_run` |
| --- | --- | --- |
| `config.default.toml` | `false` | 4 |
| `config.code-local.toml` | `true` | 6 |
| `config.demo-local.toml` | `true` | 6 |

前端能探测部署能力的唯一既有手段是「路由挂没挂」（`serves_search` / `serves_chat` /
`serves_code` 决定 router 是否 include，一个没挂的能力回 404）。而 `tasks` 与
`approvals` 是**无条件挂载**的——所以「这个部署开没开委派」在 Task 这一侧连 404 这个
信号都没有。

## 2. 决定：答「下一个任务」，不答任何一个已经存在的任务

这是本 ADR 唯一真正需要划的那条线，因为两个数长得一模一样而含义相反。

ADR-040 把整段 `[multi_agent]` 冻进每个 Task 行自己的 `run_semantics_snapshot`：一个
Task 跑在**它提交那天**的配置上，不是这个进程今天碰巧配成的那一份。所以：

- **提交表单**要的是进程的当前配置。它描述的是还不存在的那个 Task，而那个 Task 一旦
  提交，拿到的就是这一份。
- **任务详情页**要的是那个 Task 自己的快照。把今天的配置摆在一个三天前提交的 Task
  旁边，是在报一个它从来没见过的数。

本 ADR 只做第一件。路由的 docstring、响应模型的 docstring、前端 `types.ts` 的注释三处
都把这句话写下来了，因为这是一个**读的人不会主动怀疑**的错误：两个数都对，只是不是同
一个问题的答案。

同一条线解释了 `max_agent_invocation_attempts_per_task` 为什么不在这里。
`bootstrap/projections.py::MultiAgentConfig` 早就写明它为什么不进投影——它由
`reserve_agent_invocation` 在一次行锁里比较并自增，而**上限读自 Task 行自己的快照**；
那段 docstring 的原话（英文）说的是：把它投影在这里，等于把同一个数的第二个、会分叉的
来源，摆在执行它的那段代码一个 import 之外。本 ADR 没有推翻这句话，它是照这句话划的线。

反过来，委派那四个数**可以**答，理由也在那段 docstring 里：深度、孩子数、子池大小
全都在**一次运行、一个进程**里被回答，由一个活得和这次运行一样长的 scope 管着。

## 3. 为什么不答「有哪些子代理」

这是本批最容易顺手做错的一件事，而它错得很安静。

子代理目录**不是配置**，是由「这个进程是什么」在代码里选的：Task Worker 装
`DEFAULT_SUB_AGENTS`（`researcher` + `analyst`），API 进程装 `CODE_SUB_AGENTS`
（`explorer` + `analyst`，ADR-089），然后各自再按本进程实际注册的工具
`narrowed_to` 收窄一次。

于是一个「诚实地从自己手里取数」的实现会答出 `explorer`——而问的人正在填一个 **Task**
的提交表单，Task 那一侧永远不会有 `explorer`。这不是少答，是答错，且错得像对的。

三条路都被否掉了：

- **答 API 进程手里的那份**：错的，理由如上。
- **答一份写死的清单**：那是把「进程决定目录」这条规则复制成两份，其中一份没有人会
  跟着改。`ComputerPage` 的 docblock 已经因为同一种手抄收过两次账。
- **答一个空列表**：另一种形状的假话——它读起来是「这台部署没有子代理」。

所以答案是**这个进程不是知道这件事的那个进程**，而控制台不会拿到一份没有人能站在
后面的名单。`tests/api/test_task_capabilities_api.py` 里有一条测试把这个缺席钉成断
言（响应的 key 集合恰好是 `{"delegation"}`，且序列化后不含那三个名字），因为一条只
存在于散文里的克制，下一个人补一行代码就能推翻。

要真的答它，得由知道的那一方来答——那是另一份 ADR 要回答的问题，不是这一份。

## 4. 委派关着的时候，两种原因折成一种

`ApiRuntimeConfig.multi_agent` 是 `Optional`，所以服务端有三种内部状态：投影是
`None`（一个早于 ADR-089 的投影）、投影在但 `delegation_enabled = false`、投影在且开着。

对外只有两种。前两种答同一个东西，因为它们是**同一个问题的同一个答案**：下一个任务
会不会派子代理——不会。`dependencies.py` 在装配 Code 会话时已经这么折过一次，那里的注释
（英文）写的是：一个 config 说不出委派开没开的进程，没有资格猜它是开着的。

第三种状态如果暴露出去，控制台就得为它想一个渲染。而那个渲染只能是前两种之一，或者
是一句「说不清楚」——后者会出现在每一台跑着旧投影的部署上，且没有任何人能据此做任何事。

关着的时候三个树上限送 `1`、token 上限送 `0`。这不是随手填的：`0` 个孩子读起来是
「一台配到极限的部署」，而实际是「这棵树不存在」；token 那一栏送 `0` 是因为**没有一次
调用可以被限**。前端也照这条口径写：关着时**不渲染那几个数**，只说一句话。

## 5. 为什么不做提交级的委派覆盖

设计稿最初的形状是「提交页让人配置编队」。做不成，而且原因不是工程量。

**派给谁、一轮派几个，是模型在运行途中决定的。** 图的形状在提交那一刻冻住
（ADR-036），委派发生在某一个节点的运行**内部**（ADR-082），`multi_agent.topology`
是钉死的 `Literal["fixed_langgraph"]`。所以「先摆好三个 agent 再开跑」这一步在这个控
制平面里没有位置——一个能填的输入框会承诺一件做不到的事。

即使只让人**下调**上限（`docs/configuration.md` §8 允许的那一类），代价也不在表单上：

- `TaskInput` 参与 `canonical_bytes` 与提交幂等指纹，加字段就是动幂等键的一半；
- 覆盖值必须冻进 Task 自己的 `run_semantics_snapshot`，并由执行处读快照而不是读进程
  配置，否则 crash 恢复之后按哪一份跑就成了一个没有答案的问题；
- 几个上限之间有**启动期交叉校验**（开着委派时
  `max_children_per_run ** max_delegation_depth ≤ max_agent_invocation_attempts_per_task`，
  超了配置加载即失败），表单必须在提交前跑同一套校验，否则做得出一份提交得下去、
  Worker 起不来的配置。

这三条都能做，都不该顺手做。所以本 ADR 只做**读**：人看到范围，模型在范围里选。

## 6. 路由声明在 `/{task_id}` 之上

FastAPI 按声明顺序匹配，所以一条声明在 `/{task_id}` 之下的字面量路径，就是一个 id
恰好是那个单词的 Task。`/triage` 的注释已经写过这件事一次；本批是第二条字面量路径，
所以它被钉成了测试而不是又一条注释——那条测试里的 task service 一旦被问就抛，于是回归
是一个 500，而不是一个看起来很合理的 404。

## 7. 被拒绝的方案

**把这几个数挂到 `TaskView` 上。** 最省一条路由，也最直接违反 §2：`TaskView` 描述一个
已经存在的 Task，而这几个数描述下一个。把它们并排放在同一个响应里，等于邀请读者把
今天的配置当成那个 Task 的。

**挂到 `GET /v1/tasks` 列表响应上，照 `GET /v1/evaluation/reports` 的
`runs_enabled` / `how_to_run` 的先例。** 那条先例是对的先例，但形状不一样：评测的报告
列表和「能不能跑」是同一个页面同一次渲染要的两件事，所以合成一次请求，页面就不会先画
一个按钮再收回去。而任务列表是**轮询**的（未终态时 2.5 秒一次），把一个进程生命周期内
的常量挂在轮询响应里，是每 2.5 秒重问一次不会有新答案的问题。

**做成 `GET /v1/config`，把整份投影吐出来。** 会答出一堆没有人问的东西，其中不少
（DSN 主机名、artifact 根目录、model profile 名）是这个端点没有理由披露的。一条按问题
命名的路由，比一条按数据源命名的路由更容易保持诚实。

**什么都不做，让前端写死一份数。** 这是本 ADR 之前的状态。它在
`config.default.toml` 上是对的，在这个仓库自己的两个 console profile 上都是错的。
