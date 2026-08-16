# ADR-055：回执不是记录

- 决策点：工具**返回**的正文要不要进事件流；如果要，是复用
  `runtime.record_step_inputs`，还是像 ADR-054 那样开一个自带上限的新例外
- 状态：**接受**，把 ADR-019 那条 opt-in 从「步骤的**输入**」扩到「一次调用的
  两端」，而不是对 ADR-054 的无条件例外再开一个
- 日期：2026-08-16
- 影响：`ToolCompleted` 新增 `output_preview: BoundedText = ""`，在
  `ToolGateway._record` 里由 `runtime.record_step_inputs` 门控填写。
  `output_bytes`、`truncated`、`artifact` 三个字段**一字不改**；
  `runtime.record_step_inputs` 的默认值（`false`）、语义与
  `config/ownership.yaml` 的登记**都不动**；配置 schema 保持 `1.14`、零迁移。
  `tests/cli/golden/demo_tool_round.jsonl` 多出一个 `"output_preview": ""`
- 依赖：[ADR-019](./0019-run-step-transparency.md)（步骤输入是 opt-in）、
  [ADR-054](./0054-a-digest-cannot-be-consented-to.md)（摘要没法被同意）

## 1. 背景：能打开，但打开了还是不知道

Code 模式接上 `StepStream` 之后，每一步都能展开了。展开之后看到的是：

```
耗时      41 ms
输出大小  4.1 KB
```

这是一张回执，不是一份记录。它能证明有一次调用发生过、花了多久、回来多少字节，
说不出这次调用**回答了什么** —— grep 有没有命中、读回来的文件是不是下一步要改的
那一个、写入是成功还是被工具自己拒了。

工作区那五个工具让这件事更彻底：它们的 `ToolResult.artifact` 恒为 `None`
（`adapters/tools/workspace.py`），所以对 Code 而言，`output_bytes` 不是「除了
artifact 之外还有的东西」，而是**唯一的东西**。「读取工作区 · 4.1 KB」这一行，
读者能从中推断的全部内容是「有个文件被读过」。

对称的另一半早就在了：`ToolProposed.argument_preview` 记着这次调用**被要求**做
什么。一个记录了问题却不记录答案的事件流，把读者留在半句话上。

## 2. 为什么不复用 ADR-054 的无条件例外

ADR-054 给 `PermissionRequested.approval_preview` 开了一个**不受开关控制**的例外，
理由是那一处的读者正被要求**同意**：摘要没法被同意，所以参数正文必须在场，否则
那个批准按钮问的是一个人看不懂的问题。

这里不成立。`ToolCompleted` 到达时，没有人正在被要求做决定 —— 调用已经发生完了，
这条记录是给事后阅读的。事后阅读的价值很高，但它不是「不给就没法回答」，所以它
拿不到 ADR-054 那张豁免票。

## 3. 决策：跟输入用同一个开关，而不是同一个理由

`output_preview` 由 `runtime.record_step_inputs` 门控，和
`argument_preview`、`prompt_preview` 完全一致。

这个开关的名字里写着 `inputs`，而这里记的是 output，看起来是错配。不是的 ——
这个开关回答的问题从来不是「输入还是输出」，而是**「这个部署愿不愿意让运行时正文
落进事件日志」**。一次工具调用的两端，在这个问题下是同一件事：

> 一个不记录工具**被问了什么**的部署，没有同意记录它**答了什么**。

反过来更能说明问题。假设给 output 单开一个 `record_tool_outputs`：一个把
`record_step_inputs` 关掉的部署，会以为自己关掉了「正文落库」，然后在默认打开的
另一个键上把读回来的文件整份写进事件表。两个开关意味着两次判断，而这里只有一个
判断需要做。

`config/config.production.toml` 根本不设 `record_step_inputs` 这个键，所以生产
部署拿到的仍然是空串，和本 ADR 之前逐字相同。

## 4. 边界

- **上限沿用 `BoundedText`**（`BOUNDED_TEXT_LIMIT`），不像 ADR-054 那样自带一个。
  那一处需要独立上限是因为它不受开关约束，必须自己兜住；这一处已经在开关后面。
- **`output_bytes` 与 `truncated` 保留，且含义不变。** 三个字段回答三个问题：
  工具**返回了**多少（`output_bytes`）、**工具自己**截没截（`truncated`）、
  **这条记录留下了**多少（`output_preview` 及其省略号）。把它们折成一个，就会
  出现「4.1 KB 但只有 2 KB 正文」这种无法解释的行数。
- **`bounded()` 而不是原样。** 与 `argument_preview` 同一个函数，同一个理由：
  超限不该让事件构造不出来，而截断必须看得见。

## 5. 什么没有变

- `domain/events.py` 模块 docstring 那条「事件只描述、不复现」仍然是规则。本条和
  ADR-019、ADR-054 一样，是一处**写明范围的例外**，不是推翻。
- `ToolFailed` 不动。失败已经带 `error.message`，那是这条路径上的正文。
- Chat、Work、Task 的行为不变：三条流程用的是同一个 `ToolGateway`，同一个开关，
  所以打开这个开关的部署在三处都会多出「工具返回」，关着的三处都不会。

## 6. 做完的判据

一次真实回合里，一个工作区工具的步骤展开后能读到它返回的正文；同一份代码在
`record_step_inputs = false` 下重跑，该字段为空串且事件流其余部分逐字不变。

**已验证**（2026-08-16，本地 demo profile，真实 provider）：一次
「新建 fib.py 并读回」的编码回合，两条 `ToolCompleted` 的「工具返回」分别是
`Wrote 247 characters to fib.py.` 与读回的完整源码；`demo_tool_round.jsonl`
（CLI demo 未开该标志）的差异只有 `"output_preview": ""` 一处。
