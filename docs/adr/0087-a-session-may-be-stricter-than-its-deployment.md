# ADR-087：一段会话可以比它的部署更谨慎，不可以更宽松

- 决策点：2026-08-27 的用户反馈，第二条是「没有权限的设置，例如模型自己决定，需要人
  来决定」。控制台此前只有一个「只做计划」复选框（ADR-0079）：它答的是「这一轮能不能
  改」，答不出「改之前要不要问我」。而后者今天由部署配置一次性替所有回合决定，屏幕上
  没有任何地方说得出它是什么。参照 Codex 的 approval policy 与 Claude Code 的模式循环
- 状态：**接受**。回合新增一条权限轴 `CodeApprovals = Literal["standard",
  "before_write"]`，进 `AuthorizationEnvelope.approval_required_risks`，与提示词的
  `with_write_gate` 配套；界面上与 `CodeMode` 合成一个三档选择器
  （只做计划 / 改前问我 / 自动改动）。
  **明确不做**：不把 `CodeMode` 与 `CodeApprovals` 合并成一个四值枚举、
  **不提供「什么都别问我」这一档**、不把选择记进 localStorage、不动
  `config_schema_version`、不关闭 known-gaps F-26
- 日期：2026-08-27
- 影响：`application/code_session.py`（`CodeApprovals`、`code_approval_risks`、
  `_system_prompt_for` 第四轴、`_request_for`）；`application/code_prompt.py`
  （`with_write_gate`）；`apps/api/routes/code.py`（`AskRequest.approvals`）；
  前端 `CodePage`（`CodePermission` / `TURN_OF` / 三档选择器）、`api/{client,types}`
- 关联：[ADR-086](./0086-a-produced-file-is-not-answerable-to-which-store-it-landed-in.md)
  是同一批反馈的另一半，收紧的是信封的另一半

---

## 1. 权限轴：为什么是第二条轴，而不是 `CodeMode` 的第三个值

界面上是一个控件，三档：只做计划 / 改前问我 / 自动改动。Claude Code 的模式循环把
plan、acceptEdits、bypassPermissions 压成一条梯子，读起来确实是一条梯子。

但信封里必须是两半，因为它们收紧的是**不同的两半**：

- `CodeMode` 收紧 `allowed_tools`——plan 回合按 `ToolSpec` 自己的 risk 过滤成只读
  （ADR-079 的 `read_only`）。
- `CodeApprovals` 收紧 `approval_required_risks`——同一批工具，换一个「谁来拍板」。

压成一个四值枚举，就得在唯一读它的地方（`_request_for`）再拆回两半，而拆的时候那个
值本身谁也不叫。所以：**界面合并，信封分开，合并只发生在 `CodePage` 的 `TURN_OF`
一张表里。**

## 2. 明确不提供「什么都别问我」

这一档看起来是梯子上顺理成章的第四格，Codex 有（`--dangerously-bypass-approvals-
and-sandbox`），Claude Code 有（`bypassPermissions`）。**否掉。**

要拿掉的是 `destructive`，而 `destructive` 在 Code 里是 `project_run`——在用户自己的
机器上跑一条命令。ADR-077 的标题就是那句话：*一条在这台机器上的命令，在它跑之前先给
人看见*。让一个下拉框能关掉它，是把一份 ADR 变成一个默认值。

所以这条轴的性质是**只加不减**，而它写成了代码而不是一句话：

```python
base = ("external", "destructive") if external_requires_approval else ("destructive",)
if approvals == "standard":
    return base
return ("write", *base)
```

`base` 是每一条返回路径的子序列，读两行就能确认。这和 ADR-079 的「plan 只会收窄」是
同一种可检查性——那一条靠比对两个列表，这一条靠读一个表达式。

一个想要比部署配置更少提问的人，要去改部署（`code.external_requires_approval`），
不是改这一轮。**会话可以比部署更谨慎，不可以比部署更宽松。**

## 3. 这条轴也回答了 F-26 的一半，但没有关掉它

`docs/known-gaps.md` F-26 记的是：`policy.write_tools_require_approval` 是
`Literal[True] = True`，`config.default.toml` 写着 `true`，而 `rg` 在这两处之外零命
中——**按构造，写工具不停在任何人面前**。它给的完成判据是二选一：接上一道真的
`write` 审批闸，或者像 ADR-059 删 `node_retry_max_attempts` 那样把字段删掉。

它同时提了一个问题：*每一次写都停下来的回合还能不能干完活*。这次的答案是：**那不该
由部署替所有人回答，它是一次一回合的选择**——所以做的是第一项的一个变体，闸接上
了，开关在发送框上而不在配置里。

**但 F-26 不关。** 那个字段仍然没有读者：它叫 `policy.write_tools_require_approval`，
读起来是「这个部署要求写入审批」，而这次加的是「这一轮的人要求写入审批」。把它接成
新控件的**默认档**是能做的，代价是改 `Literal[True]` 为 `bool` 并因此动
`config_schema_version`——F-26 自己说了那次 schema 变更应当与下一次合并，不单独 bump。
本次不做，F-26 的措辞相应收窄：闸不再是不存在的，缺的是那个**字段**的读者。

## 4. 模型要被告知，理由和 ADR-058 是同一条

`with_write_gate` 往系统提示末尾追加一段：每一次写入都会停在人面前，因此**不要少
写，要整块写**。

不告诉它会怎样，`CODER_SYSTEM_PROMPT_WITH_SANDBOX` 的注释里已经量过一次：*模型会为
它被描述成所处的那个世界表现正确*。一个不知道自己被门控的模型，会照常做十二次小
edit——十二个问题，大半在人已经不看着的时候才到。

同样重要的是它**没有**说的那句：「所以少写点」。那正是那条注释记下的错误——把一件工
具定价过高，买到的是沉默不是谨慎，而一个打开这一档的人要的是被问，不是被少做。

`with_write_gate` 是纯追加，没有锚点，因此不进
`_assert_every_prompt_combination_resolves`：那个断言存在的理由是「锚点会漂移」，而
这里没有任何一句话是别的提示可能不再包含的。它与 `with_plan_only` 互斥并在代码里写
成了 `and not plan_only`——一个 plan 回合根本没有写工具，告诉它写入会停在人面前，
正是这整套选择要避免的那种「描述一个它不在的世界」。

## 5. 明确不做

- **不合并 `CodeMode` 与 `CodeApprovals`。** 见 §1。
- **不提供「什么都别问我」。** 见 §2。
- **不把权限档记进 localStorage。** 它是**一轮**的属性，回合起始就冻进信封；一个跨
  会话记住的开关，会让「这一轮到底能不能写」变成读者要去别处查的问题。这与
  ADR-079 给 `CodeMode` 的理由是同一条。
- **不动 `config_schema_version`。** 这次没有新配置字段：权限走请求体，不走配置。
- **不关闭 known-gaps F-26。** 见 §3：闸接上了，但那个**字段**仍然没有读者。
- **不给这一档做「本会话都允许」的默认。** `approve_for_session` 对 `write` 是可用的
  （`UNREPEATABLE_RISKS` 只硬拒 external/destructive），而按参数摘要记的长效批准正是
  为这种反复出现的同一次调用准备的。但把它做成**默认**会让第一次「允许」悄悄变成
  「以后都允许」，而一个刚打开这一档的人要的恰好相反。三个按钮的顺序不动。
