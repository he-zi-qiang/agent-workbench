# ADR-034：读不出来的时候再问一次，而不是去消息里找那个对象

- 决策点：结构化节点收到"答案外面裹了一句话"时该怎么办；ADR-032 §3.3 的严格到底严在
  哪一件事上；这条严格该由解码器守还是由多跑一轮守
- 状态：**接受**，收窄并兑现 [ADR-032](./0032-the-external-researcher-is-an-agent.md) §3.3
- 日期：2026-08-10
- 影响：`plan`、`critic`、`review`、`research_external` 四个结构化节点在**消息不是一个 JSON 对象**
  时多跑一次纠正轮次；新增 `StructuredOutputFramingError`；`_outcome_update` 改成能合并
  多个 run。解码器接受的东西、图的形状、`admits`、授权信封、`CANONICAL_V1_NODE_IDS`
  **均不变**
- 依赖：ADR-030（每次 invocation 一份预算与墙钟）、ADR-032

## 1. 背景：正确答案被当成故障

2026-08-10 的本机验收（`config.web-local.toml`、真实 DeepSeek、本机 PostgreSQL 5433 +
Qdrant 6333），三个不同 objective 提交三次（`task_ccdfad3c…`、`task_8d124a55…`、
`task_89b408b2…`），全部死在同一处：

```text
StructuredOutputError: structured output must be one JSON object
TaskNodeRunFailedError: task node research_external did not produce valid output
```

第三次的 `ModelCompleted.text` 是决定性的：

```text
The tool calls were denied due to permission scope. The objective explicitly states
this is explanatory writing based on existing knowledge and does not require external
sources. I'll return an empty items list.

{"items":[]}
```

模型给出的**正是 ADR-032 §3.3 明文允许的那个答案**，只是前面多了一句说明。
`decode_external_evidence_output` 要求整条消息是且只是一个 JSON 对象，于是这个答案被
整条丢掉、节点失败、Task 死掉。

后果的形状值得说准：**只要这一轮研究工具什么都没读到，Task 就必死**，而在 web profile
上"什么都没读到"是每一次（本机 fake-IP 代理下地址闸门按设计拒绝所有外网，见
`docs/status.md` 2026-08-10 事实一）。

这与 ADR-032 §3.3 立的规矩不是同一件事。那条规矩防的是**把"读不出"降级成"没读到"**，
因为下一个节点会在沉默上写出一份有模有样的报告。而这里没有沉默：模型写了
`{"items":[]}`，那是它的答案。

## 2. 为什么不是"把消息里的那个对象抠出来"

最省事的写法是在解码前找一遍消息里的 `{...}`。本 ADR 明确拒绝它，理由不是它不好写，
而是**它改的是"什么算模型的答案"**。

一条消息里出现一个 JSON 对象，可能是在回答，也可能是在举例、在引用、在解释自己**没有**
返回什么：

```text
I could not reach any page. Had I read the quarterly report I would have answered
{"items":[{"url":"https://example.test/q3","title":"Q3 report","text":"Revenue rose
four percent."}]}, but I did not read it.
```

"这是我的答案"和"这是答案会长什么样"之间的差别是语义的，不是语法的——没有哪种取法能
把两者分开。抠出来就等于**由解析器代替模型做出断言**，而"模型自己说出这个包"正是
ADR-032 §3.2 那条"只记录工具真的返回给你的内容"唯一的落脚点：解码器从来没有、也无法
核对 URL 是否真的被取过。所以抠字符串不是放松一点点严格，是把那条性质的地基抽掉。

这条口子有测试钉着：`test_a_json_object_the_model_only_described_never_becomes_evidence`。
把 "find the trailing `{...}`" 写进解码器，它会红——节点会静悄悄地成功，并把一个模型
自称没读过的页面变成可引用的证据。

## 3. 决定

### 3.1 解码器一个字不放松，改的是"再问一次"

节点接受的仍然**只有**"整条消息是且只是一个 JSON 对象"。变化只有一处：读不出来的时候
不立刻判死，而是**再问一轮**——把读不出来的那条消息按它本来的身份（模型自己的一轮）放
回去，然后要求"把那个 JSON 对象单独再发一次"。

第二轮仍然读不出来，节点照样失败。所以 ADR-032 §3.3 那条不对称一个字没动：**没人读得懂
的输出永远不会变成"没读到"**，因为 `{"items":[]}` 只可能由模型自己说出口。

这样做还有一层：纠正轮次里模型能看见的新东西，只有"你上一条消息读不出来"。它没有拿到
任何新材料，所以它能做的最多是把已经在记录上的答案重说一遍。

### 3.2 只有"不是一个 JSON 对象"能换来这一轮

`StructuredOutputFramingError` 把解码失败分成两类，分界线是**这条消息是不是一个 JSON
对象**，而不是**这个对象对不对**：

- **可纠正**：前后有旁白、包了代码围栏、对象后面还跟着一句话、整条压根不是 JSON。
  这些是**送信的方式**出了问题，消息里那个答案对不对还没被问到；
- **不可纠正**：这条消息**是**一个 JSON 对象，但不是这个节点要的值——critic 评的是另一份
  draft，evidence item 的 url 是 "page 3" 这种不能定位的东西。这些是模型**做出了一个断言
  并且错了**。

后一类不给纠正轮次，理由和 §2 是同一个：那已经不是在问"你刚才说了什么"，而是在把模型往
"能通过校验的答案"上推。对 `research_external` 尤其如此——一条没有 locator 的 evidence
被"纠正"成有 locator 的，多出来的那个 locator 只能是编的。

`_require_completed` 仍然排在解码之前，所以撞上 token 上限、停在半句 JSON 上的 run（
ADR-032 §4 记的那种）依旧当场失败，不会为一个从来没写完的答案买第二次整轮。

### 3.3 纠正轮次是一次独立的 run，并且不带工具

它自己 `resolve` 一次 invocation：有自己的 `agent_run_id`、自己的事件流分组、自己的
预算与墙钟（ADR-030），并且在花第二笔钱之前**重新验证这个 Worker 的 claim**。两次 run
的用量与 run id 都进入节点的状态增量——`budget_usage` 是加法通道，只报最后一次的节点会
交给图一张少算了前面的账单。

**纠正轮次一律不带工具**，而且这一条在共用的运行处执行，不由各个调用点自己记得。省钱是
次要的：一轮够不到任何东西的对话，就不可能带回第一轮没产出过的材料。带着工具再问一次，
等于给模型第二次机会去读、去被拒、再解释一遍——那正是第一轮失败的成因。

写在一处是必要的而不是整洁：`research_external` 的工具来自动态目录，v2 `review` 的三个
只读 workspace 工具**来自它自己的 profile**。两处来源不同，一个"每个调用点记得把工具摘掉"
的约定会在第三个节点上失效，而失效的样子是一个能重新翻工作区、并因此给出与被复述那条
不同的裁决的 reviewer。

### 3.4 修在四个节点共用的那一处

`plan`、`critic`、v2 的 `review`、`research_external` 用的是同一个严格解码器形状，因此
**暴露面完全相同**：只有 `research_external` 复现过，是因为只有它带着工具跑、也只有它会
因为工具被拒而先解释一句。换一个爱把推理写在前面的模型，planner 一样会死在这里。

`review` 值得单独点名：它**持有工具**（三个只读 workspace 工具），而"刚用完工具的模型
倾向于先说一句自己做了什么"正是本 ADR §1 那条消息的成因。按这条推理，它是继
`research_external` 之后第二可能撞上的节点。

所以纠正轮次做在四个节点共用的运行处，而不是补在外部研究节点上。

## 4. 为什么不是用 tool call 强制结构化输出

让模型通过调用一个 `submit_*` 工具来交结构化结果，保证比本 ADR 强——形状由传输层保证，
不由提示词保证。不采用是因为在这套代码里它的代价不在同一个量级：

- 这样一个工具必须出现在 Task **提交时就冻结**的授权信封里，否则
  `permitted_tools` 的交集会把它去掉。而那个交集是"子 agent 不可能比它所属的 Task 权限
  更大"唯一的实现处，为一件与外部效果无关的事在那里开一个旁路，是拿一条硬边界换一处便利；
- 运行时的循环以"这一轮没有工具调用"为终止条件，"调用这个工具即结束 run"是一套新的循环
  语义，而不是加一个工具；
- 答案要作为工具参数走一遍 schema 校验。ADR-032 的契约允许 20 条各 8000 字符，这条路径
  在实际部署的模型上是最不可靠的一条。

留作重审条件（§6），不是否掉。

## 5. 后果

- **多花的钱是有条件的。** 第一条消息就是一个 JSON 对象时，一分钱不多花，事件流一个字
  不变；只有需要纠正时才多一次模型调用，且**最多一次**；
- **一个节点可能出现两个 `agent_outcome_refs`。** 这是有意的：两次 run 都真的发生了，
  都花了钱，都该看得见；
- **纠正轮次拿的是一份新的墙钟额度。** 和 ADR-030 "墙钟是一次尝试的"同一个理由：它是
  一次独立的 invocation；
- **它可能在两次 run 之后才失败。** 4096 字符的答案截断（运行时的 `MAX_OUTPUT_TEXT`）
  会让消息停在半句 JSON 上，纠正轮次也救不回来，于是这类失败从一次 run 变成两次。
  这类答案在今天同样是失败的，本 ADR 只是让它多付一次；
- **`research_external` 的空答案终于能走完。** 这是 web profile 上唯一常见的形状。

## 6. 重审条件

- 如果某个部署的模型即使被明确要求也稳定地在 JSON 前后写字，"一次纠正"就不够了，届时
  要回到 §4 的 tool call 方案，而不是把次数加到两次、三次；
- 如果运行时获得了"这次调用必须用这个 schema 回答"的一等支持（供应商的 structured
  output，或本地的 submit 工具循环语义），§4 的理由失效，三个结构化节点应该一起改过去；
- 如果 `MAX_OUTPUT_TEXT` 与 ADR-032 契约里"20 条各 8000 字符"的矛盾被处理，
  §5 那条"两次 run 之后才失败"要重新量一遍。
