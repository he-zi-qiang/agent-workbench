# ADR-097：没有读者的漏斗不是漏斗

- 决策点：`[rag.retrieval]` 声明的候选漏斗五个数至今没有任何读者；是把它们接进检索路径，
  还是从 schema 里删掉；接线之后"请求只能在系统上限以内下调"这句话由谁执行
- 状态：**接受**，把漏斗接进检索路径，并让 `rerank_top_k` 成为请求可要的真实上限
- 日期：2026-08-31
- 影响：`application/retrieval.py` 的 `RetrievalService` 新增 `fused_top_k` 与
  `rerank_top_k` 两个可选字段；`adapters/retrieval/reference.py` 的
  `ReferenceVectorIndexRetriever` 新增 `dense_top_k` / `sparse_top_k`；
  `apps/api/dependencies.py` 与 `apps/task_worker/composition.py` 两处构造点从配置读入。
  **`CandidateRetrieverPort` 一个字都没动**（见 §3.2）。配置 schema、事件形状、
  `answer_context_k` 的处置**均不变**（见 §4.3）
- 依赖：[ADR-016](./0016-self-built-retrieval.md)（自研检索）、
  [ADR-033](./0033-fusion-ranks-are-ours.md)（融合在本进程内）、
  [ADR-017](./0017-llamaindex-primary-rag.md) 第 3 步（切换前要有能区分两条路径的度量）

---

## 1. 背景：一条被校验、然后没人读的漏斗

`config.default.toml` 的 `[rag.retrieval]` 写着五个数：

```toml
dense_top_k = 40 ; sparse_top_k = 40 ; fused_top_k = 40 ; rerank_top_k = 8 ; answer_context_k = 8
```

`settings.py` 的 `validate_candidate_funnel` 在启动时校验它们**彼此**单调，
`tests/config/test_settings.py` 还钉着这条校验。看起来这是一条被认真对待的配置。

**但五个数没有一个有读者。** `RetrievalService` 的两处构造点传的是
`candidate_retriever` / `documents` / `telemetry` / `reranker` /
`rerank_timeout_seconds`，一个 top_k 都没有。实际生效的是

```python
limit = request.top_k * self.candidate_multiplier   # 8 * 4 = 32
```

两个值都来自 dataclass 默认值。`answer_context_k` 看起来是例外——它被投影进
`RetrievalConfig`——但那个字段同样没有消费者。

这被登记为[已知缺口 A-07](../known-gaps.md)，分类**口径不实**。

## 2. 真正的问题不是"一个字段没人读"

一个没人读的布尔值是浪费；**一条没人读的漏斗是一句谎**，因为
[配置说明](../configuration.md) §8 用它做了一个承诺：

> 请求只允许在系统上限以内下调：…… dense/sparse/fused/rerank `top_k`

这句话描述的是一道闸。而约束请求的其实是两个**硬编码字面量**：
`routes/chat.py` 的 `le=50` 与 `adapters/tools/knowledge_search.py` 的
`MAX_TOP_K = 20`。**把 `rerank_top_k` 调到 1，一个请求照样可以要 50。**

所以 A-07 的两条出路不对称：删掉字段能消除谎言，但也消除了"部署可以收窄检索"这个能力；
接线则要求把 §8 那句话真的执行起来。**选接线，是因为那句话本来就该是真的。**

## 3. 决定

### 3.1 五个数各归各位

| 配置 | 落在哪 | 含义 |
|---|---|---|
| `dense_top_k` / `sparse_top_k` | `ReferenceVectorIndexRetriever` | 每一臂各自要多少 |
| `fused_top_k` | `RetrievalService` → Port 的 `limit` | 融合后交给授权的候选上限 |
| `rerank_top_k` | `RetrievalService` | **请求可要的真实上限**（§3.3） |
| `answer_context_k` | 不动，见 §4.3 | 它是默认值而不是上限 |

### 3.2 `CandidateRetrieverPort` 不动，这是有意的

Port 只有一个 `limit`。把它拆成 `dense_limit` / `sparse_limit` 会让**三个实现**
（reference、llama_index、seed_expansion）都得回答一个只有混合检索器才有的问题：
两臂各要多少。`reference.py` 现有注释已经写明这件事属于检索器内部——

> Each arm proposes a full candidate set; RRF is what narrows them to one.

所以两臂上限做成**检索器自己的构造参数**，Port 的 `limit` 继续表示"融合之后要多少"，
正好是 `fused_top_k`。**一个 Port 契约没有因为一次配置接线而变宽。**

**代价要写清楚：两臂上限只对 reference 那条路生效。** LlamaIndex 那条把
`sparse_top_k` / `hybrid_top_k` 一并传给 store，而 store **故意不用它们截短任何一臂**
（见其 docstring）。于是出厂值 `dense_top_k == sparse_top_k == fused_top_k == 40` 时
两条路要到的候选是同一批，[ADR-017](./0017-llamaindex-primary-rag.md) 第 3 步要的可比性
不受影响；**而一个把两臂配成不相等的部署，就把两条路变成了不可比的两个检索器**。
那不是本 ADR 的 bug，是那次配置的选择——但它应当在做 A-03 等价评测之前被排除，
否则评测比的是两个东西。

### 3.3 `rerank_top_k` 是请求的上限，不是建议

`retrieve()` 里把 `request.top_k` 夹到 `rerank_top_k`：请求可以要更少，不能要更多。
这不是新政策，是 §8 那句话第一次被执行。硬编码的 `le=50` 与 `MAX_TOP_K = 20` **保留**
——它们是传输层对畸形请求的防线，与部署级上限是两件事，且删掉其中任何一个都会让
"配置没配"的进程失去下界。

### 3.4 未配置时保持旧行为

四个字段都是 `int | None`，`None` 时沿用 `request.top_k * candidate_multiplier` 与
不夹取。**内存 double、契约测试与既有 7 处测试构造点因此一行都不用改**，而生产两处
构造点显式从配置读入。这不是骑墙：它把"配置没说"和"配置说了 0"分开，而后者应当是
校验错误而不是一次静默的空检索。

## 4. 后果

### 4.1 候选池不再随请求伸缩，而这在两个方向上都成立

此前候选池是 `request.top_k × 4`，此后是 `fused_top_k`。**默认请求（`top_k = 8`）
是 32 → 40**，但这不是"一个方向的小改动"，说成那样会漏掉一半：

| `request.top_k` | 此前问索引要 | 此后 | 方向 |
|---:|---:|---:|---|
| 3 | 12 | 40 | 变宽 |
| 8（默认） | 32 | 40 | 变宽 |
| 50（路由上限） | 200 | 40 | **大幅变窄** |

小请求变宽、大请求变窄，因为**"要多少候选"从此是部署的决定而不是调用方的**。
最后一行看着吓人，但它与 §4.2 是同一件事的两面：`top_k = 50` 的请求此后只会被展示
`rerank_top_k = 8` 条，为它取 200 条候选本来就是在为一个不会被展示的数付钱。

选出的条数与答案上下文在默认路径上**不变**。但候选集变了，**检索结果就可能变**，
因此见 §5。

### 4.2 `top_k > 8` 的请求现在会被夹到 8

这是本 ADR 唯一一处**收窄**用户可见行为的地方，也是它的全部意义。此前一个请求可以
要 50 条而部署无从阻止；此后 `rerank_top_k` 说了算。**这会让某些既有调用拿到更少的
结果**，而那正是 §8 一直声称的行为。

**它带出一处新的、更小的同形不诚实，必须记下来而不是让它自己长大。**
`knowledge_search` 的 `INPUT_SCHEMA` 向模型声明 `top_k` 的 `maximum` 是
`MAX_TOP_K = 20`，而出厂 `rerank_top_k = 8`。于是模型可以**合法地**要 20 条，
拿回 8 条，而没有任何地方告诉它为什么。这与 A-07 是同一种形状——一个数字声称的比
系统给的多——只是从配置搬到了工具契约上。

本 ADR **不顺手修它**，理由是修法不止一种而每一种都不小：把上限改成从配置生成，
会让工具目录随部署而变（而 MCP 目录是进程启动时冻结的）；在结果里说明被截到几条，
是给模型多一个字段；把 `MAX_TOP_K` 直接降到 8，则把两个本该分开的边界又焊在一起。
选哪条要看它在多智能体场景里怎么表现，那不是本 ADR 的题目。**登记在
[已知缺口](../known-gaps.md) A-07 的剩余部分里。**

### 4.3 `answer_context_k` 仍然没有读者，A-07 因此只关一半

它是**默认值**而不是上限：决定"没指定时给多少"的地方是 API 的
`Field(default=8)` 与 `RetrievalRequest.top_k = 8`，都在请求构造的边界上，不在
检索服务里。把默认值改成配置驱动要动的是 FastAPI 模型的类定义时求值，属于另一条边界，
不顺手，也不该塞进本 ADR。**A-07 保持开着，收窄为"只剩 `answer_context_k`"。**

## 5. 重审条件

**本 ADR 没有拿到检索质量证据，这一点必须写在这里而不是别处。** §4.1 那个 32 → 40
改变了返回的候选集，而本次落地的机器上 `sentence_transformers` 与 `FlagEmbedding`
都不在（`embedding` extra 未装），52 题 gold set 跑不起来。

因此：**在 [A-03](../known-gaps.md) 的等价评测于同一份 52 题 gold set 上重跑之前，
不应把本 ADR 当作"检索质量已确认不变"的依据。** 已有的确定性测试证明的是**机制**
（改配置能改变候选上限、请求会被夹到 `rerank_top_k`），不是**效果**。

若重跑显示 40 与 32 的差异超出噪声底，正确的动作是改 `config.default.toml` 里的数，
而不是把接线撤掉——那时配置终于是一个可以据以调整的旋钮，这正是本 ADR 想要的结果。
