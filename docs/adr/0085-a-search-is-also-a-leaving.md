# ADR-085：一次搜索也是一次离开

- 决策点：`config.demo-local.toml` 记着 2026-08-26 的用户反馈与当时的答复——打开
  `policy.shell_tools_enabled`，让项目态回合经 `project_run` 够到宿主 shell、因而够到
  网络，代价是**每一次调用停下来问人**。剩下的问题是：Code 会不会有一件**一等的**
  联网工具，还是"联网"永远等于"写一条命令、等一个人"。以及如果要有，它凭什么进
  Code 的工具目录、旗子放在哪一层、`external` 这一档的审批语义怎么办
- 状态：**接受**。`web_search` 的 `risk="external"` 一个字不动；它是**第五个名字而不是
  第五个元组**（`CodeSessionService` 上一个布尔字段 + 一处 append，四个元组字面量不变）；
  旗子是 `policy.search_tools_enabled`，默认 `false`；投影写成"旗子为真**且**
  `[research]` 配了"的与；`code.sandbox_requires_approval` 改名
  `code.external_requires_approval`，`config_schema_version` 1.18 → 1.19。
  **明确不做**：不降级 risk、不给 external 做批量批准、不为 Code 建 external 白名单、
  不给 search 单独的每工具预算、不限制到某一侧、不改控制台导航把人引去 Task
- 日期：2026-08-27
- 影响：新增 `domain/research.py`；`adapters/tools/web_search.py`（名字改从 domain 引，
  并修一处既有偏差：改读 `invocation.cancellation`）；`application/code_session.py`
  （字段、append、两处已变假的性质注释、一条 import 期组合断言）；
  `application/code_prompt.py`（`_NETWORK_CLAIMS` 四锚点、`with_web_search`）；
  `bootstrap/settings.py`（新字段、改名、schema 版本）；`bootstrap/projections.py`；
  `apps/api/dependencies.py`；`config/ownership.yaml`；两个 profile

---

## 1. 头条：变的不是天花板，是"有没有人在场"

这份 ADR 最容易被写错的地方，是把代价记在审批门上。实测（`code_risk_ceiling` 对着
真实 spec 跑，2026-08-27）：

| 元组 | 无 search | 加 search | |
|---|---|---|---|
| `CODE_TOOLS` | `write` | **`external`** | 抬高 |
| `CODE_TOOLS_WITH_SANDBOX` | `external` | `external` | 不变 |
| `CODE_PROJECT_TOOLS` | `write` | **`external`** | 抬高 |
| `CODE_PROJECT_TOOLS_WITH_RUN` | `destructive` | `destructive` | 不变 |

所以"加 search 会抬天花板"既不是普遍真的、也不是普遍假的：**已经握着 external 或更高
工具的两条臂什么都不变；两条素臂从 `write` 抬到 `external`。** 而控制台今天走的正是
`WITH_RUN`（ADR-077 的开关在 `config.demo-local.toml` 打开），那一格不变。

真正的净变化在别处，它必须是头条：

> **今天 Code 要够到网络，必须花掉一次人类审批**——`curl` 走 `project_run`，
> `destructive`，无条件上膛，而且卡片上是规范化后的**真实命令**。
> **加了 `web_search` 之后，够到网络不再需要任何人在场**，而不可信的网页文本落进一个
> 握着 `project_write` / `project_edit`（`write` 风险，永不上膛）的回合。

这不是脚注。这是这次决定买到的东西和付出的东西。

## 2. 不降级 risk

显然的省事做法是把 `web_search` 说成 `read` 或 `write`：一个只把公开网页读进上下文、
不产生外部副作用的调用，看起来和"在用户机器上跑命令"不是一类。**否掉。**

判据不是"有没有副作用"，而是 `domain/sandbox.py` 早就写下的那一条：external 说的是
**内容离开了本进程**。沙箱 `--network=none`、跑完什么都不剩，照样是 external——因为
脚本本身离开了。按这条判据，搜索是更干净的样本：离开的是**用户自己的问题**，而且离开
给了第三方。`adapters/tools/web_search.py` 自己的注释说的是同一件事——一次超时或失败
的搜索同样已经把问题送上了公开网络，所以它**在调用之前**就记账。

"它无副作用、可重放"这条轴另有两个字段在管：`idempotency="safe"` 与
`binding.operation_key is None`。降级等于让 `risk` 重复它们已经说过的话，同时抹掉它
唯一在说的那句。

## 3. 第五个名字，不是第五个元组

`CODE_TOOLS` / `CODE_PROJECT_TOOLS` / `..._WITH_SANDBOX` / `..._WITH_RUN` 四个字面量
**一个字不动**。`web_search` 是 `CodeSessionService` 上的布尔字段，append 发生在选完
`tool_names` 之后、plan 模式收窄之前。

理由不是省事。`sandbox_run` 只给扁平侧、`project_run` 只给项目侧，都是**工具自己的
性质**（一个读 ContextVar，一个要目录）；`web_search` 不进任何 scope，两侧都真——
它是第一根**正交**的轴。写成元组就是 4→8，其中两个带 `_AND_` 的名字正是 ADR-077 刚
删掉的形状。而 `code_session.py` 那条"有两个答案可读，而不是一个答案加一次追加"的
立论，前提是**名字数少于组合数**；在笛卡尔积下它自己失效。

append 放在收窄之前还有一个白拿的好处：plan 回合自动丢掉它（`external` 不是 `read`），
不需要任何新分支。

## 4. 旗子进 `[policy]`

`policy.search_tools_enabled: bool = False`。理由逐字沿用 ADR-077：`policy_fingerprint()`
哈希 `[policy]` 的每一个字段，翻它就改 `policy_identity`，此后每一次 run 都记着自己
跑在哪个答案下。同一段注释说沙箱可以不进指纹——因为它 `--network=none`。本案的事实
正相反：这件工具存在的意义就是出网。

**投影必须是一个与**：`policy.search_tools_enabled and research is not None`。
漏掉它，一个打开旗子却没配 `[research]` 的 profile 会让 `code_risk_ceiling` 在**每一个
回合**抛 `ValueError`——因为名字被 offer 了而 registry 里没有 spec。这不是假想：
`config.demo-local.toml` 里根本没有 `[research]` 段，联网能力来自 `scripts/dev.sh`
导出的环境变量。半配的部署应该得到**没有这件工具的那套安排**，而不是一个起得来、
却兑现不了自己 offer 的进程。这条形状是从同一个文件里 `sandbox_enabled` 那段抄来的。

## 5. `external` 这一档，一个部署只有一个答案

`approval_required_risks` 是按**风险档**取值的，`sandbox_run` 与 `web_search` 同档，
信封内**无法**给它们不同答案。所以 `code.sandbox_requires_approval` 改名
`code.external_requires_approval`，默认仍为 `false`，注释说清它现在管两件工具。
改名会让写着旧名的配置文件停止加载，故 `config_schema_version` 由 `1.18` 抬到 `1.19`。

顺带重写它的默认值论证：原文第一条理由（ADR-054"卡片上只有工具名和参数摘要，
哈希没法被同意"）**已经过期**——ADR-077 之后，`tool_gateway` 无条件把规范化后的真实
参数放进 `PermissionRequested.approval_preview`，对一次搜索，卡片上读到的就是 query
本身，那是**可以被同意的**。默认值现在只靠 Task 那条先例，这一点写明而不是继承旧话。

## 6. 提示词：四个锚点，和一条 import 期断言

`with_host_commands` 与 `with_web_search` 都靠"恰好匹配一条否则 raise"工作。这条纪律
是对的，但它让两个改写器的锚点集变成一对**耦合**，而这份耦合从任何一个文件里都看不见。

看不见的方式还很贵：`with_host_commands` 把整句 no-shell 换成 `_HAS_SHELL`，而
`_HAS_SHELL` 描述网络是**可达的**（ADR-084 那一批补的）——所以它跑完之后，三条
no-shell 拼写一条都不在了，第四个锚点才在。一个锚点集只有前三条的 `with_web_search`
会匹配到 0 条并 raise，**触发条件是 `config.code-local.toml` 的默认组合，也就是每一个
项目态回合 500**：不在 import，不在某条测试里，是生产环境里每回合一次，而模块
类型检查通过。

所以锚点是**四个**，顺序固定为 base → host → search → plan，并且新增
`_assert_every_prompt_combination_resolves()`：4 个元组 × gated/ungated × plan/act 全部
求值一遍，模块 import 时调用。32 次纯函数调用，代价是零，而它是这一对锚点唯一被放在
一起检查的地方。

**刻意不**折进已有的 `_assert_project_tuples_enter_their_own_scope`：那条查的是"元组有
没有把工具泄漏进它不进的 scope"，而 `web_search` 不进任何 scope——塞进去会恒真，
等于没查。

## 7. 明确不做

- **不降级 risk**。见 §2。为了让方案成立而把一个该是 external 的东西说成 write，
  是把"这个部署变宽了"这个可见信号静默删掉。
- **不给 external 做批量批准**。`approve_for_session` 对 external/destructive 的硬拒是
  设计不是遗漏：一次哈希不能被同意，逐次审批买的就是"人看见了那条命令"。
- **不为 Code 建 external 白名单**。那是在 `code_risk_ceiling` 之外造第二处"工具风险
  写在哪"，而它的重写正是为了消灭这种表。
- **不给 search 单独的每工具预算**。`RunBudget` 没有 per-tool 上限；chat 的"最多两次"
  靠的是一个只装一件工具的 registry，Code 搬不了。这是本次接受的代价，记进
  `known-gaps.md`，不在这条 ADR 里发明新机制。
- **不限制到某一侧**。与 `sandbox_run` / `project_run` 不同，`web_search` 没有 scope 上
  的理由；硬限一侧是编一个不存在的约束。
- **不改控制台导航把人引去 Task**。同一天的 `config.demo-local.toml` 已经用打开
  `shell_tools_enabled` 回答过"Code 该不该够得到外面"，把人往别处引与那个决定正面冲突。
