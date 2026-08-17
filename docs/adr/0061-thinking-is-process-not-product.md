# ADR-061：思考是过程，不是产物

- 决策点：模型的思维链要不要成为协议一等公民；它是 transient 还是 durable；
  它受不受答案的发布围栏管辖；它进不进下一轮的上下文；哪些界面显示它
- 状态：**接受**。`ModelThinkingDelta` 与 `ModelDelta` 平行地进 port 事件联合
  与领域事件表，登记为 **transient**；durable 的那一半是
  `ModelCompleted.thinking_preview`（BoundedText 上限的摘录）；发布围栏对
  思考与答案**同一政策**；思考**永不**回填对话账本；请求侧新增
  `thinking: bool | None` 开关，与 profile 的 `thinking`/`reasoning_effort`
  正交；Chat 四个形态一律钉 `False`
- 日期：2026-08-17
- 影响：`ports/model.py`（第五种 ModelEvent + `ModelRequest.thinking`）、
  `domain/events.py`（新 transient 事件 + `ModelCompleted.thinking_preview`）、
  `domain/runs.py`（`AgentRunRequest.thinking`）、
  `adapters/models/deepseek.py` 与 `fake.py`（双实现同步）、
  `runtime/agent_runtime.py`（`_consume` 新分支 + `_ModelTurn.thinking`）、
  `application/answer_release.py`（白名单 + 同政策）、
  `application/chat_execution.py`（四处钉 False）、`apps/api/sse.py`（分种合帧）、
  `bootstrap/settings.py`＋`projections.py`＋`model_factory.py`＋
  `config/ownership.yaml`（两个新叶子，**带默认值，不抬 schema**）
- 依赖：ADR-019（事件描述而非复制）、ADR-035（答案不是摘要）、
  ADR-051（live 帧没有位置）、**ADR-052**（撤不回的答案才可边写边看——本 ADR
  把同一判据延伸到思考）、用户决策 2026-08-16（「切 reasoner 拿真思维链」
  「覆盖 Code 逐字 + Task 实况，不做 Chat」）

## 1. 探测：先量，再定

四条事实，2026-08-16/17 对 `api.deepseek.com` 实测（本地证据，CI 离线不覆盖）：

1. **思考与工具调用兼容**。`deepseek-v4-flash` + `thinking:{type:"enabled"}` +
   `tools` 同一请求被接受：模型先流 `reasoning_content`（逐词），推理完毕后
   在同一条流里发出 `tool_calls` 分片，`finish_reason: "tool_calls"`，
   `usage.completion_tokens_details.reasoning_tokens = 15`。**计划里"若互斥则
   思考只开在无工具轮"的备用分支不需要了。**
2. **不带 `thinking` 参数时，具体 id 与别名的默认行为不同**——这是三值设计
   的实测依据，同一个提问各发一次：

   | `model` | 响应自陈 | `reasoning_content` | `reasoning_tokens` |
   |---|---|---|---|
   | `deepseek-v4-flash` | `deepseek-v4-flash` | **有**（231 字符） | **80** |
   | `deepseek-chat` | `deepseek-v4-flash` | 无 | 无 |

   别名解析到同一个模型，却带着一套「不思考」的默认。所以「切到具体 v4 id
   却不声明立场」会静默买推理，而「继续用别名」不会。
3. **wire 形状**：思考阶段 `delta.content` 为 `null`、`delta.reasoning_content`
   为文本；边界帧一条 delta 同时带两者。
4. 本 ADR 初稿把第 2 条写成了「别名不思考 ⇒ v4 不思考」，又在 §5 断言
   「v4 默认思考开着」，两处互斥。上表是补测后的口径：**两句话都对，主语
   不同**。记在这里而不是改掉，因为读者会按这段去配新 profile。

第 3 条决定了解析器的形状：`_text_events` 返回**两种**事件的列表，
reasoning 在前——那是它被写出的顺序，倒过来会让答案出现在产生它的思考之前。

## 2. 决定：双轨，一条实时一条留痕

**transient 的那一半**：`ModelThinkingDelta` 与 `ModelDelta` 完全同构——
token 速率、live 通道扇出、断线不可回放。它是"正在想"的那个体验。

**durable 的那一半**：`ModelCompleted.thinking_preview`，BoundedText 上限。
选择扩展既有事件而不是新开一个 durable 思考事件类型，理由是**归属**：思考属于
那一次模型调用的记录，和它的 `finish_reason`、`usage`、`text` 是同一件事的
不同侧面；单独的事件类型会让"这次调用想了什么"和"这次调用做了什么"在时间线上
分成两行，读者要自己拼。

摘录而非全文，是 ADR-019「事件描述而非复制」的显式例外的**小号版本**：
`text` 拿的是 ANSWER 级上限（ADR-035：答案不是摘要，产物必须完整），
`thinking_preview` 拿的是 preview 级上限（4096）——因为完整的链已经实时流过，
留痕的职责只是让**没在看直播的人**（重连的读者、只有轮询的 Task 时间线）
知道它想过什么。

**Task 的实况就是这一半。** Worker 是独立进程，`LiveEventChannel` 仅进程内
扇出（ADR-051 的形状），所以 Task 没有 live 通道可用——每个模型轮结束时
`ModelCompleted` 落库，2.5s 轮询把它带到 WorkPage。粒度是"每个模型轮"
而不是"每个 token"，这是架构事实，不是没做完。

## 3. 围栏：思考跟着答案走，不是跟着工具进度走

`_TRANSIENT_HANDLED` 白名单里现在有三种。`ToolProgress` 直通，因为它的文字是
工具处理器描述自己的工作，不是模型写的。`ModelThinkingDelta` **跟 `ModelDelta`
同政策**：redacted 形态抹空、provisional 形态放行。

判据就是 ADR-052 的那一条，只是把主语换掉：模型**推理的对象**正是它被给到的
证据，所以一段思考可以逐字复述那些段落——而 `AnswerWithheld` 的全部意义，是
这些段落此刻已经不该被这个 principal 看到。若思考不受管辖，它就是
`AnswerCommitted` / `ModelCompleted.text` / `AnswerWithheld` 之外的**第四条
文本逃逸通道**，而且是最不设防的一条，因为没人会想到去检查它。

`ModelCompleted.thinking_preview` 与 `text` 一起被抹（两种政策下都抹）：
durable 的候选文本由三个发布方法**刻意地**释放，这条规则不为过程文本让路。

## 4. 思考不进上下文

DeepSeek 要求 `reasoning_content` 不得回传下一轮。本仓的做法比"记得别传"更硬：
`domain/messages.py` 的 content block 只有 text / tool_use / tool_result 三种，
**不为思考新增第四种**；`_ModelTurn.thinking` 是运行时的一个字段，
`assistant_message(text=turn.text)` 回填时它根本没有可去的地方。
一个不存在的容器不会被忘记清空。

## 5. 两轴正交：profile 说"能不能"，request 说"这轮要不要"

- **profile 轴**（配置）：`thinking = unsupported | disabled | enabled` +
  `reasoning_effort = low | high | max`。三值而非布尔，是因为
  `unsupported`（默认）意味着**一个键都不发**——对没被探测过的模型或网关，
  未知键可能直接 400，"没人说过"必须落在"什么都不发"上，而不是"发 disabled"。
- **request 轴**（代码）：`thinking: bool | None`。`None` 用 profile 的默认；
  `True`/`False` 覆盖它；对 `unsupported` 的 profile，覆盖被忽略——没有参数
  可发就是没有。

**显式发送两个方向**是有代价换来的，理由就是 §1 第 2 条那张表：具体的
`deepseek-v4-flash` 不带参数时**默认思考**。一个从别名切到具体 id 却没声明
立场的 profile，会静默地为没人要的推理付钱——所以 `disabled` 也要发。
反过来，仍指向 `deepseek-chat` 的 profile（本仓的 `[model.compact]` 与所有
非 demo 环境）留在 `unsupported` 是安全的：那条路径本来就不思考。

Chat 的**五个** `run_kind="chat"` 构造器一律钉 `False`：
`chat_execution` 的四个（fixed / agentic / ungrounded / web fallback），
加上 `task_triage` 的分类器——它最需要这一条，因为调用方压着十秒客户端
超时、输出预算只够一个小 JSON，推理会同时吃掉这两样，而截断的裁决会静默
落回默认值。Chat 界面不渲染思考（用户决策），围栏对 redacted 形态又会抹掉
它——买一段既看不见又要被抹掉的推理，是纯粹的浪费。

测试逐个构造器断言，并且**中间那一跳也钉住了**：
`AgentRunRequest.thinking → ModelRequest.thinking` 只有一行赋值，删掉它
类型检查照过、其余测试照绿，而后果正是本节要防的事
（`test_the_runs_thinking_switch_reaches_the_model_request`）。同一条测试
顺带要求 `FakeModel` 遵守这个开关——一个在这件事上与真适配器答案不同的
替身，没法用来测试问它的调用方。

## 6. 被拒绝的方案

**`ModelDelta` 加一个 channel 字段**。省一个事件类型，代价是每个下游消费者
（Chat reducer、Code hook、CLI、SSE 合帧）都要在用它之前先分流，而漏分流的
默认行为是**把思考当答案显示**——最坏的失败方向。分成两种类型，漏处理的默认
行为是不显示。

**思考全文落库**。可回放、可审计，但与 ADR-019 正面冲突，且思考的体量常常
超过答案本身；真正需要全文的场景（调试一次坏推理）由实时流覆盖。

**为思考单开可见性开关（display 轴）**。Claude 的 `display` 之所以独立，是因为
它的思考对**所有**调用都存在，可见性是纯 UI 决定。这里不同：`thinking=False`
连推理都不产生（省钱），`live_text` 政策管可见性（安全）——两个已有的旋钮
已经覆盖了那两件事，第三个开关只会造成"开了但看不见"的组合。
