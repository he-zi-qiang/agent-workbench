# ADR-039：配置里的一个指标名字是一句承诺

- 决策点：当配置声明的评测能力和实现对不上时，以哪个为准；`evaluation` 这一节
  该不该继续承载路线图语义
- 状态：**接受**
- 日期：2026-08-11
- 影响：配置 schema `1.13` → `1.14`。`evaluation.ragas_enabled` 默认由 `true`
  改为 `false`，且写 `true` 会**在配置加载阶段失败**；`evaluation.rag_metrics`
  只接受 `evaluation.metrics.RETRIEVAL_METRICS` 里真的存在的名字，默认清单从
  19 项收窄为 5 项。**没有任何运行时行为改变**——这两个字段此前没有任何代码读取，
  这正是问题本身。
- 依赖：[ADR-017](./0017-llamaindex-primary-rag.md)（RAGAS 作为离线 LLM-judge
  辅助的口径）

## 1. 背景：这一节说了三件代码做不到的事

`[evaluation]` 此前是这样的：

```toml
ragas_enabled = true
rag_metrics = [
  "recall_at_k", "precision_at_k", "mrr", "ndcg_at_k",
  "rerank_ndcg_delta", "rerank_recall_at_k", "rerank_latency_ms",
  "faithfulness", "factual_correctness", "answer_relevance",
  "abstention_rate", "abstention_accuracy",
  "citation_precision", "citation_recall", "citation_locator_accuracy",
  "retrieval_latency_ms", "generation_latency_ms", "token_usage", "cost",
]
```

对着代码逐项核：

- 仓库里**没有** RAGAS 依赖、runner 或 judge 校准集。`ragas_enabled` 除了
  `settings.py` 的字段声明和一条 schema 测试之外，没有任何读取方。
- `RETRIEVAL_METRICS` 注册表里只有五个名字：`recall_at_1`、`recall_at_3`、
  `full_coverage_at_3`、`mrr`、`retrieval_latency_ms`。上面 19 个名字里对得上的
  只有 `mrr` 和 `retrieval_latency_ms` 两个——连 `recall_at_k` 都不是注册表里的
  键，因为注册表按实际的 k 展开。
- 剩下 17 个分三类：Answer / 拒答 / Citation 那些需要一个判定器，
  rerank 那些需要重排前后两份名次，token / cost 那些需要计量。三样都不存在。

更糟的是第三点：`tests/config/test_settings.py` 里有一条断言**要求**
`precision_at_k`、`factual_correctness`、`abstention_rate`、`citation_precision`、
`citation_recall` 必须在这份清单里。也就是说，一个测试在守着这份虚构清单不被删掉。
删掉其中任何一个名字，CI 会红——它把"配置说了假话"变成了一条受保护的性质。

这三件事叠起来的效果，不是少了几个指标，而是：**一个读配置的人得到的结论是错的。**
他会认为这个项目在给答案打分。README 在另一处诚实地写着 RAGAS "整体保持
Planned"，于是同一个仓库的两个文件互相矛盾，而配置那份看起来更权威——因为它是
代码要加载的东西。

## 2. 决策：这份文件说什么在跑，路线图写在计划里

`config/` 下的文件是**运行时契约**，不是意图声明。判据很简单：这个文件里的每一
个值，都应该能被指到一段真的会读它的代码。做不到的，属于
[实施计划](../implementation-plan.md)。

于是：

1. `rag_metrics` 只允许出现 `RETRIEVAL_METRICS` 的键。多写一个名字，配置加载
   阶段就失败，错误信息里列出可用的名字。
2. `ragas_enabled` 只允许 `false`。
3. 那 17 个名字不是被删掉，是被移回它们本来的位置——路线图。

## 3. 为什么是"拒绝 true"而不是"默认 false"

默认改成 `false` 只修好了默认值，陷阱一动不动：下一个想要答案级评分的人把开关拨到
`true`，进程正常起来，什么错误都没有，他合理地认为功能开了。这和现在的状态是同一个
缺陷，只差一次编辑。

一个背后没有任何代码路径的开关，唯一诚实的响应是**拒绝这份配置并说清缺什么**。
错误信息点名缺的是依赖、runner 和校准集，并指向 `docs/status.md`。

这不是新形状。`online_judge_in_ci: Literal[False]`、`ragas_offline_only:
Literal[True]`、`testing.allowed_failpoints` 对 `CANONICAL_FAILPOINTS` 的白名单
校验，都是同一条规矩：**配置不能请求一个这套二进制给不了的行为。**
这里用校验器而不是 `Literal[False]`，只是为了让错误信息能解释原因——`Literal`
给出的报错说不清"为什么不行"。

等 runner 落地，这条校验和它一起删掉，那次删除本身就是"能力真的到了"的证据。

## 4. 为什么校验器指向注册表，而不是自己抄一份

`CANONICAL_FAILPOINTS` 是在 `settings.py` 里就地写死的一个 `frozenset`。
`IMPLEMENTED_RAG_METRICS` 没有照抄这个做法，而是
`frozenset(RETRIEVAL_METRICS)`——直接指向计算它们的那个注册表。

区别在于两者会怎么漂移。failpoint 的名字是**协议**：注入点分布在多个 Adapter 里，
没有一个天然的单一定义处，所以在配置层集中列一份是合理的。指标不是：它们有一个
明确的所有者，就是那个注册表，而漂移的方向是可以预测的——有人往配置里加了打算实现
的指标，注册表没跟上，然后一份 gold set 报告悄悄少一列。指向注册表让"配置得出来"
和"算得出来"由构造成为同一份清单。

方向上也是安全的：`bootstrap` 是外层包，允许依赖核心层；`evaluation.metrics`
只 import 标准库，不会把重依赖拖进配置加载路径。

## 5. 代价与边界

- **这是一次不兼容收窄。** 一份 `1.13` 的配置文件如果带着旧的 19 项清单或
  `ragas_enabled = true`，在新二进制上**加载失败**。这正是 `config_schema_version`
  这个 pin 存在的理由，所以版本升到 `1.14`。
- **`task_metrics` 与 `multi_agent_metrics` 没有一并校验。** 它们是同一类问题——
  Task 与 Multi-Agent 的 runner 同样不存在——但没有注册表可指，抄一份写死的名单
  就会犯 §4 说的那个错。等对应 runner 落地时一并处理；在那之前，
  [配置文档](../configuration.md)明写这两项的 runner 尚未落地。
- **没有任何运行时行为改变。** 这次改动不会让任何一次评测跑得不一样，因为此前
  就没有代码读这两个字段。改的是这个仓库对外说了什么。
