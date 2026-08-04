# ADR-019：把提示词与工具参数记进事件流，而不是记进 telemetry

- 决策点：运行步骤的可观察内容；`runtime.record_step_inputs` 的引入
- 状态：**接受**
- 日期：2026-08-03
- 影响：config schema `1.3` → `1.4`；`ModelStarted`、`ToolProposed` 新增可选字段

## 背景

试用控制台时提出的问题：Chat 和 Work 的步骤能不能像 Codex / Claude 那样点开，看到
真实的命令、代码和"模型思考的提示词"。

前两样其实已经在事件里，只是没被渲染：`ModelCompleted.text` 携带模型的完整输出，
`ToolCompleted.artifact` 指向工具产物，`PermissionResolved.reason_code` 说明为什么
被拒。控制台把它们一律塞进一个 `<pre>{JSON.stringify(payload)}</pre>`——**信息在，
可读性不在**。这部分是纯前端问题，不需要 ADR。

需要 ADR 的是第三样。**提示词和工具参数不在任何地方。**
`ToolProposed` 只有 `argument_bytes` 和 `argument_sha256`，`ModelStarted` 只有
model id。`tool_executions` 表的注释写得很直白：

> The digest of the canonical arguments, never the arguments: this table is read
> by operators, and tool arguments carry user text and retrieved passages.

## 问题

看起来这条已经被 `observability.record_prompt_body: Literal[False] = False` 决定过
了——那是一个单值 `Literal`，按 ADR-018 立的规矩，它编码的是**已冻结的不变量**，
改它要先写 ADR。

但把这条直接套过来是错的，因为它管的不是同一个去处。

| | `observability.record_prompt_body` | 本 ADR |
|---|---|---|
| 写到哪 | OTel span → 第三方 collector | `events` 表 → 该 Task/Session 的事件流 |
| 谁能读 | 运维、任何拿到 collector 的人 | 只有通过鉴权、且拥有这条 Task/Session 的 principal |
| 租户边界 | 无（span 属性是扁平的） | 有（事件流按 tenant + owner 授权） |
| 现有内容 | 只有标量：耗时、token 数、状态 | 已经有 `ModelCompleted.text`，即模型全文 |

最后一行是关键。**事件流早就在携带模型生成的正文**，因为答案本来就要从这里回到提
问的人手里。所以"提示词不能进事件流"并不是一条已经成立的不变量——它从来没有被决
定过，只是没人写过。

真正要回答的是：**把送进模型的输入也记下来，会不会削弱这个系统的任何一条承诺。**

## 决策

**引入 `runtime.record_step_inputs`，默认 `false`，只写事件流，不碰 telemetry。**

开启后：

- `ModelStarted.prompt_preview` 记录 system prompt 与消息序列的有界摘要
- `ToolProposed.argument_preview` 记录规范化参数的有界摘要

`observability.record_prompt_body` **保持 `Literal[False]`**。它守的是导出到外部
collector 这条路，本 ADR 不动它，`tests/adapters/test_telemetry.py` 里那条断言继续
成立。

### 为什么默认关

因为它改变的是"这个部署把什么写进了数据库"，而这件事不该由一次升级替运维决定。
一个已经在跑的部署升级之后，事件表不应该突然开始存用户问题的全文和检索到的原文。
想看的人自己打开——校招演示、本地调试、给人讲"它到底怎么想的"，都是显式选择。

### 为什么是有界的

和 `schema.py` 里那句一样的理由：事件同时是数据库行和 SSE 帧。无界的提示词就是无
界的行、无界的帧。上界取 `BoundedText`（4096），超出的部分截断并标记——截断的提示
词仍然说明了模型看到的开头是什么，而拒绝记录只会让这个功能在长上下文时静默消失。

### 为什么不复用 `tool_executions`

那张表是运维视角的账本，它的"只存摘要不存参数"是对的，本 ADR 不改它。参数进的是
事件流，鉴权口径和 `ModelCompleted.text` 完全一致。

## 后果

- 打开后，`events` 表的体积随提示词增长；这是开启者自己选的，文档里写明。
- 控制台把 `prompt_preview` / `argument_preview` 当作**可选**内容渲染：字段不在就
  不显示这一块，不显示"未记录"之类的占位，也不假设每个部署都开了。
- 关闭时不记录任何内容，但事件负载**不是**逐字节不变：两个字段以空串出现在
  序列化结果里。写这份 ADR 时以为它们会被省略，`tests/cli/golden/demo_tool_round.jsonl`
  当场证伪了——golden 文件的作用就是这个。空串每条事件多 22 字节，接受；换成省略要
  给事件序列化加 `exclude_defaults`，那会影响所有事件类型，代价比这大。

## 备选方案

**把提示词写成 artifact，事件里只放 ref。** 更接近 `ToolCompleted.artifact` 的现有
形状，也天然不占事件行。没有选它，是因为每个模型调用都要多一次 artifact 写入和一次
额外的授权读取，而提示词的自然大小恰好在 `BoundedText` 之内——为一个默认关闭的调试
视图付一次存储往返，代价和收益不成比例。如果以后要记录完整而非有界的提示词，这条路
是对的。

**永远记录，不给开关。** 被否决：见"为什么默认关"。
