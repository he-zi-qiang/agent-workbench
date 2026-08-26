# ADR-082：账号的问题不是这次调用的问题

- 决策点：DeepSeek 余额耗尽时返回 HTTP 402。适配器把 4xx 一律折成
  `provider_error`，运行以 `stop_reason: "error"` 结束，Code 控制台渲染出
  `这一轮没有跑完（error）`。这句话**没有一个字是假的**，但它和"模型 id 已下线"、
  "请求体不合法"、"上游 500 重试耗尽"长得完全一样——而这四件事要人去做的动作各不
  相同。要不要给"provider 拒绝的是账号而不是这次请求"一个自己的错误码；如果要，
  它凭什么进一个**封闭**词表；以及这条信息在哪一层被丢掉的
- 状态：**接受**。`ErrorCode` 新增 `provider_account_rejected`（401/402/403），
  适配器新增 `_rejection()` 从状态码单独判定；`/v1/code` 的 `AskResponse` 补上
  `error_code` 与 `error_message`——**信息真正丢失的地方在这里**，不在适配器；
  Code 控制台与 Task 失败文案各加一条分支。**明确不做**：不新增 `StopReason`、
  不新增异常类、不拆成三个码、不往仓库配置里写 provider 价格
- 日期：2026-08-26
- 影响：`domain/errors.py` 的 `ErrorCode` 增一项；
  `adapters/models/deepseek.py` 新增 `_ACCOUNT_STATUSES` 与 `_rejection()`，
  `_attempt()` 里的构造改为调用它；`apps/api/routes/code.py` 的 `AskResponse`
  增两个可选字段并在构造处读 `turn.outcome.error`；`web/src/api/types.ts`、
  `web/src/features/code/CodePage.tsx`（`stopNote` 签名 + 两条分支 + 调用处）、
  `web/src/features/work/failure.ts`（`CODE_LABELS` 一行、新增 `CODE_REMEDIES`）。
  **不动配置契约**：`config_schema_version` 保持 `1.18`，本次没有新增配置叶子

---

## 1. 背景：一个被量出来的失败，不是一个假想的失败

这一节把**量到的**和**从代码读出来的**分开写，因为混起来就成了一次假的测量。

**量到的**（2026-08-26，这份 checkout，`GET /user/balance` 返回 200）：DeepSeek 账户
余额 CNY 1.08、USD 0.00，赠送额度已为 0。`is_available` 仍是 `true`，所以调用**没有**
被一次性拒掉——它是跑到一半余额见底，然后这一轮断在半路。

**报上来的**：运行跑不完，提示"额度不足"。

**从代码读出来的**（现在由 `CodePage.test.tsx` 钉住）：`stopNote` 里没有任何一条分支
是 provider 失败能走到的，所以它们全部落到最后那句模板里，括号里是光秃秃的
`error`——既没提 provider，也没提账号。

于是这件事被当成"多 agent 把预算跑爆了"来排查，方向从第一步就是错的。而真相是：

- Chat（`apps/api/dependencies.py`）与 Code（同上）的 `RunBudget` **都没有设**
  `max_total_tokens`；`bootstrap/settings.py` 的默认是 `None`，九个 config profile
  里没有一个写过它。`token_budget` / `cost_budget` 这两个 stop reason 在任何现有
  部署里**都不会触发**。
- 没有任何 profile 写过 `[model.*.pricing]`，`_project_prices` 返回 `None`，
  `cost_micro_usd` 恒为 0。**这套平台自己不知道自己花了多少钱。**

也就是说：唯一知道"额度"这件事的是 provider，而 provider 说的那句话，在到达读者
之前被压成了一个单词。

## 2. 决定一：这个码凭什么进一个封闭词表

`ErrorCode` 是域的封闭词表，加一个词是有代价的——每个渲染错误的地方都多一条要么
写、要么漏的分支。这个代价此前**被明确拒付过一次**：`adapters/tools/web_search.py`
的注释写着，一个适配器的网络故障不配拥有自己的码，区别由 `message` 承担。

那条先例在这里不适用，理由是**它们不是同一类东西**：

| | `provider_error` | `provider_account_rejected` |
|---|---|---|
| 说的是 | 这次调用出了问题 | 这个部署的账号被拒绝了 |
| 再试一次 | 可能有用（5xx/429 就是这么分的） | 永远没用 |
| 修的地方 | 代码、配置、请求 | **在这套系统之外** |

最后一行是关键。`provider_error` 的所有成员，修法都在读者能碰到的东西里；这一个
不是——它要人去 provider 那边充值或换密钥。一个分不清这两者的读者会去翻自己的
YAML，而他该去翻的是账单。**"要人去系统外面做一件事"是一个域级事实，不是一句
措辞。**

`web_search` 那条注释仍然成立，并且这份 ADR 不放宽它：区别只在 message 的，就留在
message 里。

## 3. 决定二：一个码，不是三个

401（密钥被拒）、402（余额耗尽）、403（这把密钥不允许这次调用）合并成一个码。

**否掉三个码**的理由：系统对这三者的处置**完全相同**——停下、绝不重试、告诉人。
不同的只有那句话。而那句话正是 `message` 的职责，也正是 `web_search` 那条注释
说对了的部分。三个码会让每一处渲染多写两条永远一起出现的分支。

具体是哪一个，写在 `message` 里，并且带上状态码，供一周后读事件日志的人使用。

## 4. 决定三：不新增 `StopReason`

显然的做法是加一个 `stop_reason: "provider_account"`。**否掉**。

`StopReason` 回答的是"这个循环为什么停了"，而这里的答案就是 `"error"`——它没说错。
把 provider 的账号状态塞进 `StopReason`，等于把一个关于**外部账户**的事实写进
**运行如何结束**的词表，这跟 `context_reason_for` 那条注释拒绝把 context window
折进 `RunBudget` 是同一个理由：预算是有人授权这次运行花多少，上下文窗口是这个部署
的模型物理上能装多少，两者不是一件事。

正确的形状是：`stop_reason` 说循环怎么停的，`ErrorInfo` 说为什么。**两者本来就都
在 `AgentOutcome` 上**（`domain/runs.py` 的不变量甚至要求失败的 outcome 必须带
`ErrorInfo`）。缺的从来不是词表，是传递。

## 5. 决定四：信息是在路由丢的，不是在适配器丢的

这是这份 ADR 里最容易被跳过、也最要紧的一条。

适配器其实**一直**说得够清楚——`the provider rejected the request with HTTP 402`
里有 402。`AgentOutcome.error` 也一直带着它。丢掉它的是
`apps/api/routes/code.py`：`AskResponse` 只抄了 `status` 和 `stop_reason`，
把 `error` 留在了服务端。

所以即使不加任何错误码，光把 `error` 传出去，读者也能看见 402。加码是为了让控制台
能说中文、能给出**对的下一步**；传 `error` 才是修那个 bug。两件事都做了，但顺序
不能记反——否则下一个被折叠掉的字段还会以同样的方式消失。

`error_code` 与 `error_message` 两个都发：码是控制台写句子的依据，message 是它
**还没学会**的码的兜底。这条规则不是新发明的，`failure.ts` 的模块注释早就写着：
没被认出来的 detail 原样显示，因为它仍然是任何人手上最具体的东西。

## 6. 明确不做：不往仓库配置里写 provider 价格

排查过程中很自然会想到"顺手把 `[model.main.pricing]` 填上，这样
`max_cost_micro_usd` 就能用了"。**否掉**，理由是
`bootstrap/settings.py` 里 `ModelPricingSettings` 自己写着的那句：

> 价格是某个 provider 与某个部署之间合同上的事实，本仓库不知道它——发一个猜测出来
> 的数字，会让每个运维者的配置里多一个看起来权威、实际不是的数。

余额告急不构成把猜来的单价写进版本库的理由。价格仍然是运维者自己填的东西；填了
之后 `max_cost_micro_usd` 才开始有意义，这一点没有变化。

## 7. 证据

- `tests/contracts/test_deepseek_model.py`
  - `test_a_refused_account_is_not_reported_as_a_provider_error`：401/402/403 落在
    新码上且不可重试，状态码仍在 message 里；400 作为对照仍是 `provider_error`
  - `test_a_refused_account_is_never_retried`：`max_retries=3` 下只发一次请求
  - `test_an_http_error_reports_its_status_and_nothing_else`：原有的**不泄漏**断言
    保持在 401 上（它是最可能在响应体里回显密钥的状态码），只更新了期望的码
- `tests/api/test_code_api.py`
  - `test_a_failed_turn_carries_why_and_not_just_that_it_stopped`
  - `test_a_turn_that_finished_says_nothing_about_errors`：完成的一轮两个字段都是
    `null`，控制台判"有没有"而不是判"值等于什么"
- `web/src/features/code/CodePage.test.tsx`
  - `names the account when the provider refused it`：并断言那句
    「直接说下一步就能继续」**不出现**——理由同 `context_limit`：同一个会话的下一轮
    会用同一个账号再撞一次
  - `shows the server's own words for a failure it has no phrase for`
- `web/src/features/work/failure.test.ts`
  - `sends a refused account to the provider, not back to the config`：非重试类的
    默认建议是"先改动任务或配置"，对这个码是错的，所以 `CODE_REMEDIES` 覆盖它
