# ADR-046：加法的那一半不许做减法

- 决策点：`research_external` 里读网页那半的 run **停在自己的上限、被守卫掐断、被传输
  故障打断**时，节点该不该失败；ADR-032 §3.1 说的"纯加法"在**失败方向**上算不算数；
  这条豁免该给哪些节点、不该给哪些
- 状态：**接受**，收窄 [ADR-032](./0032-the-external-researcher-is-an-agent.md) §4
- 日期：2026-08-13
- 影响：`_decoded` 多一个 `halted` 参数，只有 `research_external` 传它。解码器接受的东西、
  纠正轮次的规则（ADR-034）、图的形状、`admits`、授权信封、`CANONICAL_V1_NODE_IDS`
  **均不变**
- 依赖：ADR-030（每次 invocation 一份预算与墙钟）、ADR-032、ADR-034

## 1. 背景：证据已经在手上，任务死在拿到它之后那一步

2026-08-13 凌晨，本机 console profile（`config.demo-local.toml`、真实 DeepSeek、
PostgreSQL 5433 + 两个 MCP server），同一条 objective——"请你创建一个 word 文档，内容是
调研 deepseek 最新模型 1000 字"——连提六次，六次全死。三小时内 Worker 记下的
`RunFailed` 共 7 条，其中 6 条在 `research_external`：

| 时刻 (UTC) | 停止原因 | 步/工具调用 |
|---|---|---|
| 04:29 | `provider_error`：`ProxyError` | 9 / 16 |
| 06:11 | `budget_exceeded`：`token_budget` | 11 / 20 |
| 06:20 | `budget_exceeded`：`token_budget` | 12 / 22 |
| 06:32 | `provider_error`：`RemoteProtocolError` | 3 / 4 |
| 07:02 | `budget_exceeded`：`token_budget` | 10 / 18 |
| 07:13 | `tool_failed`：`3 were refused as repeats` | 7 / 14 |

停止原因有四种，形状只有一个：**`external_search` 那一半早就成功了**。07:13 那次的事件流
里，节点的第一件事就是把一份 11247 字节的 `evidence_bundle` 存进了 artifact store——控制台
的"附件 · 检索到的证据"显示的正是它——然后读网页那半的 run 开始跑，抓了八个页面，撞上
守卫，节点抛 `TaskNodeRunFailedError`，整条 Task 以 `execution_failed` 收场。

用户看到的是：一条列着"收集资料 23 步"的时间线，一个下载得到的证据附件，和一句"任务失败"。
它读到的东西没有一样进入过下一步。

**这正好是 ADR-032 §3.1 承诺过不会发生的事。**那一条写的是"这让改动是**纯加法**的：目录为
空的部署一步不多走，一分钱不多花"。加法说的是能力：注册了 research 受众工具的部署多读几页。
但在实现里它同时是**减法**——注册了工具的部署，会因为多出来的那次 run 失败而丢掉整条 Task，
而同一条 Task 在没有目录的部署上会正常走完。目录成了一种风险，而不只是一种能力。

§4 那条"工具全部成功，节点仍然失败"的后果，当时是就 token 上限一件事记的，缓解办法是
把 `config.web-local.toml` 的上限单独提到 120000。上面六次里有三次就是在 **120000** 上撞的
——上限不是没调够，是这条路径上"读得多"和"读得完"本来就没有一个能同时满足的数。另外三次
更说明问题：代理抖动和重复守卫跟上限一点关系都没有，调多大都挡不住。

## 2. 为什么不是把上限再调大、或者让守卫别杀

**调大上限**是把同一件事再赌一次。这个 run 的输入是网页正文，它的大小由被读的站点决定，
不由部署决定：实测同一批 deepseek 文档页，一次 6 步读完 36805 token，另一次 12 步就冲过
120000。而且它挡不住 07:13 那次——那次是模型对着一批互相重定向的页面反复抓同一个 URL，
[ADR 之外的守卫](../../src/agent_workbench/runtime/agent_runtime.py)按内容记账把它掐了，
掐得对：一个不断重复自己的 run 不是在进展。上限再高，只是让它多烧一会儿。

**让守卫别杀**方向就错了。守卫存在的理由是"再问第四遍不会有新答案"，撤掉它等于把
2026-08-13 02:30 修掉的那个循环放回来。

真正错位的地方不在这两处，而在**谁为这次失败付账**。这半个节点是可选的、附加的、
读的是搜索那半已经找到的东西；让它的失败去决定整条 Task 的生死，是把一次机会成本记成了
一次致命故障。

## 3. 决定

### 3.1 停下来的 run 交出"什么都没读到"，而不是让节点失败

`research_external` 的读网页那半，run 以 `status == "failed"` 收场时——撞上限、被守卫掐、
被 provider 断——节点**把它当成"没读到"**：不贡献证据，照常记账，图继续走。搜索那半已经
存下的 bundle 原样留在 `evidence_refs` 里。

这不是把 ADR-032 §3.3 那条不对称放松了。§3.3 防的是"**读不出**降级成**没读到**"，怕的是
下一个节点在沉默上写报告——而那里模型是**说了话的**，只是没人读得懂，所以它的话必须被当成
一个错误的断言。这里模型**一个字都没说完**：没有断言，就没有断言被降级。ADR-034 §3.2 结尾
写的"撞上 token 上限、停在半句 JSON 上的 run 依旧当场失败"，说的是它不该换来一次纠正轮次
——那一条不动，一个从来没写完的答案仍然不值得买第二次整轮。变的只有：这半个节点没写完
答案时，赔的不再是整条 Task。

而"沉默上写报告"在这里也不成立：搜索那一半的证据在，`synthesize` 拿到的是真读到的东西。
就算两半都空，`{"items":[]}` 和 `ExternalEvidenceSkipped` 早就允许这个节点交白卷
（ADR-032 §3.3），空的研究分支本来就在图的语义里。

### 3.2 这条豁免按节点给，不按错误给

改的是 `_decoded` 的一个参数，不是 `_require_completed` 的判断。四个结构化节点共用同一个
严格解码器（ADR-034 §3.4），共用同一个"run 没跑完就失败"的判断；这一条只有
`research_external` 传 `halted`。

分界线是**这个节点被允许交白卷吗**：`plan` 欠图一份计划，`critic` 和 `review` 欠一份裁决，
没有哪种状态是"它什么都没给出来而图继续走"。`research_external` 的读网页那半有，而且那个
状态图里已经有名字了。写成参数而不是写成一张节点名单，是因为名单会在第五个节点上过期，
而参数就在调用点上，看得见。

有一条对照测试钉着这件事：同一个"撞上限"的 executor 喂给 `plan`，它必须照样失败。

### 3.3 取消不算停摆

只有 `status == "failed"` 走这条路。`cancelled` 照旧让图停下——一条被人按停的 Task，不能
因为它恰好停在一个容许空答案的节点上，就接着去写它的报告。

### 3.4 降级不是免单，也不是不说

两次 run 的用量和 run id 照旧进节点的状态增量（ADR-034 §3.3 的加法通道不变）：读到一半的
run 花掉的钱，账上一分不少。

也不静悄悄：这次 run 自己的 `RunFailed` 带着它的上限和错误码留在事件流上，挂在
`research_external` 这个 graph node 下面。控制台的时间线本来就按节点折叠事件，所以"这一步
没读完"是看得见的——只是它不再等于"这条任务没了"。

## 4. 后果

- **一条成功的 Task 可能比它打算读的少读了几页。** 这是本 ADR 明码标价买下的东西：拿
  "报告的材料少一点"换"报告存在"。买得起的原因是搜索那半的证据仍然在，且哪一步没读完在
  时间线上写着；
- **两半的失败语义从此不同。** `external_search` 被 Gateway 拒是 `ExternalEvidenceSkipped`
  （早就不致命），读网页那半的 run 停摆现在也不致命。这个节点因此没有任何一条能杀死 Task
  的路径了——它要么贡献证据，要么不贡献；
- **上限该多大重新变成一个成本问题**，不再是一个可用性问题。`config.demo-local.toml` 的
  120000 保持不变：调它现在只影响读多少，不影响能不能走完；
- **B-06 没有被这一条覆盖。** provider 抖动（`ProxyError`、`RemoteProtocolError`）在这个
  节点上不再致命，但在别的节点上照旧致命，而且照旧带着一个没人读的 `retryable: true`。
  见 [known-gaps.md](../known-gaps.md) B-06。

## 5. 重审条件

- 如果将来 `synthesize` 开始**要求**外部读取的证据（而不是"有就用"），§3.1 的理由就变了：
  那时一个空的读网页半边会让下一步无据可写，该在那里立一条明确的前置条件，而不是让这里
  重新变成致命；
- 如果 ADR-032 §5 说的那件事发生了——`external_search` 本身变成模型自选的工具、两半合成
  一次 run——那么"哪一半失败"就不再可分，本 ADR 的分界线要重画。

---

**验证**（2026-08-13，本机 console profile，真实 provider）：把
`multi_agent.max_tokens_per_agent_invocation` 临时压到 25000，让读网页那半必定撞上限，
提交同一条 objective（`task_387baac8…`）。事件流：`research_external` 记下
`RunFailed: the run passed its ceiling: token_budget`，随后图继续走
`synthesize → critic → synthesize → critic → synthesize → critic`，以 `TaskSucceeded`
收场，产出一份 38115 字节的 `mcp-result.docx`。同一条 objective 在改动前的六次里
六次都死在 `RunFailed` 之后那一步。
