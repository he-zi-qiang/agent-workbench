# ADR-0114：搜索只负责找到，不负责度量——握不到执行工具的回合要被明说这一点，委派是例外不是默认

- 状态：Accepted
- 日期：2026-09-12
- 关联：ADR-0058（提示词必须描述这个回合**真正所在**的世界——本 ADR 是它的又一个实例：
  一个没有 shell 的回合，从来没有人告诉它「搜索不是尺子」）、ADR-0089（Code 可以委派——
  本 ADR 给那把工具补上「什么时候不该用」）、ADR-0112（`AGENTS.md` 是项目记忆——本 ADR 记的
  事故正是一条**正确的**记忆把一个没有仪器的回合送进循环）、ADR-0113（编码会话有了浏览器——
  原生档有，Compose 档没有，见 F-39）、ADR-0030（步数是兜底不是预算——所以撞到步数上限的
  回合在读者眼里是「模型不行」，而不是「预算到了」）、ADR-0081（压缩——本 ADR 的提醒为什么
  不写进系统提示的原因之一）

## 1. 背景

用户的原话：「code 模式中还是这种问题，重复进行搜索，可能当初设计就有问题？请你参考本地
Claude desktop 的技术实现进行修改」。附的转录是 81 个步骤，其中六十来个写着「搜索项目目录」，
末尾一句「这一轮把步数用完了」。

事件流里的那一段（Compose 栈，`config.compose-local.toml`，项目 `windows测试`，会话
`ses_33521d79…`）：

| 运行 | 是谁 | 步数 / 工具调用 | `project_grep` | 终态 |
|---|---|---:|---:|---|
| `run_37ee…` | 第一轮，父 | 60 / 100 | **74** | `max_steps` |
| `run_2624…` | 它委派的 `explorer` | 14 / 36 | 31 | completed |
| `run_9fb4…` | 第二轮，父 | 48 / 108 | **98** | 模型调用超 120 s 信封 |
| `run_6970…` | 它委派的 `explorer` | 45 / 119 | **114** | completed |
| `run_9022…` | 它委派的 `analyst` | 1 / 0 | 0 | completed |
| `run_f3c2…` | 它委派的 `explorer` | 30 / 109 | 106 | 重复熔断（3 次拒绝） |

模型在每一步说的话（`ModelCompleted.text`）把它在干什么讲得很清楚：

> A `mario.html` already exists (41 KB). Let me read it before touching anything.
> … let me verify the invariants AGENTS.md warns about — especially the 160-character rows
> … I can't run Node here, so a generator script I can't execute is useless.
> … Now I must verify every row is exactly 160 characters. Let me check with anchored regexes.
> … I'm burning calls on guesswork. Let me measure the trailing dot run directly …
> … The middle run is at most 96. Let me narrow it. / … at most 89. / 79. / 69. / 59. / 49. /
> 40–49. / 45–49. / 47–49. / The middle run is 49.
> … Row 183 is neither 160 nor 159/161 — so it must contain a character outside `[.XB?PoF]`.

三件事同时成立，缺一件都不会是这个结果：

1. **`AGENTS.md` 里有一条正确的记忆。** 上一段会话（ADR-0112）留下的：「关卡 `LEVEL_ROWS`
   是 15 行字符串，**每行必须正好 160 个字符** … 改完务必核对每行长度」。模型认真照办了。
2. **这个回合手里没有任何能计算的东西。** Compose 档不给 `project_run`（F-37）、不给沙箱
   （F-24）、也不给浏览器（F-39）。「I can't run Node here」是实话。
3. **唯一像仪器的工具是 `project_grep`，而它不是仪器。** 模型用正则量词当尺子：`^.{160}$`、
   `X{44}\.{3}X{1,200}`、然后对一段 `X` 的长度做二分，每步两次搜索，做了三十步。**量出来的
   还是错的**——源码那一行是 `   "…160 个字符…",`，三个空格、一对引号、一个逗号，锚定计数
   永远差五；于是它把本来就对的行「修」了一遍又一遍。事后用 `awk '{print length}'` 看，
   十五行全是 160，从头到尾。

`runtime/agent_runtime.py` 里那个「同一问题问四次就拒」的熔断（`MAX_IDENTICAL_CALLS`）
一次都没响：七十四次搜索没有两次是同一个问题。响的是步数上限，而 ADR-0030 早就写过步数
上限响起来是什么样：「the tools all work, it just never finishes」，读者看到的是模型不行。

委派把这件事乘了四。父回合把「审计这个文件」交给 `explorer`，孩子只有读工具，做了同样的
二分，31 次；下一轮交出去三个，119 / 0 / 109 次。`derive_child_budget` 把 `max_steps` 与
`max_tool_calls` **原样**传给孩子（ADR-0030 的理由，没错），所以一个 60 步的回合能变成四百
次工具调用的会话。`delegate_agent` 的工具描述写了怎么委派，一个字没写什么时候**不**委派。

## 2. 决定

**Claude Code 面对同一件事的四样东西，搬三样过来；第四样（一把 shell）记成缺口。**

1. **基础提示词多一段，每个变体都继承**（`application/code_prompt.py`）：
   「A search finds; it does not measure.」——搜索回答*在哪里*，匹配的是整行源码（缩进、引号、
   逗号都在），所以它给不出一个字面量多长、一样东西有几个、一个表达式对不对。需要计算的核对
   只有两种做法：手里有能跑代码的工具就跑；没有就把那几行**读**出来、推一次、在报告里写明
   「是读出来的不是跑出来的」。**它从来不是一个可以用再一次搜索去收窄的问题。** 段尾两句是
   Claude Code 自己的操作规则原样搬来：够了就动手，不要重推已经拿到的结论；改动返回成功
   就不要再读一遍去确认——没成功工具会拒绝。
2. **握着 `delegate_agent` 的回合被告知「默认不用」**（`with_delegation`）：子代理冷启动、
   读过的它一样没读过、只有读工具、**你算不了的它也算不了**、还花一份跟本回合一样大的预算。
   用户要求时用；一个可分、只靠读就能答、且结果会把本对话塞满而你只要结论的问题时用。
   「一件事有几个部分」不是委派的理由。这一段只在回合**真的握着**这把工具时出现——从
   `tool_names` 读，不从部署开关读，因为 ADR-0096 的选择可以把它勾掉。
3. **`explorer` 自己的提示词也说这句**（`application/sub_agents.py`）：它只有读工具，更需要。
4. **运行时的一句提醒，不是拒绝，不是上限**（`runtime/agent_runtime.py`）：一个运行里某一把
   工具每被问到第 25 次，那一次的结果末尾多一句「这是本运行第 N 次调用 X；如果最近几次是在
   收窄同一个问题，这把工具答不了它：把已知的写下来动手，或者报告什么没能确定」。
   每个 25 的倍数再说一次。写在**消息**里而不是事件里；只加在成功的结果上。

顺带一件配置修正，不是决策：`config.compose-local.toml` 补上 `[model.main] timeout_seconds = 240`
与 `[runtime] model_timeout_seconds = 300`——上表第二轮的死法正是 demo-local 在 2026-08-27
写过注释的那一对平局，这个 profile 当时没有跟。

## 3. 为什么是这些做法而不是别的

### 3.1 为什么不是直接给它一把 shell

Claude Code 不需要这条 ADR 的原因只有一个：它永远有 Bash。数一行多长是
`awk '{print length}'`，一次调用。这是**正确答案**，而它在这里有三条路，三条都不是本 ADR 能走的：

- 宿主 `project_run`：F-37 已判「拒绝」——Compose 里没有宿主。
- 容器内 `project_run`：命令会跑在 API 容器里、`AW_*` 之外的环境原样继承（`bootstrap/
  child_environment.py` 的设计），而 API 容器挂着 key 卷。一条 `cat` 就能把 provider key 读进
  转录。这是能力变更，要自己的 ADR 与自己的隔离论证，不该由一次「模型循环了」的修补顺手打开。
- 沙箱 / 浏览器：F-24、F-39，各有各的未接线理由。

所以第四样东西记成 **F-40**，而这三段提示词加一句提醒，是「没有仪器的回合应该怎么办」的
诚实版本：**说清楚它没有，让它读完就推一次，然后停。**

### 3.2 为什么是提醒不是拒绝，为什么是 25

现有熔断拒绝的是「同一个问题问第四次」，门槛放在「重读一次是正常的」之上。这个循环的每一次
都是新问题，所以任何按签名的拒绝都看不见它；而按工具名拒绝会拒掉正当的工作——大树上一次
宽泛的探索就是三十次 grep。拒绝错一次的代价是一个回合，提醒错一次的代价是一句话。

25 是量出来的。当天本地事件流里所有**跑完的**运行，最忙的那把工具中位数 2 次、90 分位 11 次；
循环的那几个是 31、42、74、98、106、114。25 落在健康尾巴之后、病态花掉一半预算之前。每个
倍数再说一次，是因为第一次可能被无视——上面那个模型自己说了三次「I'm burning calls」还是
继续了。

### 3.3 为什么写进结果消息，而不是系统提示或事件

系统提示是缓存前缀：上表第一轮 172 万输入 token 里 169 万是缓存命中。中途改系统提示等于
把整段对话的缓存作废一次，代价与它要省的是同一种钱。事件那一侧，`ToolCompleted.output_bytes`
描述的是工具答了什么，操作员读日志要的正是这个；模型在此之上多读的一句是运行时的行为，
记在运行时——测试 `test_the_nudge_is_written_to_the_message_and_not_to_the_event` 钉住
两者的差正好是那一句。这也是 Claude Code 的形状：提醒挂在工具结果上，以 harness 自己的
口吻。

只加在成功的结果上，是一个具体的坑：`ToolResultBlock.from_tool_result` **只在 `content` 为空时**
才把 `error` 渲染成文字，往一个拒绝的空 `content` 上追加提醒会把拒绝本身盖掉。计数照样前进，
下一个倍数落在哪一次就是哪一次。

### 3.4 为什么委派那一段是「默认不用」，而不是关掉 `delegation_enabled`

开关是部署的（ADR-0089），它决定这把工具**在不在**目录里；本 ADR 决定的是握着它的回合怎么
**衡量**它。Claude Code 的规则原文是「Do not spawn agents unless the user asks. Each spawn
starts cold and re-derives context you already have — it's the expensive path」，它从来不是
「把 Agent 工具删掉」。多加的一句是这里特有的：孩子算不了父亲算不了的东西——这是委派在
这个事故里救不了场的全部原因。

### 3.5 为什么不改 `project_grep` 的输出（比如把行长附在匹配上）

那是给一次事故量身定做。`file:line:content` 是 ripgrep 的约定，模型的训练里有它；行长只是
这一次要的数，下一次要的是 JSON 能不能 parse、一个表达式等于几。通用的答案是能算，见 3.1。
搜索工具该做的是把「它匹配的是整行」这一事实留在提示词里让模型知道，而不是长出一个字段。

### 3.6 为什么不动那条 `AGENTS.md`

它是用户的（ADR-0112 §3）；而且它是**对的**——每行确实必须 160，改完确实该核对。错的不是
记忆，是拿来核对的仪器。一条 ADR 如果得出「记忆太较真」的结论，下一次它会删掉一条真正
救命的注意事项。

## 4. 代价，与没做的

- **提醒会落在正当的第 25 次上。** 一句话。测试 `test_a_run_below_the_threshold_is_not_nudged`
  把地板钉在 24。
- **提示词是概率的。** 没有任何东西检查模型照做——ADR-0058 那句「Prose that asked for
  behaviour nothing checks would be a promise this system cannot keep」在这里的答案是：检查的
  那一半就是第 25 次的提醒。
- **没有给 Compose 档的编码回合任何能算的东西。** 记为 F-40，做完的判据写在那里。
- **转录里前两步显示的是裸的 `ToolCompleted`**（应为「查看项目目录」「读取项目目录」）。
  事件流里这两步的 `ToolProposed` 都在，是控制台那一侧的分组没拿到它们——本批只观察到，
  没有诊断，记在 status.md 里。

## 5. 证据

- `tests/application/test_code_session.py`：`test_every_coding_prompt_says_a_search_does_not_measure`
  （六个变体）、`test_a_turn_holding_delegate_agent_is_told_the_default_is_not_to`（含计划档
  的段落顺序）、`test_delegation_guidance_follows_what_the_turn_holds_not_the_deployment`
  （走真实的请求构造，两个方向）。
- `tests/application/test_delegation.py`：`test_the_explorer_is_told_a_search_does_not_measure`。
- `tests/runtime/test_agent_runtime.py`：`test_the_twenty_fifth_call_of_one_tool_carries_a_nudge`、
  `test_a_run_below_the_threshold_is_not_nudged`、`test_two_tools_do_not_add_up_to_one_nudge`、
  `test_the_nudge_is_written_to_the_message_and_not_to_the_event`。
- 导入期：`_assert_every_prompt_combination_resolves` 现在枚举 64 个组合（多了委派这一轴）。
- 配置：`agent-config-check --config config/config.compose-local.toml` 通过。
