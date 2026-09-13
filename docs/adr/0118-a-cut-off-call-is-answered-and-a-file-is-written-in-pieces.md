# ADR-0118：被输出上限截断的调用作答给模型而不是终止运行；文件可以分段写

- 状态：Accepted
- 日期：2026-09-13
- 关联：ADR-0116（重复的调用按记录作答——本 ADR 在同一条流水线上再加一种「作答而不是终止」）、
  ADR-0114（提示词里「分步构建」那一段——本 ADR 把它改成模型做得到的一句）、`docs/known-gaps.md` B-07
  （截断与坏 JSON 曾共用一句话——本 ADR 把截断那一半从错误变成一次调用）

## 1. 背景

第八十五批合入之后，用户新开一段会话说「请你编写马里奥」，三轮都没有写出文件，然后说
「连一个基本的马里奥都编写不了，请你优化」。三轮的事件记录（`run_616…9b0b`、`run_6f1…6b96`、
`run_ea5…5025`，deepseek-chat，「改前问我」）逐条看：

| 轮 | 步 / 调用 | 结局 | 卡在哪 |
|---|---|---|---|
| 1「请你编写马里奥」 | 7 / 9 | `RunFailed` | 读完文件、跑了四条 `python3` 验证关卡几何（四张卡），然后一次 `project_write` 装整个 `mario.html`：`output_tokens: 8192`，供应商 `finish_reason: length`，JSON 断在中间 |
| 2「下一步」 | 22 / 25 | `RunCompleted`（报告说没写） | 从头重读所有文件（338k 输入 token），试图用 `python3 -c` 把 `snake.py` 劈成两半好「分段读」，读回来误判为没劈开，同一条命令连发三次——ADR-0116 按记录作答、收走工具，模型如实报告 |
| 3「开始编写」 | 3 / 5 | `RunFailed` | 重读文件，又一次 `project_write` 装整个文件，8192 处截断 |

三件事叠在一起：

1. **截断是给操作员看的错误，不是给模型看的答案。** `adapters/models/deepseek.py` 在 `length` 且参数
   解析不了时返回 `ErrorInfo(provider_error, "…ask for it in smaller pieces")`，运行时据此 `RunFailed`。
   那句「分小块要」写给了控制台前的人；模型——唯一能把东西发小一点的那一方——从没读到它，而且
   `RunFailed` 的一轮不留报告，下一轮从零开始，连自己上一轮算好的关卡布局都没有。
2. **模型没有分段写的工具。** 提示词（ADR-0114）说「先写骨架，再用编辑把各段放进去」；模型在第二轮
   用自己的话复述了这句（"skeleton first, then sections by edit"），第三轮还是整个文件一次发——因为
   `project_edit` 要求命名一段唯一的 `find`，「文件末尾」不是它不重读文件就能命名的东西。
3. **这台部署的上限就是 8192。** `config.compose-local.toml` 用 `deepseek-chat`，它的输出上限是 8K；
   一个 42 KB 的页面约 12–15k token，在这个模型上**永远**装不进一次调用。这不是偶发，是常态。

### 1.1 对照 Claude Code

| | Claude Code | 本仓库（本 ADR 之前） | 本 ADR |
|---|---|---|---|
| 模型输出上限 | 32k–128k，整份文件一次 `Write` 通常装得下 | 8192（deepseek-chat），一个中等页面就装不下 | 不变——所以分段是常态 |
| 上限截断了一次工具调用 | 截断的 `tool_use` 块被丢掉，模型被告知 `max_tokens`，循环继续，模型自己改小 | `RunFailed`，模型看不到，下一轮从零来 | 调用带 `cut_off` 交给运行时，运行时以一条拒绝作答，循环继续 |
| 分段写文件 | 没有 append；`Edit` 靠唯一锚点，或 Bash `>>` | 只有 `project_edit`（要锚点） | `project_write` 多一个 `append` |
| 跨回合上下文 | 整份 transcript 留在上下文里直到压缩 | 只带用户消息和最终报告（F-44） | 不动，登记 |

## 2. 决定

### 2.1 截断的调用是一次带标记的调用，运行时用拒绝作答

- `ToolCall` 多一个字段 `cut_off: bool = False`。DeepSeek 适配器在 `finish_reason == "length"` 且某个
  调用的参数解析不了时，不再返回错误，而是交出 `ToolCall(id, name, arguments={}, cut_off=True)`，流以
  `max_tokens` 结束、`error=None`。id 和名字在流的最前面，先于参数到达，所以截断的调用总有这两样；
  半截参数**丢弃**，不猜。
- 运行时（`agent_runtime.py`）：`finish == "max_tokens"` 只在**没有** `cut_off` 调用时才是「答案被截断」
  的失败；有的话进入批处理。批处理里 `cut_off` 的调用在计数与按记录作答**之前**被
  `ToolGateway.refuse` 作答（`invalid_tool_input`，`retryable=False`），因为记录按 (工具, 参数) 键入，
  截断的调用参数为空，两次就会被当成「同一个问题」而按记录作答并附上「别再重复」——那是另一件事。
  那句拒绝写给模型：调用被上限截断、什么都没跑、什么都没写、再发一遍装不下、分几次发、大调用前少推理。
  它不点名任何工具的参数：运行时没有文件的词汇，接受分段的那个工具在自己的描述里说。
- `_RunLedger.cut_offs` 计数，`MAX_CUT_OFFS = 2`：第三次截断结束运行（`token_budget` / `budget_exceeded`），
  理由是两条各说了「分段发」的拒绝都没被读。
- 只对**调用**如此。纯文本回答被截断仍然是失败——「截断的答案不能看起来完整地交给调用方」这条不动。

### 2.2 `project_write` 多一个 `append`

- `append: true` 时把内容加到文件末尾；文件不存在就是普通的创建，所以每一段都可以用同一种写法发。
- 不按读取回执把关，理由与 `project_edit` 相同：这个工具自己先读一遍，写入用 `if_unchanged` 围住那次
  读。回执决定的是事后的**覆盖范围**，而且是**沿用**不是**签发**：这一轮自己创建、之后只追加过的文件，
  模型看过它的每一个字节，之后整个替换仍然允许；从没读过、追加了一次的文件，之后整个替换仍然被拒
  （「你只看过它的一部分」）。二进制文件拒绝追加。
- 工具描述把话说全：一次发整个文件会在输出上限处被截断、然后什么都没写；用 `append` 分段、每段几百行。

### 2.3 提示词：说模型做得到的那一句

`CODER_SYSTEM_PROMPT_PROJECT` 多一层 `_rewrite`：「分步构建」那段改成——被截断的调用会以拒绝的形式
回来并说明原因，再发一遍装不下；第一段用 `project_write`，之后每一段用 `project_write` 加 `append`，
每次几百行，在能叫出名字的接缝处分（一个函数的末尾、一个 `<script>` 块的末尾）。扁平工作区保留原句
（`workspace_write` 没有追加，提示词不能点名一个回合手里没有的东西）；「只做计划」的回合换成不点名
工具的版本（计划要写出会分成哪几段）。

## 3. 为什么是这些做法

- **为什么作答给模型而不是自动续写或自动切分**：续写需要把半截 JSON 原样送回并让模型接着写，
  DeepSeek 的 tool call 参数不支持续传；运行时替模型切文件则是运行时在猜文件的结构。Claude Code 的
  做法就是让模型知道并自己改小；这里唯一的区别是这台部署的上限让「改小」成为常态，所以给它一个
  能改小的工具。
- **为什么是 `append` 而不是让 `project_edit` 支持「末尾」**：追加本来就是一种写，风险、scope、回执
  语义都跟 `project_write` 一样；给编辑加一种没有 `find` 的模式是第二套语义。
- **为什么是 2**：与 `MAX_REPLAYS` 同一个理由——第一次是模型得知装不下，第二次是再被告知一次，
  第三次一样的尝试说明这一轮不会再变。
- **为什么不把 8192 这个数写进提示词**：上限属于模型档位（`max_output_tokens`），提示词不读配置；
  「几百行一段」在 8K 和 32K 上都成立，而数字写进静态文本就是「没人检查的承诺」。

## 4. 代价与已知缺口

- **跨回合只带用户消息与最终报告（F-44）**。第二轮重读全部文件花了 338k 输入 token，第三轮又读了一次；
  `RunFailed` 的一轮连报告都不留。本 ADR 让截断在**同一轮内**被处理，所以模型不必跨回合记住失败；
  但把工具结果带过回合（Claude Code 的做法）是另一条 ADR。
- 参数恰好在合法 JSON 边界被截断的调用会被当成完整的调用派发（比如 `content` 被截成一个合法的短
  字符串）。没见过，登记不修：判断「合法但不完整」需要理解参数的语义。
- 扁平工作区（`workspace_write`）没有追加。原生路径上 `config.code-local.toml` 是 32768 的上限，那里
  没有这个常态。
- B-07 记录的「供应商自己的 finish 词没进事件流」：截断那一半现在不再是一句错误，`ModelCompleted.
  finish_reason == "max_tokens"` 是它留下的记录；`_completed_tool_calls` 不再接收 `reported`。

## 5. 证据

- `tests/contracts/test_deepseek_model.py`：截断的调用作为带 `cut_off` 的 `ModelToolCallProposed` 交出、
  流以 `max_tokens` 结束、参数为空；坏 JSON 在 `tool_calls` 结束下仍是 `provider_error`（对照组不动）。
- `tests/runtime/test_agent_runtime.py`：截断的调用被作答、什么也没跑、下一次请求的工具结果里有那句话、
  运行完成；第三次截断结束运行，前两次各一条 `ToolFailed`。
- `tests/adapters/test_project_tools.py` `TestWritingInPieces` 四条：分段落地且每段各自报告、自己分段
  建的文件之后可以整个替换、没读过的文件追加后整个替换仍被拒、二进制拒绝。
- `tests/application/test_code_session.py`：项目提示词点名 `project_write` 与 `append`，扁平提示词不；
  「只做计划」的提示词不点名 `project_write`（原有用例）。
- `tests/domain/golden/domain_v1.json` `ToolCall` 多一行 `"cut_off": false`。
- 门禁与实测见 `docs/status.md` 第八十六批。
