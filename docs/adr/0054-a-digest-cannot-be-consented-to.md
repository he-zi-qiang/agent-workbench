# ADR-054：摘要没法被同意

- 决策点：一个停在审批上的工具调用，参数正文要不要进事件流；如果要，靠
  `runtime.record_step_inputs` 那个开关，还是别的什么；判据错了会怎样
- 状态：**接受**，对 `domain/events.py` 模块 docstring 里"事件只描述、不复现"
  那条规则开一个**有范围的例外**（不是推翻它）
- 日期：2026-08-15
- 影响：`PermissionRequested` 新增 `approval_preview`（自带上限
  `APPROVAL_PREVIEW_LIMIT = 2048`，不复用 `BoundedText`），在
  `ToolGateway._await_approval` 里**当且仅当真的有人要被问**时无条件填。
  `ToolProposed.argument_preview` 与 `ModelStarted.prompt_preview` 的门控
  **一个字不改**；`runtime.record_step_inputs` 的默认值、语义、
  `config/ownership.yaml` 的登记**都不动**；配置 schema 保持 `1.14`、零迁移
- 依赖：[ADR-019](./0019-run-step-transparency.md)（步骤输入是 opt-in：本条明写
  自己**不是**它的一个新开关）、
  [ADR-042](./0042-blocking-belongs-to-the-adapter.md)（阻塞属于适配器：本条的等待
  是事件循环里的 await，不走 call runner）

## 1. 背景：审批卡上有工具名，没有参数

`domain/events.py` 的模块 docstring 写着：

> Event payloads describe rather than reproduce. Tool arguments appear as a size
> and a digest, not as their content

这条规则是对的，而且它有牙：`ToolProposed.argument_preview` 与
`ModelStarted.prompt_preview` 是仅有的两处例外，两处都被
`runtime.record_step_inputs` 门控，默认 `false`，`config/config.production.toml`
根本不设这个键。

于是在 C2 之前，一次需要人批的调用在生产里留下的记录是：工具名（`ToolProposed`
是 durable 且工具名**不受门控**）、scopes、risk、参数字节数、参数 sha256。

**看不到的恰好是参数正文。** 而对 `sandbox_run` 这类工具来说，参数不是这次请求
的一个细节，参数**就是**这次请求。让人去批一个只看得到 `sandbox_run` 和一串
sha256 的调用，等于让人去批一个他没读过的东西。

摘要可以用来**核对**，不能用来**同意**。

## 2. 为什么不是"Code 装配期强制 record_step_inputs=True"

这是最省事的做法，也是错的，理由不在开关本身而在它的**作用域**：

`record_step_inputs` 同时门控 `ModelStarted.prompt_preview`。打开它，跟着参数一起
进 `run_events` 的还有**整段 prompt**——在检索形态里，那里面是被召回的文档正文。
为了让人能读一行 shell 命令，把整批检索到的文档写进事件表，这个交换比明显不成立。

ADR-019 那个开关管的是**留给以后看的记录**：默认关，因为它改变的是这个部署长期
存下什么。本条要解决的是**此刻要问的一个问题**。两者不是同一件事，所以不该共用
同一个开关——共用的代价是，想要后者的部署被迫接受前者的全部作用域。

## 3. 判据：有人可问就写，没人可问就不写

`approval_preview` 的填写条件不是一个配置项，是一个事实：

```python
if self._approvals is None:
    await sink.emit(PermissionRequested(...))     # 不带 preview
    return await self.refuse(call, ...)           # 今天的行为，一字不改
...
await sink.emit(PermissionRequested(..., approval_preview=canonical[:LIMIT]))
```

一个没有审批闸门的部署会拒掉这些调用，它把参数记下来**什么也换不到**——没有人会
读，因为没有人被问。所以这条例外的范围恰好是"存在一个要看它的人"，而不是"某个
部署愿意多存点东西"。

这也是它的对照测试：同一条事件流里，`ToolProposed.argument_preview` 必须仍然是
空串。写 preview 不能变成一条从后门打开 ADR-019 的路。

## 4. 为什么自带上限而不是复用 `BoundedText`

`BoundedText` 是 4096，用在被门控的字段上。这个字段**不受门控**，所以它的上限是
"一个无条件字段最多能往流里放多少"这件事**唯一**的约束。既然唯一，就该自己写明，
并且写得比 4096 小：它的读者是正在决定要不要放行的人，不是事后重建一次 run 的
运维。定成 2048。

超长按字符截断（`max_length` 对 `str` 计的是字符，不是字节），而不是拒绝——被截断的
预览仍然让人看得到"这是什么命令"，而一个因为参数太长就不发的 `PermissionRequested`
会让审批卡整个消失。

**但截断必须留痕**（末尾补 `...[truncated]`）。这不是修饰：一段被无声剪掉尾巴的
命令，读起来跟一段完整的命令**一模一样**，而尾巴恰好是重定向、第二个路径、
`--force` 待的地方。让人对一份他看不出是否完整的文本点"同意"，是本条要解决的那个
问题的缩小版。短到装得下的参数**不加标记**，否则每一份预览都像被截断过，标记就不
再表示任何东西。

参数的**身份**照旧由 `ToolProposed.argument_sha256` 承担，它是对完整参数取的，
不受截断影响；真实长度由 `argument_bytes` 承担。

## 5. 明确没有改的

- `runtime.record_step_inputs` 的默认值、语义与 ownership 登记。
- `ToolProposed.argument_preview` / `ModelStarted.prompt_preview` 的门控。
- `observability.record_prompt_body`（仍然钉死 false，管的是 OTel 导出，不是这条流）。
- 配置 schema 版本：本条不新增任何配置字段，`1.14` 不动。

## 6. 作废条件

如果将来审批卡改成"从别处按 `tool_call_id` 拉取参数正文"——一个只有被问的那个
principal 能读、且不进 `run_events` 的接口——那么这个字段就该删掉，例外随之作废。
本条不预先设计那个接口：它需要一个能按调用寻址、且带自己那套授权的读端，而在
只有一个进程能回答审批的形态下（见 `ports/approval_gate.py`），它买到的东西是零。
