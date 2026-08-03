# ADR-016：自研 ingestion 与 retrieval（已被 ADR-017 取代）

- 决策点：复核 ADR-003 与实际实现的差异
- 状态：**已被 [ADR-017](./0017-llamaindex-primary-rag.md) 取代**（2026-08-02）
- 日期：2026-08-01

> 本文保留为历史决策记录，不再代表当前技术路线。当前决定是由 LlamaIndex
> 承担 ingestion/retrieval 框架职责，并用 RAGAS 做离线生成质量评测；现有自研路径
> 只作为迁移期实现与对照基线，不能再写成项目最终框架口径。

## 背景：一条基线自己和自己矛盾

架构基线里有两句话，它们要求的是相反的事。

ADR-003 写着「LlamaIndex 只承担 RAG ingestion 与 retrieval」，后果是
「LlamaIndex Document、Node、Retriever 必须经过 Adapter」。

而 §15 Definition of Done 的最后一条写着：

> 能够清楚解释「**为什么没有**把 LlamaIndex、LangGraph、CrewAI 等框架层层嵌套」。

**现实执行的是第二句。** 从 WP04 到今天，ingestion 与 retrieval 全部是自研的：
`TextDocumentParser`、`Chunker`、`BgeM3Embedder`、`BgeM3SparseEncoder`、
`QdrantVectorIndex`、`RetrievalService`、`BgeReranker`。仓库里没有 `llama_index`
依赖、没有 Adapter、没有测试——架构守卫的 `FORBIDDEN_CORE_IMPORTS` 甚至把
`llama_index` 列为核心层禁止导入。

一个描述了「项目没有执行的决定」的 ADR，比缺一个 Adapter 更糟：读完 ADR 再读代码
的人看到的是自相矛盾，而这个仓库对外的全部价值是**每一条声明都有证据**。

## 决策

**记录自研胜出，并说明它买到了什么。** 不补 LlamaIndex Adapter。

### 为什么不补

补它只有两种形态，两种都更差：

**替换掉自研路径。** 现在这条路径是实测过的——38 题 gold set 上
MRR 0.960 / recall@1 0.947 / 61ms。用一个没有同等证据的实现换掉它，是拿掉证据
换一个框架名字。

**并存一条没人用的路径。** 那么能力表要写「LlamaIndex Adapter 已实现（默认不启用、
未评测）」。这正是这个仓库一直在拒绝的那种句子。

### 自研买到了什么（每条都是实际撞到的）

**一次融合，位置明确。** dense 与 sparse 的 RRF 只在 Qdrant Query API 里发生一次。
检索适配器映射融合结果、**不按相对分数二次排序**——融合两次会造出一个两个检索器都
没产生过的排序。要在一个 QueryEngine 抽象下保证「恰好一次、且在数据库里」，需要知道
它内部在哪一层做了什么。

**ACL 是双检的，且检查点由我们决定。** 向量库里存的是**上次索引时**的 ACL 副本，
只用来缩小候选；每个候选在成为上下文之前都要拿 PostgreSQL 重新核对，答案写作期间
来源失去可读性还要撤回。这个顺序（找 → 授权 → 构造）是 `RetrievalService` 的
方法文档第一句，也是它存在的理由。

**分块边界属于索引身份。** 哪个 tokenizer 数的 token 决定每一条边界落在哪，所以
counter 的名字进 `Chunker.identity`，再进每个 chunk id。两套分块共用一个名字意味着
重建索引会静默移动边界，而按旧偏移建立的引用会指向已经不在那里的文本。

**sparse 必须来自 FlagEmbedding。** 见 [ADR-013](./0013-bge-m3-sparse-encoder.md)：
sentence-transformers 的 `SparseEncoder` 会给没有声明 sparse head 的模型接一个
`SparseAutoEncoder`，对 BGE-M3 产出的是 4096 维的 dense 重编码，而不是模型真正拥有的
250002 维词项权重。这个区别是逐层拆到编码器实现才看清的。

### 有限借用的边界

不嵌套不等于不用。实际借用的是三样，每样都只在一个位置：

| 借用 | 位置 | 为什么是它 |
|---|---|---|
| **LangGraph** | Task 控制平面（ADR-002） | 图、条件边、fan-out/fan-in、interrupt 与 checkpoint 语义是真需求，自研等于重写一个工作流引擎 |
| **LangChain** | 仅 `langchain-core` 的工具契约 | `adapters/tools/langchain.py`，把一个 LangChain 工具变成普通 `ToolBinding`。**没有** executor、agent、chain |
| **Qdrant / pypdf / pydantic 等** | 各自一处 | 基础设施与格式，不是 Agent 抽象 |

LangGraph 的 checkpointer 仍是自研的（[ADR-014](./0014-own-postgres-checkpointer.md)），
因为官方那个带 LGPL 和第二个 PostgreSQL 驱动。**这是「有限借用」的具体含义：借用图
执行，不借用它对持久化的意见。**

## 后果

- ADR-003 标记为**已被取代**，基线 §17 能力表相应订正；
- 简历与 README 的口径是「**自研 + 有限借用**」，并给出上面这张表；
- `llama_index` 留在 `FORBIDDEN_CORE_IMPORTS` 里——它现在的含义从「Adapter 还没写」
  变成「核心层不接受这个依赖」；
- 如果将来要引入 LlamaIndex，**先要有一个它比自研强的评测结果**，而不是先有 Adapter。
  `scripts/run_rag_eval.py` 就是那个评测该跑的地方。

## 被否决的替代方案

**A. 悄悄删掉 ADR-003。** 一个作品集项目的价值在于推理过程可读，删掉一次走错的决定
等于把「我们想过这个」也一起删了。取代它并写明为什么，比它从没存在过更有说服力。

**B. 改写 ADR-003 原文，假装当初就是这么定的。** 同上，且更糟：基线 ADR-001～011 是
v1.3 成文时的边界，改动它们会让「基线」这个词失去意义。
