# ADR-052：撤不回的答案才可以边写边给人看

- 决策点：`AnswerReleaseSink` 是否对**每一个** Chat 形态都抹掉 `ModelDelta.text`；
  如果不是，判据是什么、由谁给出；判据错了会怎样
- 状态：**接受**，澄清（不是收窄）[架构基线](../architecture-baseline.md) §5 里
  answer release gate 那一段的作用域
- 日期：2026-08-13
- 影响：新增 `LiveTextPolicy`；`AnswerReleaseSink` 多一个构造参数（默认严格）；
  `TurnExecution` 多一个**必须实现**的方法。`ModelCompleted.text`/`output_ref`
  的处理、三个发布方法、终态事件、会话历史**均不变**
- 依赖：ADR-018（无接地对话是显式形态）、ADR-021 §3（读网页不算接地）、ADR-051

## 1. 背景：管道通了，流出去的是空的

ADR-051 把 transient 事件接到了订阅上。但 `AnswerReleaseSink.emit` 当时的写法是

```python
if isinstance(payload, ModelDelta):
    payload = payload.model_copy(update={"text": ""})
```

**对每一个形态都成立**。于是浏览器收到的是一串 `text` 为空串的 `ModelDelta`：
时序对了、帧对了、正文没有。

那行代码是对的，写它的时候也只可能这么写——当时唯一的问题是"检索来的答案能不能在
复核之前露出去"，答案是不能。这一条决定的是：**它管的是不是所有形态。**

## 2. 判据不是"用没用检索"

一开始最顺手的判据是"grounded 的不许，ungrounded 的可以"。它碰巧给出正确答案，
但理由是错的，而错的理由会在下一个形态上给出错的答案。

真正的判据是：**这一轮有没有可能以 `AnswerWithheld` 收场。**

因为 redacted 守的就是这一件事——模型写完到答案发出之间，某个授权可能被撤回，
于是"已经流出去的文字"变成"本不该被看到的文字"。这个状态存在，就不能边写边给；
不存在，就没有任何东西可守。

按这个判据看四个形态：

| 形态 | `authorized_revisions` | 能否 withheld | 判定 |
|---|---|---|---|
| `FixedTwoStepExecution` | 非空 | 能 | redacted |
| `AgenticExecution` | 非空 | 能 | redacted |
| `RoutedExecution` | 接地分支非空 | 能 | redacted |
| `UngroundedExecution` | **两条 return 都硬编码 `()`** | 不能 | **provisional** |

**读网页不改变这一点**，这也是判据必须写成"有没有可撤回的 revision"而不是
"有没有查过东西"的原因。`UngroundedExecution` 在 ADR-021 的兜底分支里会抓页面，
但一个抓来的页面没有 revision、没有 ACL、没有可复核的东西（ADR-021 §3）——
它不可能在这里到发布之间被撤回，因为它从来没有被授予过。

这也是为什么这条 ADR 说自己是**澄清**而不是收窄：基线那一段的开头就是
"对使用检索证据的 Chat"。它管的范围从来就是检索形态，只是实现比它管得宽。

## 3. 决定

### 3.1 `LiveTextPolicy` 是一对具名值，不是一个布尔

`Literal["redacted", "provisional"]`。值的名字就是它的论据；`live_text=True`
是一个谁都可以为某个形态翻一下的开关，而翻错的那一次没有任何东西会说话。

默认 `redacted`：一个没想过这个问题的调用方，拿到的是这个问题存在之前每个调用方
都有的行为。

### 3.2 问的是形态，而且它必须回答

`live_text_policy` 挂在 `TurnExecution` 协议上，**没有默认实现**。
一个忘了回答的新形态在类型检查处就红，而不是继承基类恰好选的那个读数——
两个读数里有一个是泄漏。

`AnswerModeSelector` 把问题转交给它 `select()` 出来的那个形态，自己不持有意见。
一个能和真正产出文字的形态意见相左的路由器，就是第二份口径。

### 3.3 `ModelCompleted` 在两种模式下都照样抹除

delta 是"正在写"，`ModelCompleted.text` 是**写完的候选答案**，而候选答案正是
`commit` / `commit_ungrounded` / `withhold` 三个方法存在的理由。
给 provisional 放行它，等于往 durable log 里放一份没有任何人决定要发布的答案。

### 3.4 抹除改成白名单

原来的写法是"遇到 `ModelDelta` 就抹"。那么一个将来新增的 transient 类型会
**默认穿过**围栏——而 transient 正是那些不经 durable log 直达订阅者的事件，
是最不该默认放行的一类。

现在模块持有 `_TRANSIENT_HANDLED`，并在 import 期断言它覆盖
`TRANSIENT_EVENT_TYPES`。给 `EVENT_DURABILITY` 加一个 transient 类型而不来这里
决定它可以带什么，进程起不来。

`ToolProgress` 明确放行，理由写在常量旁边：它的 `message` 是工具处理器描述自己
工作的话，从来不是模型写的，答案被 withheld 时它没有任何东西需要改变。

### 3.5 一条防御性的 backstop，并且承认它不可达

`ChatService` 在 produce 返回后检查：声称 provisional 却带回了
`authorized_revisions`，整轮失败，不发布任何答案事件。

**这条在本仓库的形态上不可达**，唯一的 provisional 形态两条 return 都硬编码空元组。
它守的是**将来**某个形态误标自己：那时文字已经按第一种声明流出去了，
没有任何安全的方式再按第二种发布。

这一点在代码注释、PR 正文和测试 docstring 里都写明了，因为一条看起来像回归测试、
实际由 stub 触发的用例，会让人以为覆盖了一条生产路径。

## 4. 后果

- Chat 的 direct/ungrounded 形态在浏览器里可以逐字出现；检索形态明确不能，
  它们的 delta 仍然是空串——空串在这里是有意义的信号（"正在生成，正文待复核"），
  界面必须把这两种情况显示成不同的东西，而不是都显示成一片空白。
- `docs/frontend-design.md` 里"不声称逐 token streaming"那一句在前端落地之后要改，
  且只能改成分形态的说法。改早了就是一句假话。
- 这条 ADR 明确**不**碰 Task：Task 跑在 worker 进程，transient 到不了 API 的通道
  （ADR-051 §3.1），所以它连"要不要放行"这个问题都还没有资格问。

## 5. 重审条件

- 出现第五个 Chat 形态时，重审 §2 的表。判据本身应该不用改，但表要长一行。
- 有人提出让检索形态也逐字（比如"先流出去，被 withheld 时前端擦掉"）时，重审 §2：
  擦掉发生在读者已经读过之后，那不是围栏，是道歉。
- `UngroundedExecution` 若某天获得一个会产生 `authorized_revisions` 的工具，
  §2 的表当场失效，而 §3.5 的 backstop 会把它变成失败而不是泄漏——那时应该改表，
  不是改 backstop。
