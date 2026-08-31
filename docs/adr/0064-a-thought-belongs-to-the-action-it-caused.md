# ADR-064：一段思考属于它促成的那次动作

- 决策点：Code 的转录把每次模型调用的推理摆在哪里；留痕的上限与裁切方向；
  以及推理要不要回灌进工具循环
- 状态：**接受**。摘录是**每次模型调用**的推理，并在同一条事件上带着它决定的
  `tool_call_ids`，所以它在时间线上的位置就是那次调用的位置——转录按事件顺序
  把每段思考渲染在它促成的动作**正上方**，实时与回放共用同一条渲染路径
- 日期：2026-08-17
- 影响：`web/src/features/code/`（`turnBlocks.ts` 的 `reasonings` 换成
  `steps`、`CodeTurn.tsx` 删掉两个抽屉换成时间线、`useCodeStream.ts` 暴露
  `thinkingCallId`）、`domain/schema.py`（新增 `ThinkingText` 与
  `bounded_thinking()`）、`domain/events.py`（`thinking_preview` 换上限）、
  `runtime/agent_runtime.py`、`ports/model.py`、`apps/cli/rendering.py`。
  **不抬 `DOMAIN_SCHEMA_VERSION`**；无迁移；落库 JSONB 形状不变但值可变长
- 关系：**取代 [ADR-063](./0063-a-produced-name-is-a-fact-not-a-sentence.md) §5**；
  **细化 [ADR-061](./0061-thinking-is-process-not-product.md) §2**（上限与裁切
  方向）；**更正 ADR-061 §4 的前提**（决定保留，理由更换）。ADR-061 的 §3
  围栏、§5 两轴正交、§6 拒绝 display 轴**完全不动**

## 1. 背景：顺序信息一直都在，是浏览器把它扔了

读者打开一个跑完的 Code 会话，看到的是：指令、一个写着「做了什么 ▸」的折叠块、
产出卡、报告、再一个写着「想过什么 ▸」的折叠块。**不点两次，屏幕上一条命令、
一句推理都没有。** 点开「想过什么」，得到的是一列彼此脱节的段落——它们回答不了
推理唯一存在的理由：*它为什么跑了那条命令*。

成因不是后端缺数据。实测一次真实会话的事件流（27 条，`sequence` 单调有序）：

```
ModelStarted → ModelCompleted(思考, 提出调用) → ToolProposed → ToolCompleted → ModelStarted → ...
```

这和 Claude Code 会话 transcript 里 `thinking → tool_use → tool_use → thinking`
的形状是同一个。更关键的是，合并**已经做好了**：`stepGroups.ts` 早就把「只调
工具、不出正文」的模型轮归档到它命名的第一个工具调用名下，并且是**前置**合并。
携带 `thinking_preview` 的那条 `ModelCompleted`，在已发布的代码里就已经在正确的
组、正确的位置上。

`turnBlocks.ts` 却在紧邻的一个表达式里另建了一份扁平的 `reasonings`——两份列表
来自同一个有序数组，之后再没有任何东西把它们接回去。交错不需要「重建」，它已经
建好了，只是没人去读。

## 2. 决定：一步一行，思考在动作正上方

一轮的 DOM 顺序变成：指令 → **步骤时间线** → 产出卡 → 报告 → 原始事件（折叠）。
**没有任何抽屉挡在读者与「它做了什么、为什么」之间。**

一步的形状：

```
▸ 先建一个空的 clock.html，再把时钟逻辑写进去。      ← 思考，淡、斜体、12px
  写入工作区   clock.html                            ← 它促成的动作
```

- **折叠的单位是一段推理的正文，不是一轮的全部推理。** 折叠行是这段推理的第一
  句，它已经回答了「为什么跑这条命令」；要读完整段点这一段，不影响其它步骤。
- **短到没有正文的思考根本不是 `<details>`**，是不可点的 `<p>`——一个点开是空的
  三角，比没有三角更糟。
- **没思考的步骤只有动作行**，不留占位、不写「（无推理）」。

样式取自 Codex 的 `dim().italic()`：那是本次调研的四份材料里，**唯一从源码验证
过**的渲染列。Claude Code 一侧的证据是它自己的会话 transcript（本机 `~/.claude/
projects/*.jsonl`，一条记录一个 content block，形状计数 381 `tool_use` / 171
`thinking` / 109 `text`，无一条混排），那是持久化形状而非渲染，只用来确认交错
顺序，不用来推断像素。

### 实时 → 定稿是**原地晋升**，不是抹掉再追加

`useCodeStream` 在 `ModelCompleted` 到达时清空实时文本——**这一行没有改**，它和
两条既有测试一起钉住「同一次调用不会被渲染两次」。改的是另一半：那段文字消失，
是因为**同一个位置上出现了它的定稿行**。

React key 用 `step.modelCallId`，不用 `step.key`：同一次调用活着时是
`model:mc_2`、定稿后并进 `tool:call_x`，用组 key 会在那一帧重挂载，把读者正在读
的折叠**摔上**。这与既有的 `actionsOpen` 事故是同一类。

### 被否掉的方案：把实时思考累加起来

三份调研材料把「替换而非累加」称作 accident。**它不是**——它是被三处注释与三条
测试钉住的决定。改成累加会当场破坏互斥不变量，回到最初那个 bug：一段推理同时
出现在页面顶端、步骤里、以及原始 JSON 里，一次四调用的轮次上出现五份拷贝。

## 3. 留痕的上限：改的不是那个数字，是裁切方向

真正的缺陷不是 4096 太小，是 `bounded()` **从头部保留**。推理的形状是「我看到了
什么，所以我要做什么」，从前面切等于稳定地扔掉结论——而结论正是读者回头看一个
意料之外的工具调用时唯一想要的东西。

- 单开 `THINKING_TEXT_LIMIT = 16_384`，**不抬 `BOUNDED_TEXT_LIMIT`**：后者被
  `prompt_preview`、`ModelDelta.text`、`argument_preview`、`output_preview` 共用，
  而 ADR-063 §1 的整段论证正建立在 `argument_preview` 是 4096 之上。先例是同文件
  的 `ANSWER_TEXT_LIMIT`。
- 尺寸取自实测：本仓两个开思考的 profile 都是 `reasoning_effort = "low"`，一次
  调用实测 1503 字符（未触顶）；同一提问在 `high` 下 5067。16K 给 high 档约三倍
  余量，又只有答案上限的四分之一——它一次模型调用一行，而答案一次运行一行。
- `bounded_thinking()` 保头也保尾，头四分之三给交代、尾四分之一给结论，中段用
  `…… 中段省略 ……` 命名。**触顶时读者会看见这个标记**，这是被记录的行为而不是
  bug。

### 版本与兼容（沿用 [ADR-035](./0035-an-answer-is-not-a-preview.md) §4 与 ADR-063 §6）

- **不抬 `DOMAIN_SCHEMA_VERSION`**：`reject_unsupported_schema_version` 要求严格
  相等，抬它会让每条历史 payload 立刻读不出来。
- 新代码读旧行：安全（旧值都 ≤ 4096）。
- **旧代码读新行：会被隔离**——`DomainModel` 是 `extra="forbid"`，读回时重新
  校验，超过 4096 的值会让那一行进 quarantine（有计数，不静默丢）。
  **滚动升级先升读的一侧**：`agent-api` 先于写入侧。

## 4. 刻意不做：把推理回灌进工具循环

**记录里两边都写错过，这一节是来更正的。**

`ports/model.py` 的 docstring 写着「providers require it withheld from the next
request」，ADR-061 §4 的前提是 DeepSeek 要求不得回传。**两句都是假的。**
2026-08-17 对 `api.deepseek.com` 实测（`deepseek-v4-flash`，思考开启，声明工具，
脚本见证据节），第二轮：

| 请求形态 | 结果 |
|---|---|
| assistant 消息**不带** `reasoning_content` | **HTTP 200** |
| **带** | **HTTP 200** |
| 带一份**被截断过**的 | **HTTP 200**，无任何校验或提示 |

所以两个方向都被接受，**不回传是本仓的选择，不是谁的要求**。§4 的决定保留，
理由更换为三条：

1. **紧迫性是假的**——不回传不会 400，没有潜伏 bug。
2. **收益未经测量**——DeepSeek 没有服务端裁剪契约，回灌的每个 token 都算我们的
   input token，一路计进 `BudgetUsage`；Codex 为此专门有计量，我们没有。
3. **代价最高**——新增一种 `ContentBlock` 变体会改变落库的 `messages.payload`
   形状，未升级的读侧遇 `kind:"thinking"` 直接隔离。

**第三条实测还带出一个更硬的约束**：回传被截断的推理会被**静默接受**。所以若
将来要灌，只能**逐字或者不灌**——把 `bounded_thinking()` 的产物灌回去，等于给
模型一份被我们篡改过的、它自己推理的记录，而没有任何东西会告诉我们。

**将来若要做，唯一可接受的形状**：照 Codex 的表示分裂——面向模型的那份全文逐字、
只活在这次运行的 ledger 里、永不渲染、永不落 durable 事件；面向人的那份是
`thinking_preview`，受发布围栏管辖、永不上行。一个字段做不了两件事。
**门槛**：`evals/` 的一次 A/B（同一批指令，回灌开/关，比工具调用次数与成功率），
因为实测证明 API 不会告诉我们它有没有用。

## 5. 其它刻意不做

- **不实现 `signature` / `encrypted_content` / `redacted_thinking`。** Anthropic
  的签名与 OpenAI 的加密推理项都是那两家 wire 上的原语，DeepSeek 的 Chat
  Completions 没有对应物。含有它们的方案不必读完即可驳回。
- **不加显示/详略旋钮**（Codex 的 `hide_agent_reasoning` /
  `show_raw_agent_reasoning`）。ADR-061 §6 明确否决过第三个旋钮，理由至今成立。
  **与参考实现对齐本身不是推翻记录的理由。**
- **不改 Work / Task 一侧。** Worker 是独立进程，没有实时通道（ADR-051），摘录
  是它唯一能显示的思考——那是架构事实而非选择。`StepStream` 与 `summariseGroups`
  的既有用法不动。
- **不动 Chat**：五个 `run_kind="chat"` 构造器仍一律钉 `thinking=False`。
- **不给消息加 `run_id`**：ADR-063 已定价并拒绝的尾对齐配对问题，本次不碰。

## 6. 取代 ADR-063 §5

该节写「Code 的思考摘录只出现在按轮次的折叠块里，不再进步骤树」，理由是「摘录是
模型对**整轮**的推理」。**这个前提对我们自己的事件是错的**：`agent_runtime` 每个
`model_call_id` 发一条，并在同一条事件上带 `tool_call_ids`。§5 的顾虑（把跨步骤
的文字挂到一个它不从属的节点上）描述的是一份我们没有的数据。ADR-063 的其余部分
不受影响，其 §6 的版本不对称规则被本 ADR 沿用。

## 7. 证据

- 事件顺序可重建：`web/src/components/stepGroups.test.ts` 新增四条，钉住前置
  合并、多调用归第一个、不可达调用保留自己的行、答话轮自成一步——`groupSteps`
  此前**零测试**，而整个设计压在它身上。
- 交错渲染：`turnBlocks.test.ts::puts each model call's thought on the step it
  caused`、`::keeps the answering turn's thought where the report follows it`、
  `::leaves an anchor for the call that has not come back`、
  `::never files one model call as two steps`。
- 不双渲染（主护栏，保留并改写）：
  `CodePage.test.tsx::shows the reasoning of one model call exactly once`。
- 零点击可读（本次投诉的直接回归）：
  `CodePage.test.tsx::shows what a finished turn did without a click`。
- 折叠而非截断：`CodePage.test.tsx::keeps a long thought's conclusion one click
  away rather than cutting it`。
- 保头保尾：`tests/domain/test_thinking_text.py`（9 条）、
  `tests/runtime/test_agent_runtime.py::test_a_long_chain_keeps_its_conclusion_in_the_durable_record`。
- 回传探测：脚本 `probe_roundtrip.py`（scratchpad，未入库），三次请求均 200。
  **本机本地证据，CI 离线不覆盖。**
- 实机：`demo-api` + `demo-worker` 真栈跑通一轮 `sparkline.html`，settle 后
  4 个步骤各自带着思考与它促成的动作，`is-live` 计数由 1 归 0。
