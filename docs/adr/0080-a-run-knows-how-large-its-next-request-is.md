# ADR-080：一次运行知道自己下一个请求有多大

- 决策点：`RunBudget` 管步数、工具调用数、累计 token、成本和截止时间——**没有一个是
  下一个请求的大小**。一段长对话越过模型窗口时，路径是 `_stream_model` →
  `adapters/models/deepseek.py`（**故意不读** error body，因为它可能把 prompt 原样吐
  回来）→ `provider_error: the provider rejected the request with HTTP 400` → 整轮
  失败、没有报告。读转录的人被这句话指向模型适配器，而那里恰恰是唯一没出问题的地方。
  要不要给运行一个上下文天花板；如果要，它的分母从哪来，以及拿什么数字去比
- 状态：**接受**。`ModelProfileSettings` 新增可选的 `context_window_tokens`；
  `domain/runs.py` 新增纯谓词 `context_reason_for()` 与 `StopReason` 取值
  `"context_limit"`；运行时在**每一轮之前**用上一次 prompt 的 `input_tokens` 去比
  `窗口 × runtime.context_soft_limit_ratio`。**明确不做**：不猜任何窗口数字，不做
  压缩（那是 ADR-081），不让这一轮变得可恢复（F-01 保持拒绝）
- 日期：2026-08-25
- 影响：`bootstrap/settings.py::ModelProfileSettings` 新增
  `context_window_tokens`；`bootstrap/projections.py` 的 `ModelProfileConfig`
  与 `AgentRuntimeConfig`／`ApiRuntimeConfig` 随之带上它与
  `context_soft_limit_ratio`；`domain/runs.py` 新增 `context_reason_for()` 与一个
  `StopReason`；`runtime/agent_runtime.py` 新增两个构造参数、`_RunLedger.
  last_input_tokens` 与循环顶部的检查；`apps/api/dependencies.py`（五处运行时）与
  `apps/task_worker/composition.py` 传参；`config/ownership.yaml` 两行；
  `web/.../CodePage.tsx::stopNote` 一个分支。
  **不动配置契约**：`config_schema_version` 保持 `1.18`——既有段下新增带默认值的叶子
  不抬版（`docs/configuration.md` 的版本表自己写着这条规则，`[model.main.pricing]`
  就是先例，它也没抬过版）

---

## 1. 背景：一句把人指向错误地方的错误消息

今天一次超窗的运行留下的全部证据是：

```
status: failed
stop_reason: error
error: provider_error: the provider rejected the request with HTTP 400
```

三处都在说谎，或者说三处都没说实话：

- `stop_reason: error` 说不出撞到了什么。
- `provider_error` 把责任归给供应商，而供应商是对的——那个请求确实装不下。
- 消息里没有任何数字。运维分不清"模型窗口太小"和"这一轮读了太多文件"。

适配器**故意不读** error body（一个 chat completion 的错误可以把 prompt 回显出来），
所以这条消息不可能变得更具体——**该变具体的地方不在适配器里**。

`RunBudget` 有五个天花板，一个都不管这件事。这不是遗漏：步数、工具数、token 总量、
钱、时间，全是"这次运行被授权花多少"，而窗口是"这个模型物理上装得下多少"。两者是不同
的量。

## 2. 决策

### 2.1 分母是可选的，因为这个仓库不知道它

`ModelProfileSettings.context_window_tokens: int | None = None`。

**不出厂任何数字**，与 `pricing` 完全同构，理由也一样：

- `config.default.toml` 里 `model_id` 写的是 `not-configured-deepseek-main`，一个占位符
  的窗口只能是编出来的。
- 它也不能一般地从名字推出来：`base_url` 可以指向一个把窗口改短了的网关。
- 发一个猜出来的数字，就是在每份配置里放一个看起来权威、其实不是的值——而这个值决定
  运行什么时候被掐掉。

不配它就没有这道天花板，过长的回合仍然死成 provider 400。**这是不说的代价，写进文档
而不是留白**（`docs/configuration.md` 的新一节）。

**没有交叉校验，而且这与 `max_cost_micro_usd` 的先例不矛盾。** 成本上限有"要了却给不
了"的状态（设了上限、没配价格），所以那一条在启动时被拒。这里没有对应状态：
`context_soft_limit_ratio` 永远有值（默认 `0.75`），它只是没有东西可以取分数。

### 2.2 拿什么数字去比：上一次 prompt，不是累计，也不是 `total`

**不是累计。** `BudgetUsage.tokens` 是跨轮累加的（运行时每轮 `merged`），而每一轮都
重发整段对话——累计输入随轮数近似平方增长。一个挂在 `halt_reason_for` 旁边、读
`usage.tokens.input_tokens` 的谓词会掐死健康的运行：十轮稳定 6,000 token 的 prompt
累计 60,000，对 64,000 的窗口来说已经"越界"，而其中没有一个请求接近过它。这条反例
写进了测试，因为这个错误太自然了。

**不是 `total`。** `TokenUsage.total` 是一轮里流动的一切——prompt **加**补全**加**
cache write——其中两样从来没在发出去的那个请求里。`input_tokens` 才是"我上次发出去的
东西有多大"，而且是供应商报的，不是这里拼出来的。

**它是滞后的，这一点要说出来。** 下一个 prompt 是这一个加上补全再加上工具返回的东西，
比被比较的数大。ratio 留下的余量就是覆盖这个的。一个试图预测下一个 prompt 的复合数
是一个"穿着测量精度的估计"，而且仍然会漏掉还没人要的那些工具结果。

### 2.3 谓词是自由函数，不是 `RunBudget` 的方法

它要的两个数都不是提交者能选的：窗口是这个部署的模型物理上装得下多少，ratio 是运维
愿意填到多满。把它们塞进 `RunBudget`，就是把一个关于供应商的事实写进"某个人授权了
什么"的记录里。

### 2.4 停在这里，报出自己撞到的天花板

检查放在循环顶部、与 `halt_reason_for` 同一处，因为它回答同一个问题的另一半：不是
"它花光了给它的额度吗"，而是"它接下来要发的东西装得下吗"。

**三个数字进消息**：上一次 prompt 多大、窗口多大、ratio 是多少。没有它们，运维拿到的
只是一个停止原因，仍然分不清换个模型和拆小任务哪个才对。

### 2.5 这是软上限，而且第一轮不受保护

- 检查在两轮之间，一次 `project_read` 能在检查通过之后再追加 48,000 字符。ratio 的
  余量吸收它。它保证的不是"下一个请求一定装得下"，而是"运行自己说出撞到了什么"。
- 检查读的是"上一次请求"的大小，所以**回合一之前没有可读的数字**。一个开局就超窗的
  prompt 仍然死在 provider 400。这是这个形状的既定限制，不是遗漏：真正的增长发生在
  跨轮累积工具结果的过程里，而那是从第二轮起就被看住的。

## 3. 被拒绝的方案

**出厂一个 DeepSeek 的窗口数字。** 拒绝，见 §2.1。这也是为什么本 ADR 落地后
**`config/` 下没有任何 profile 打开这道天花板**——和成本上限一模一样，那里也一个价格
都没配。能力梯子因此停在 Implemented + Tested：它被实现了、被测了，但在任何出厂
profile 上都不生效，打开它是一行配置。

**用 `ApproximateTokenCounter` 在发之前估一把。** 拒绝。它是 ingestion 的分块计数器，
按字符估 token；拿它守一个会掐掉运行的闸门，等于用一个估计值决定生死，而供应商每一轮
都免费告诉我们真实数字。真正的诱惑是"这样第一轮也能保护"——不值得，代价是整条路径上
从此有两个不一致的 token 概念。

**超窗时自动压缩再重试。** 这是对的下一步，也正是 ADR-081。放在这里做会把"诚实地
失败"和"补救"绑成一次改动，而后者需要先看见前者真的发生。**先有诚实的失败，再有补救。**

**把 `context_window_tokens` 做成必填。** 拒绝。它会让每一份现存配置在这次升级后停止
加载，换来的只是"每个部署都被逼着填一个它可能不知道的数"。

**改用一个新的 `ErrorCode`。** 不需要。`budget_exceeded` 说的正是这件事——一个天花板
拦住了它——而区别由 `stop_reason` 与消息里的三个数字承载。

## 4. 不变量

1. **没有声明窗口就没有天花板**，且运行的行为与本 ADR 之前逐字相同。
2. 判断读的是**上一次请求的 `input_tokens`**，永不读累计用量，永不读 `total`。
3. 检查在**每一轮之前**，所以被拦下的请求**没有被发出去**。
4. 停止原因是 `context_limit`，且消息里带着上一次 prompt、窗口与 ratio 三个数字。
5. 窗口与 ratio 都**不进 `RunBudget`**——它们不是提交者授权的东西。

## 5. 怎么验证

- `tests/runtime/test_budgets.py::TestARunKnowsHowLargeItsNextRequestIs`——6 条纯谓词：
  没声明窗口就没意见、软上限之下继续、达到即停、第一轮没有可判断的数字，以及两条
  **反例**：累计读法会掐死一个十轮稳定 6,000 token 的健康运行；`total` 读法会掐掉一个
  实际 prompt 只有 40,000、窗口还空着四分之一的运行。
- `tests/runtime/test_agent_runtime.py::TestARunKnowsHowLargeItsNextRequestIs`——5 条
  在循环上：超限的运行停在第一轮之后（`steps == 1`，也就是没有发出那个必然失败的
  请求）、停止原因带三个数字且不甩锅给 provider、没声明窗口的部署行为不变、限内的
  运行不受影响，以及 **ratio 真的是决定线在哪的那个旋钮**（同一次运行，0.5 停、0.9 过）
  ——否则它就是配置里一个什么都不决定的数，正是这个仓库反复删掉的形状。
- `tests/architecture/test_config_ownership.py`——新叶子必须有主，这条在本次改动中
  **先红后绿**，是守卫在工作的证据。

能力梯子停在 **Implemented + Tested**，并且要连着 §3 的那句一起读：出厂 profile 里
没有一个声明了窗口，所以这道天花板在任何默认部署上都**不生效**。「运维看到这条消息
之后能直接判断该换模型还是该拆任务」是**未经证实的**——被证实的是运行停在了发出那个
请求之前，并且报出了三个数字。
