# ADR-079：计划不是授权

- 决策点：Code 的每一个回合从第一次工具调用起就武装着写入用户的真实仓库。
  `AskRequest` 只有 `instruction` 一个字段，所有工具元组都含 `*_write`／`*_edit`，
  而 `write` 风险**不在**任何 `approval_required_risks` 里——按构造，写入不停在人
  面前。要不要有一种「先说你要做什么」的回合；如果要，它靠什么执行，以及那份计划
  与随后那一轮之间是什么关系
- 状态：**接受**。`CodeRequest`／`AskRequest` 新增 `mode: "act" | "plan"`，
  在回合起始冻结。`"plan"` 把被提供的工具按**各自 `ToolSpec` 的 risk** 收窄到只读，
  信封的 `max_tool_risk` 随之落到 `read`，提示词说明这一轮改不了任何东西。
  **明确不做**：计划不授权后续回合，不做审批，不落库，不约束随后那一轮
- 日期：2026-08-25
- 影响：`application/code_session.py` 新增 `CodeMode`、`read_only()`，
  `code_risk_ceiling()` 改为读 specs 并新增 `tools: ToolRegistry` 字段（在
  `__post_init__` 里强制），`_system_prompt_for()` 新增 `plan_only`；
  `application/code_prompt.py` 新增 `with_plan_only()`；
  `apps/api/routes/code.py::AskRequest` 新增 `mode`；
  `apps/api/dependencies.py` 新增 `_LiveToolRegistry`；
  `web/` 的 `askCode` 新增 `mode` 参数、`CodePage` 新增开关与「按这个计划执行」。
  **不动配置契约**：`config_schema_version` 保持 `1.18`

---

## 1. 先纠正一句话：只读的一半早就存在，只是不按回合、也对模型不可见

不要说「Code 面上没有任何只读开关」。执行侧的一半已经在那里，是 **scope 闸**：
`project_write`／`project_edit` 带 `permission_scopes=("workspace:write",)`，
`EnvelopePolicyEngine.decide` 会以 `missing_permission_scope` 拒绝，身份来自
`x-principal-scopes` 头，控制台还有一个可编辑的 scope 输入框。

它不是 plan mode，理由要写下来：

- 它是**全局的**，不是按回合的。持久在 localStorage，改一次影响此后每一轮。
- 它与 Work 页 v2 的 `work` 节点**共用**同一个 scope，去掉就断了 Task 导出。
- 它**对模型不可见**。模型照旧被提供写工具，直到中途某次调用被拒才发现——而这正是
  `CODER_SYSTEM_PROMPT_WITH_SANDBOX` 那条注释记下的失败：模型为一个它不在的世界
  正确地行动，然后报告一件没发生的事。

缺的是一个**按回合的、写进提示词的、工具清单层面的** plan mode。

## 2. 决策：一个不能写的回合是另一种回合，不是同一种回合换个说法

三件事必须一起动，否则模型又会为一个它不在的世界正确地行动。

### 2.1 工具清单按 risk 收窄，不按名字后缀

`read_only(tool_names, risks=...)` 只留 `ToolSpec.risk == "read"` 的名字。

**不按 `*_write`／`*_edit` 后缀过滤**，这一条是本节的全部重量。后缀过滤是"一个工具
的风险写在第二个地方"，而且那个地方带通配符：第一个不守命名约定的工具——将来的
`project_move`、任何从 MCP 绑进来的东西——都会溜进一个已经在提示词里、也在信封里被
告知"改不了任何东西"的回合。`code_risk_ceiling` 的旧 docstring 自己否掉过这种表。

顺序保留，所以一个 plan 回合的工具清单是 act 回合那份的**子序列**——这让"plan 只会
收窄、永远不会加"变成读两份清单就能看出来的性质，而不是一件要信任过滤器的事。

### 2.2 信封的天花板跟着落下来，而且没人为它写分支

`code_risk_ceiling` 从"命名 `project_run` 与 `sandbox_run` 的 if 链"改成"读每个被
提供工具自己的 `ToolSpec`，取最大值"。旧写法对问题的判断是对的（不要第二张风险表），
但它**自己就是那张表**，只有两行。读 specs 是零行的版本，也正是让 `read` 这个第四个
取值自然落出来、不需要任何人加第三个分支的原因。

被提供却没有 spec 的名字**抛异常**而不是取默认值。两个可选的默认值都以一种下游不会
报告的方式出错：`read` 会造出一个否定本回合被授予的工具的信封，`destructive` 会悄悄
抬高每一个含拼写错误的回合的天花板。

这要求 `CodeSessionService` 持有 `ToolRegistry`，且**在 `__post_init__` 里强制**。
第一版把它做成可选、缺失时回退到旧的 `write`——`test_a_turn_holding_the_run_tool_
is_not_told_there_is_no_shell` 当场抓住：一个被提供 `project_run` 而天花板是 `write`
的回合，是一个信封否定自己所授工具的回合，结局是 `outside_submitted_envelope`。

API 侧的 registry 是**每次调用重建**的（`sandbox_run` 由 `startup` 事后填进
`SandboxSlot`），所以服务拿到的是 `_LiveToolRegistry`——一个每次都经工厂解析的两行
包装。在装配期拍一张快照，会在恰恰授予了沙箱的那些部署上少掉 `sandbox_run` 的 spec，
于是天花板推导拒绝一个被提供了它的回合。

### 2.3 提示词必须说这一轮改不了任何东西

`with_plan_only()` 用与 `with_host_commands` 相同的具名 `_rewrite` 组合上去，锚点
恰好命中一次否则 import 时抛错。它改两处、加一段：

- **纪律 2**（"优先用 edit 而不是 write"）在一个两者都没有的回合里，是关于一个模型
  没有的选择的建议。改成"计划里要说清改的是哪一部分"。
- **纪律 6** 的中段（"what you changed and why… Name the files you touched"）在一个
  什么都改不了的回合里，是在要一份它做不到的报告。改成"你会做什么、每个文件里改什么"。
- 结尾一段说清：这一轮只有读的工具，提出写只会花掉一次调用换一次拒绝；以及**读它的
  人决定它跑不跑**。

`plan_only` 是唯一一个**不**从 `tool_names` 读出来的事实，这个不对称是刻意的：一份
被收窄过的清单和一份本来就没有写工具的清单，从外观上无法分辨，而只有前者应该被告知
"写这件事被拿走了"。

### 2.4 模式在回合起始冻结

和 ADR-073 §5.2 冻结文件语言在同一处、同一个理由：一个中途能变的模式，会在信封已经
用另一份清单签过之后，改变一个正在跑的模型手里握着的东西。

## 3. 计划不授权任何东西

「按这个计划执行」开启的是**新的一轮、新的信封**，和没有先计划过时完全一样。

重发的是**同一条指令**，不是计划正文。这一点要说明白，因为反过来做很自然：把模型写
的计划当成下一轮的输入，读起来像"照着办"。但那会让一段模型自己写的散文出现在一个能
写用户仓库的回合的输入里，而它是本轮唯一没有经过人的东西。

更根本的一条：**一个能授权 act 回合的计划就是审批**，而本仓把审批留给 `destructive`
且要求把命令原样展示给人看（ADR-077 不变量 2）。一份几百字的计划不满足那个形状——人
批准的是他们读到的那段散文，跑的是模型随后自己决定的一串调用。

所以也要记下它**没买到**什么：没有 git，没有 diff，计划是散文，随后那一轮**不受它
约束**。它买到的是一次「先看看你打算干什么」的机会，成本是一轮。

## 4. 被拒绝的方案

**按名字后缀收窄。** 见 §2.1。

**让 plan mode 也能写、只是写完要批。** 拒绝。那是给 `write` 风险装审批闸，与
`policy.write_tools_require_approval` 是同一件事（见 §6），且它把 plan mode 从"另一种
回合"变成"同一种回合加一道闸"——而一个被拒绝了三次写的回合，花掉的是它的调用预算。

**把计划落库、让后一轮引用它。** 拒绝。落库的计划要有 id、有生命周期、有"这份计划
还作数吗"的答案；而它唯一的用途是被人读一眼然后决定要不要跑。这些成本换不到东西。

**把 `mode` 做成 `plan: boolean`。** 拒绝。`plan=false` 在请求体里读起来像一个缺席，
`"act"` 读起来像有人做了选择——而要回答"这一轮是哪种模式"的时候，手上往往只有那一条
请求日志。前端也因此**总是**发这个字段，包括 `"act"`。

## 5. 不变量

1. **plan 只会收窄，永远不会加。** 一个 plan 回合的工具清单是同一个 act 回合那份的
   子序列，且其中每一个的 `ToolSpec.risk` 都是 `read`。
2. **模式在回合起始冻结**，与 ADR-073 §5 不变量 2 挨着。
3. **计划不携带任何授权。** 「按这个计划执行」是新的一轮、新的信封，与没有先计划过
   时的那一轮逐字相同。
4. **天花板由被提供工具的 specs 派生**，绝不在旁边配置，也绝不从名字猜。被提供却没
   有 spec 的名字抛异常。
5. **提示词与清单一致。** 一个被收窄的回合被告知它被收窄了；一个没被收窄的回合不会
   被告知。

## 6. 一件顺手了结不掉的事：`policy.write_tools_require_approval`

`settings.py` 里它是 `Literal[True]`，`config.default.toml` 里写着 `true`，
**`src/` 里没有任何读者**。它读起来像"写工具要人批准"，而写工具按构造不停在任何人
面前。

本 ADR **不接它**，理由要写清楚：plan mode 不是"写入停在人面前"，它是"这一轮没有写
工具"。把这个字段接到 plan mode 上，会让一个名字承诺一件它不做的事——正是它现在的
毛病，只是换了个位置。它的两条出路（接上一道真的 `write` 审批闸，或者像 ADR-059 那样
删掉）都要自己的决定，删掉还要一次 schema 变更。记为 `docs/known-gaps.md` 的一条
**口径不实**，不排期。

## 7. 怎么验证

- `tests/application/test_code_session.py::test_a_plan_turn_is_narrowed_to_reading_and_told_so`
  ——三件事一起动：清单是子序列且只剩 `read`、天花板落到 `read`、提示词说了这一轮
  改不了东西且不再推荐它没有的工具、也不再要一份它做不到的报告。
- `tests/application/test_code_session.py::test_a_plan_does_not_authorise_the_turn_that_follows`
  ——同一个会话里 plan 之后 act，后者的信封与没有先计划过时相同。
- `tests/adapters/test_project_tools.py::TestExclusivity`——`read_only` 只收窄且保序、
  天花板的第四个取值、没有 spec 的名字抛异常、每个被提供工具都在派生出的天花板之内
  （现在包含被收窄的那份清单）。
- `tests/application/test_code_session.py::test_a_half_wired_coding_session_is_refused_at_assembly`
  ——没有 registry 的装配在构造时被拒。
- `web/src/features/code/CodePage.test.tsx`（4 条）——开关真的改变发出去的那一轮而不是
  只改自己的样子、默认 `"act"` 也明写进请求、计划跑完给出按钮且按下去是新的一轮
  `act` 并重发同一条指令、一轮 act 之后不再提议。

能力梯子停在 **Implemented + Tested**。Code 的提示词从未对真实模型跑过，所以"模型在
plan 回合里真的只做计划"是**未经证实的**——被证实的是它手里没有写工具，且信封会拒绝
它提出的任何写。
