# ADR-035：答案不是摘要，两者不该共用一个上界

- 决策点：一个 run 的答案该有多大；ADR-019 那个 4096 管的是什么；`_EXTERNAL_RESEARCH_CONTRACT`
  的"20 条各 8000 字符"和装它的字段谁该改
- 状态：**接受**，收窄 [ADR-019](./0019-run-step-transparency.md)"有界"那一条的适用范围
- 日期：2026-08-10
- 影响：新增 `AnswerText`（65 536）与 `ANSWER_TEXT_LIMIT`；`AgentOutcome.output_text`、
  `ModelCompleted.text`、`AnswerCommitted.text`、`UngroundedAnswerCommitted.text`、
  `ChatTurnResult.answer` 改用它；运行时 `MAX_OUTPUT_TEXT` 随之抬到同一个数；
  `_EXTERNAL_RESEARCH_CONTRACT` 的每条上限 8000 → **2400**，并改为由常量插值。
  `BoundedText` 的值、事件的形状、`bounded()` 的用途、配置 schema **均不变**
- 依赖：ADR-019（提示词摘要为什么有界）、ADR-030（每次 invocation 的 token 额度）、
  ADR-032、ADR-034

## 1. 背景：两个各自都合理的数，从来没被放在一起看过

`_EXTERNAL_RESEARCH_CONTRACT` 告诉模型：一个来源一条，至多 20 条，每条至多 8000 字符。
那是约 160 KB。

装这个答案的字段是 `AgentOutcome.output_text: BoundedText`，**4096 字符**。而运行时的
`_clip`（`MAX_OUTPUT_TEXT = 4096`）在 `_consume` 里就把模型输出裁掉了——**裁在源头**，
所以下游没有任何地方还留着完整文本。

两个数都不是随手写的：8000 来自"一段值得引用的正文有多长"，4096 来自 ADR-019
"事件同时是数据库行和 SSE 帧"。问题在于**没有任何东西把它们放在一起比较过**，而它们
描述的是同一条消息。

### 真正的后果不在外部研究节点

按 ADR-034 的判断，这里最多是浪费一轮纠正。实测之后发现不是：

```text
model wrote          : 18010 chars
outcome.output_text  : 4096 chars
tail                 : 'ed paragraph. Grounded para… [truncated]'
stored artifact      : 4098 bytes
```

（真实 `ClaudeLikeAgentRuntime` + `ArtifactPersistingExecutor`，fake 模型。）

`ArtifactPersistingExecutor` 存的就是 `output_text`，所以**这个上界就是整张图的产物的
上界**：`synthesize` 写的调研报告、`export_report` 导出的那份文件，一律被切在 4096
字符——约 600 个英文词，句子从中间断开，后面缀一个没有任何下游会读的截断标记。Chat
的答案同样如此：用户看着 `ModelDelta` 把全文流完，然后拿到一份被截短的
`AnswerCommitted`。

**没有任何地方说过报告只有两页。** 这不是一条被记录下来的取舍，是两个数字碰在一起的结果。

## 2. 为什么不是把契约的数字改小就完了

那样能消掉矛盾，代价最小，而且确实要改一部分（见 §3.4）。不这么收工的理由是：**它治的
是我恰好先注意到的那处症状。** 把 8000 改成 3000，报告仍然被切在 4096，chat 答案仍然
被切在 4096，而这两处比外部研究节点严重得多——一个失败的节点会喊出来，一份被截短的报告
不会。

## 3. 决定

### 3.1 答案有自己的类型和自己的上界

新增 `AnswerText`（`ANSWER_TEXT_LIMIT = 65_536`），用于**承载模型答案本身**的字段：
`AgentOutcome.output_text`、`ModelCompleted.text`、`AnswerCommitted.text`、
`UngroundedAnswerCommitted.text`、`ChatTurnResult.answer`。

`BoundedText` 保持 4096，也保持它本来的含义：**关于**一次 run 记录下来的文字——
`prompt_preview`、`argument_preview`（ADR-019 引入的那两个），以及流式增量的一片。

这不是新形状，是这个仓库已经用过一次的形状。`ToolOutputText` 当初就是这样从
`BoundedText` 里分出来的，注释写得很清楚："共用一个类型让那种结果根本无法构造——不是被
截断，是被拒绝"。检索结果是**输入**，自然大小是它要取的证据；答案是**产物**，自然大小
是被要求写出来的东西。两者共用一个数，只是因为一开始都还小。

### 3.2 ADR-019 的"有界"没有被推翻，只是被收窄了

ADR-019 立的是：**提示词与工具参数**要有界，因为它们是数据库行和 SSE 帧。那条继续成立，
一个字没改。

它同一节里还写过一句，当时是作为背景写的：

> 事件流早就在携带模型生成的正文，因为答案本来就要从这里回到提问的人手里。

答案走事件流不是本 ADR 引入的，是这个系统本来的样子。所以问题从来不是"答案该不该进事件"，
而是"进事件的答案该以哪个上界为准"。**摘要按摘要的上界，答案按答案的上界**：一个 preview
每一步一条，一个答案每轮一条，这是大的那个上界付得起的原因。

`AnswerWithheld.text` 特意**留在** `BoundedText`。放进去的永远是本系统自己写的拒绝语，
不是它替换掉的模型输出——类型上说出这件事，比注释说更牢。

### 3.3 仍然裁在源头

`_clip` 留着，只是量尺换成了 `ANSWER_TEXT_LIMIT`。超出上界的答案照旧被裁并留下可见标记。

不改成"存的时候再裁"是有理由的，而且 `schema.py` 里 `bounded()` 的文档已经写下过：在下游
裁剪会**发布一份和供应商返回的不一样的答案**。裁在源头意味着 artifact、事件、会话记录看到
的是同一份文本——`ChatTurnResult` 里那条 `answer == outcome.output_text` 的校验正是靠这个
成立的，而它是发布围栏的一部分。

### 3.4 契约的数字由常量算出来，并且被一条测试比着

`MAX_EXTERNAL_PASSAGE_CHARS = 2400`，`LOCATOR_CHARS = 800`（一条 item 里不是正文的那部分：
URL、标题和 JSON 结构）。`20 × (2400 + 800) = 64 000 ≤ 65 536`。

提示词现在**插值**这两个常量而不是自己抄一份数字，并且有
`test_the_external_contract_asks_for_no_more_than_an_answer_can_carry` 把两边比着：谁把
任何一个数字改回去，这条测试就红。缺的从来不是正确的数，是**没有东西在比较它们**。

`LOCATOR_CHARS` 是宽裕值而不是域的极大值：`EvidenceUrl` 允许 2048 字符，真实来源 URL
没有那么长，按极大值编预算会把整个答案花在定位符上。测试里另有一条控制断言，用满长标题
加一条长 URL 证明这个宽裕值是真的够。

## 4. 后果

- **报告、chat 答案、证据包都能装下了。** 18 010 字符的报告完整落进 artifact；
- **事件行和 SSE 帧变大了。** 一条 `AnswerCommitted` 最大 64 KB，而不是 4 KB。这是有意
  接受的：这一帧就是提问的人要的东西。每一步都产生的 `prompt_preview` / `argument_preview`
  **没有**变大，那才是 ADR-019 担心的量；
- **上界仍然在。** 超过 65 536 的答案照样被裁并标记，`test_an_answer_past_its_own_ceiling_is_still_clipped_and_says_so`
  钉着这一条——搬动了一个数，不是取消了一条约束；
- **兼容方向要说准。** 新代码读旧数据没有问题：字段名和形状没变，`BoundedText` 的值也
  没变，放宽一个上界不会让已经写进库里的行失效。反过来**不成立**——一条 60 KB 的
  `AnswerCommitted` 被旧二进制读到时，envelope 的 `schema_version` 仍是 1，于是它会解析
  成功再在 `max_length` 上失败，而不是干净地报版本不符。`DOMAIN_SCHEMA_VERSION` 特意
  **不抬**：那个校验要求严格相等，抬一版会让**所有**历史 payload 一起读不出来，代价比
  它防的那件事大得多。滚动升级时先升读的一侧，这是任何放宽都有的方向；
- **ADR-034 §5 那条"被截断的答案要两次 run 才失败"仍然成立**，只是触发它需要的答案从
  4096 字符变成了 65 536 字符——即，它不再是日常形状。

## 5. 重审条件

- 如果某个部署把 `max_tokens_per_agent_invocation` 调到远高于默认的 16 000（本机 web
  profile 已经是 120 000），65 536 会重新变成模型写得出、系统装不下的那个数。届时要重新
  按那个部署的 token 额度量一遍，而不是再加一次；
- 如果答案开始有超出"一次调用能写多少"的自然大小（比如一个节点被允许把多轮输出拼成一份
  交付物），那时该走的是 ADR-019 备选方案那条路——事件里只放 ref，正文进 artifact——而不是
  继续抬这个数；
- 如果 SSE 这一层出现帧大小限制，§4 第二条要重新量。
