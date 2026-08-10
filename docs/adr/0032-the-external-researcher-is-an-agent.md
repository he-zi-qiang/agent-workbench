# ADR-032：外部研究节点在拿到工具时是一个 Agent，而不是一次写死的搜索

- 决策点：`research_external` 到底跑什么；ADR-027 给 `researcher_external` 的动态工具
  怎样才真的到得了模型；这个节点产出的证据由谁保证是"读到的"而不是"想起来的"
- 状态：**接受**
- 日期：2026-08-09
- 影响：`research_external` 在部署注册了 research 受众的工具时多跑一次 agent run；
  `researcher_external` 的 system prompt 变成 JSON 契约；新增
  `decode_external_evidence_output`。`CANONICAL_V1_NODE_IDS`、图的形状、`admits`、
  授权信封的构造方式**均不变**
- 依赖：ADR-020（`external_search` 仍然是搜索这件事的实现）、ADR-025、ADR-027

## 1. 背景：一条声明了但没有接上的线

ADR-027 §3.3 写的是"上网取材属于研究，所以把动态 MCP 源加给 `researcher_external`"。
profile 上确实加了 `dynamic_tool_sources={"research"}`，Worker 组合根确实按 audience
把 `mcp_web_fetch_page` 分给了它，配置、信封、scope、Gateway 全都齐了。

但真 Worker 上这个节点**从不调用模型**。`build_task_v1_handlers` 里 `research_external`
在 `research is not None`（真 Worker 恒真）时直接调 `research.external.gather(...)`，
那是 `GatewayExternalEvidence` 里一次参数写死的 `external_search`。只有
`research is None` 的 demo 分支才走 `artifact_handler("research_external")`，也只有
那条路径会用到 profile 的动态工具。

于是 `dynamic_tool_sources={"research"}` 在生产路径上是死代码，ADR-027 的
"researcher_external 拿到读网页的工具"在真 Worker 上不可能发生。

本机实测（task_9eac0636…）的形状是：信封里有 `mcp_web_fetch_page`，principal 带
`mcp:web`，Worker 也 discover 到了两个远端工具，而事件流里 `research_external` 这个
graph node **一条 `RunStarted` 都没有**。

**测试为什么没挡住**，值得单独记一笔：
`test_each_server_reaches_the_agent_its_audience_names` 断言的是
`profile_with_dynamic_tools(profile_for("research_external"), dynamic_tools)` 的返回值。
那证明了"目录会被交给这个 profile"，没有证明"图里那个节点会用这个 profile 跑起来"。
一个只测装配、不测调用的断言，正是这类缺陷的藏身处。

## 2. 为什么不是把 `external_search` 交给模型就完了

最直接的写法是让这个节点永远跑 agent 循环，把 `external_search` 也变成它的一个工具。
本 ADR 不这么做，理由是代价不对称：

- `external_search`（ADR-020）今天的形态是**确定性**的一次调用，evals 测的就是这个形状。
  把它改成模型自选的工具，等于在没有任何新需求的情况下改动被测基线；
- 没有配置 research 受众工具的部署（也就是绝大多数）会因此多付一次模型调用，
  换来的行为和今天完全一样。

## 3. 决定

### 3.1 只在这个 Worker 真的注册了 research 受众工具时，多跑一次 agent run

节点保留原来的确定性搜索，然后——**仅当** `dynamic_tools["research"]` 非空时——再跑一次
带这些工具的 agent run，两半的 `evidence_refs` 合并成这个节点的贡献。

这让改动是**纯加法**的：目录为空的部署一步不多走，一分钱不多花，事件流一个字不变。

合并用的是图自己的 fan-in reducer 语义（`merge_refs`）。这不是巧合：这两半如果作为两个
分支到达，图本来就会这样合并它们。

### 3.2 它的产出是证据包，不是散文

`synthesize` 把 `evidence_refs` 当 `EvidenceBundle` 读，而 `EvidenceStore.load` 会拒绝
任何不是 `evidence_bundle` 的 artifact。所以这个 run 不能走"把模型输出存成 Markdown"
的那条路——它必须交出**每条内容绑着来源 URL 和标题**的条目。

因此 `researcher_external` 的 system prompt 变成 JSON 契约，和 planner、critic 同一形状：

```json
{"items":[{"url":"https://...","title":"...","text":"..."}]}
```

契约里明确写了"只记录工具真的返回给你的内容"。这不是礼貌用语：一个能凭记忆填 URL 的
研究节点，产出的东西读起来和真证据一模一样，而这正是 ADR-020 当初拒绝"从标题编数字"
的同一个失败模式。

### 3.3 空答案是答案，坏答案是故障

`{"items":[]}` 表示"没读到能站得住的东西"，节点照常记账、不贡献证据、图继续走——一个
research 分支本来就允许空。

解析不了的输出则让节点失败，和 planner、critic 一致。理由是不对称的：把"读不出"降级成
"没读到"，下一个节点会在沉默上写出一份有模有样的报告，而没有任何地方说过它是凭空写的。

> **[ADR-034](./0034-a-structured-node-asks-once-more.md) 收窄了这一条。** 这里写的
> 严格，严的是"没人读得懂的输出不能变成没读到"，而不是"消息前面多一句话就该死"——后者
> 把本节明文允许的 `{"items":[]}` 也一起判了。今天读不出来的消息会先换来一次纠正轮次；
> 解码器接受的东西一个字没放松，第二轮仍然读不出来的节点照样失败。

## 4. 后果

- **两个开关仍然各管各的。** `research.enabled` 决定搜索那一半，MCP 的 `audience`
  决定读网页那一半。本机的 web profile 只开了后者，于是搜索那半在 Gateway 被拒
  （事件流里看得见），读网页那半正常工作；
- **一个 Task 在这个节点上可能出现两条证据。** 这是有意的，也是可分辨的：搜索来的条目和
  模型自己读的条目都带 URL，但它们在不同的 bundle 里；
- **成本，以及默认上限装不下它。** 配置了 research 受众工具的部署，每个 Task 在这个节点
  多一次模型调用。而 `multi_agent.max_tokens_per_agent_invocation` 的默认值 16000
  **不够**：实测一页正文 20–50 KB，两次读约 28000 tokens，run 会以
  `budget_exceeded: token_budget` 停在半句 JSON 上——工具全部成功，节点仍然失败。
  这不是本 ADR 引入的新约束，而是 ADR-030 那条约束第一次遇到会读东西的节点。
  默认值保持不变，`config/config.web-local.toml` 单独提到 120000；
- **重放不变。** 两个工具都是 GET 且 `retryable_effects=true`（ADR-027 §1），
  整节点重放会重新取一次页面，拿到的是那一刻的内容——这一点本 ADR 没有改动。

## 5. 重审条件

- ADR-031 的 v2 图落地、并且它的工作节点也需要读外部世界时，"哪个 profile 订阅 research
  受众"要重看一遍；
- 如果 `external_search` 本身被改成模型自选的工具，§2 的理由就失效了，届时这两半应该合成
  一次 agent run 而不是两次调用。
