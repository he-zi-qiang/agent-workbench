# 两条 Chat 路径的对照评测（5.4）

- 日期：2026-08-01
- 脚本：[`scripts/run_chat_eval.py`](../../scripts/run_chat_eval.py)
- 题目：[`gold.jsonl`](./gold.jsonl)（13 题，**先写题后跑数**）
- 原始数据：[`reports/chat-hybrid-180s.json`](./reports/chat-hybrid-180s.json)

> **2026-08-31：这份报告答的是一份已经改过的 gold set，不要拿它和以后的跑比。**
> 三道题（`single-fusion`、`compound-fuse-acl`、`compound-rrf-where`）的
> `must_contain` 当时是 `"qdrant"`——而 [ADR-033](../../docs/adr/0033-fusion-ranks-are-ours.md)
> 早已把融合搬进本进程。也就是说**这份报告里那几道题的满分，奖励的是一句关于本系统的
> 错误陈述**：转录里模型答的原话是 "Qdrant's Query API performs the hybrid fusion"，
> 它照着语料答对了，而语料是错的。
>
> 语料（`evals/rag/corpus/fusion.md`、`abbreviations.md`）与这三道题的期望
> 已于 2026-08-31 一并更正，并新增
> `tests/evaluation/test_corpus_agrees_with_the_system.py` 让这条漂移不会再长回来。
> **这份报告与它的原始数据原样保留**：它记录的是一次真实测量，而一次测量不会因为
> 被测的题目后来改了就变成假的——它只是不再和以后的跑可比。

待办清单 5.4 的原话是「fixed 与 agentic 都在了，**没有任何东西测过第二条到底买到
了什么**」。本轮把它测了，结论分三层：先是两个**此前没人发现的缺陷**，然后才是那个
对照数字。

---

## 一、agentic 路径此前**从来没有检索到过任何东西**

`knowledge_search` 的 `INPUT_SCHEMA` 把 `knowledge_base_id` 列为 **required**，并且
从**模型自己的参数**里取它：

```python
knowledge_base_id = str(arguments.get("knowledge_base_id", ""))
```

而 `AGENTIC_SYSTEM_PROMPT`、`build_agentic_request` 送出的 user message、
`_assemble_chat` 的装配——**没有任何一处告诉模型这个 id 是什么**。

于是模型只能编。实测（评测把模型实际传的参数记了下来）：

```json
{"query": "hybrid fusion",           "knowledge_base_id": "default"}
{"query": "hybrid fusion performs",  "knowledge_base_id": "default"}
{"query": "fusion",                  "knowledge_base_id": "default"}
```

语料在 `kb_eval`。每一次检索都命中一个不存在的知识库，返回
`{"chunks": [], "note": "no readable passages matched"}`，而且**状态是 ok**——因为
「那里没有」和「对你没有」在这个系统里被有意设计成同一个答案。模型于是老老实实说
「我搜了，没搜到」。

**它看起来像一个搜过了但没找到的模型，实际上是一条从未接通的链路。**

这也修正了清单 1.3 的结论。那一轮观察到 4 次 `ToolFailed: 30s timeout`，归因于机器
太慢；把超时放宽之后，失败换了一种形式，而这一种和硬件无关。

**已修**：`agentic_system_prompt(knowledge_base_id)` 把这一个事实写进系统提示。
它**不是**权限——`knowledge_base_id` 只缩小范围，能读什么仍由 PostgreSQL 逐条判定；
tenant 与 principal 继续不经过模型，因为那两个**才是**权限。补了两条测试：提示里必须
出现本轮的 kb id，且换一个 kb 时提示跟着换。

## 二、`knowledge_search` 返回不了一个正常大小的结果（**未修**）

工具把检索到的段落渲染进 `ToolResult.content`，而 `content` 是
`BoundedText = max_length 4096`。本项目自己的分块是 512 token（约 2000 字符），工具
自己的默认 `top_k` 是 8。确定性复现：

| top_k | 每块字符 | 渲染后 | 结果 |
|---|---|---|---|
| 3 | 400 | 1,482 | ok |
| 3 | 1200 | 3,882 | ok |
| 3 | **2000** | 6,282 | **拒绝** |
| 8 | 1200 | 10,332 | **拒绝** |
| 8 | **2000** | **16,732** | **拒绝** |

超限不是截断，是**整次调用失败**。后果是：一次搜索能不能活下来，取决于命中的段落
**碰巧多长**——评测里 `single-acl` 与 `single-code` 就是这样连续失败到耗尽步数预算的，
其中一题以 `stop_reason=max_steps` 收场、答案为空。模型自己的措辞是「the knowledge
search tool is failing with a validation error on every attempt」。

固定两步路径撞不到这一条：它把证据放进 prompt 的 `context`，不经过 `ToolResult`。

**评测那一轮没有修**——改工具返回值的形状是一次独立的行为变化，不该搭评测的车。
下面的 agentic 数字是**带着这个缺陷**测出来的，两题的失败要算在它头上，不算在
「agentic 这个形态」头上。**修复见文末第四节**，数字尚未重跑。

---

## 三、对照数字

修完缺陷一、带着缺陷二，同一套语料 / 同一个 retriever / 同一个模型（temperature 0）/
同一个 `top_k=3`：

| | fixed | agentic |
|---|---|---|
| 完整作答（11 题计分） | **11 / 11** | 9 / 11 |
| fact recall | **1.000** | 0.818 |
| citation precision | 0.955 | **1.000** |
| citation recall | **0.955** | 0.818 |
| 编造引用 | 1 | **0** |
| 该拒答时拒答 | 2/2 | 2/2 |
| 平均检索次数 | 1.0 | 1.0 |
| 平均模型调用 | **1.0** | 2.7 |
| **平均 token** | **472** | **3,247（6.9×）** |
| 平均耗时 | **35.0s** | 45.4s |

分题型：

| 题型 | fixed 完整 / citation recall | agentic 完整 / citation recall |
|---|---|---|
| single（4） | 4/4 · 1.000 | 2/4 · 0.500 ← 两题死于缺陷二 |
| compound（4） | 4/4 · 0.875 | **4/4 · 1.000** |
| vocabulary（3） | 3/3 · 1.000 | 3/3 · 1.000 |

### 读法

**agentic 在这套题上没有买到准确率，买到了 6.9 倍的 token。** 它唯一测得出来的优势
是两处，都很窄：

- **compound 题的 citation recall 更高**（1.000 vs 0.875）。这正是它该赢的地方——两个
  事实分在两篇文档里，固定路径只有一次 top_k=3 的机会，模型可以搜两次。但**两条路的
  fact recall 都是 1.000**：固定路径答对了内容，只是少引一篇。
- **零编造引用**（固定路径有 1 处）。样本太小，不足以称为规律。

vocabulary 题型**没有分出差别**，尽管它就是为「模型可以改写查询」设计的。诚实的说法
是：这套 10 篇文档的语料太小，hybrid 检索在原始问句上已经够好，改写没有空间可赚。

### 这套评测测不到什么

- **只有 13 题**，且 compound 只有 4 题。上面每一个差值都在能被一两题翻盘的范围内。
- **判分是确定性的关键词包含**，不是 LLM judge（ADR-006、§12.4）。它不会争议，但
  「答对了」被近似成了「答案里有这些词」。词取自语料原文，不取自任何一轮输出。
- **没跑 reranker**。三套权重同时驻留约需 12 GB，这台 8 GB 机器会换页，换页时测出来
  的是主机不是形态。两条臂共用同一个 retriever，所以对照仍然成立，但它描述的是
  hybrid 之上的两个形态，不是 hybrid+rerank 之上的。报告文件名与 JSON 里都写了。
- **`--tool-timeout-seconds 180`**，不是出厂的 30。出厂值在这台机器上会让每次检索都
  超时，那测的是主机。

---

## 复现

```bash
AGENT_WORKBENCH_TEST_QDRANT_URL=http://localhost:6333 \
AW_SECRETS__DEEPSEEK_API_KEY=sk-… \
uv run --extra embedding python scripts/run_chat_eval.py \
  --no-reranker --tool-timeout-seconds 180
```

CI 不跑它：没有 embedding 运行时，也不调用任何 provider。


---

## 四、缺陷二的修复（2026-08-01）

**根因是一个类型把两件不同的事混成了一件。** `ToolResult.content` 用的是
`BoundedText`(4096)，而 `BoundedText` 其余每一处用途——`ModelDelta.text`、
`ModelCompleted.text`、`AnswerCommitted.text`、`ChatTurn.answer`、
`AgentOutcome.output_text`——都是**模型自己写出来的话**。4096 对那些是宽裕的界。
工具结果是**喂给模型的输入**，它的自然尺寸是它被要求取回的证据量。共用一个类型，
使得一次正常检索的结果**根本构造不出来**——不是被截断，是被拒绝。

顺带确认了这个界**不是**在保护事件日志：`ToolCompleted` 只带 `output_bytes`、
`artifact` 和 `truncated`，正文从不入库。

修法两部分：

1. **`ToolOutputText`**（65,536）：工具交给模型的东西有了自己的上限。它是**兜底**，
   不是操作性限制。
2. **`MAX_CONTENT_CHARS = 48,000`**：`knowledge_search` 自己的预算，装得下
   `MAX_TOP_K=20` 篇 512-token 分块（实测 41,822 字符）。超预算时**整段丢弃、
   从低分端开始**，绝不截断——半段证据说的是文档没说的话，而引用栅栏只校验
   「这个 chunk 被展示过」，不校验模型倚赖的那句话有没有活过那一刀。**丢弃会丢证据，
   截断会造证据。** 丢了几段会写进返回值的 `note`：知道自己证据不全的模型可以换个更窄的
   查询再搜一次，不知道的会当成拿全了照答。

| top_k | 每块字符 | 渲染后 | 修前 | 修后 |
|---|---|---|---|---|
| 3 | 2000 | 6,282 | 拒绝 | ok |
| 8 | 2000 | 16,732 | 拒绝 | ok |
| **20** | **2000** | **41,822** | 拒绝 | **ok** |

`ChunkText` 自身上限是 32,768，低于本预算，所以「连第一段都装不下」这条分支**不可达**；
它保留着并标了 `pragma: no cover`，有一条测试钉住两个常量的大小关系——把预算调到
分块上限以下会让那条分支悄悄复活，那条测试会先红。

破坏验证 8 处，全部被抓住（第一次写的第 1 处是我自己的无效破坏——删了类型没删 import，
炸在收集期而不是行为上，改对后同样被抓）。
