# ADR-037：图谱只提名 chunk，不合并成一张图

- 决策点：跨文档检索怎么做；港大 LightRAG / RAG-Anything 那种「LLM 抽实体关系、
  跨文档合并成一张知识图谱」要不要照搬
- 状态：**接受**
- 日期：2026-08-10
- 影响：新增 `kg_entities` / `kg_mentions` / `kg_relations` 三张表与两个 Qdrant
  collection；检索多两条臂，与 dense/sparse 在**同一次** RRF 里融合（复用 B1 下沉
  的 `adapters/vector/fusion.py`）。`RetrievalService` 的授权、rerank、引用、
  revision fence **一行不改**
- 依赖：ADR-016（找 → 授权 → 构造的顺序）、ADR-017（框架边界）、ADR-033（融合归我们、
  臂内名次归我们）

## 1. 背景：这次有失败可指

图谱臂此前没有测量依据。38 题金集上 hybrid 已经 MRR 1.000，量不出任何改进——
在那个语料上建图，是拿一个没有对照的实现换一个框架名字，正是 ADR-016 拒绝过的事。

2026-08-10 的证伪跑出了依据（`evals/rag/reports/ABLATION.md`）。语料扩到 49 篇、
14 道**桥接实体必须被发现**的多跳题（题面只给 A 文档的词，答案文档只能靠 A 里的
团队名或集群名到达），生产路径上：

| 指标 | dense | hybrid |
|---|---|---|
| recall@1 | 0.7885 | **0.8846** |
| recall@3 | 0.9423 | **0.9615** |
| MRR | 0.8558 | **0.9199** |
| **full_coverage@3** | **0.8462** | 0.8269 |

**hybrid 在三个单命中指标上全面更好，唯独"答案到齐"更差。** 单文档题的
`coverage_rank` 恒等于 `rank`，所以「coverage 失败 − recall 失败」精确等于只捞回
一半答案的题数：dense 5 道，**hybrid 7 道——14 道多跳题的一半**。

这个反转本身就是本 ADR 的论据：**更强的词面匹配让"答案到齐"更糟**。该加的不是
更强的 lexical 臂，是能沿实体跳过去的臂。

## 2. 决策

### 2.1 图谱是提名器，不是知识表示

抽出来的实体与关系**不回答问题**，它们只做一件事：**提名 chunk**。

```
query --embed--> 实体名 collection --> kg_mentions --> (document_id, version, chunk_id)
              \-> 关系描述 collection --> kg_relations --> 同上
```

到达 `RetrievalService` 的仍然是一组 `ScoredChunk`，与 dense/sparse 臂**同一类型**。
于是 ADR-016 那条"找 → 授权 → 构造"的顺序原封不动：图谱臂只影响"找"，
授权、rerank、截断、引用、答案提交前的 revision 复核全都不知道它存在。

**没有任何答案由图谱生成。** 这与 ADR-017 拒绝 LlamaIndex QueryEngine 是同一条线：
文本到达读者的路径只有一条，且必须经过发布闸门。

### 2.2 为什么不合并成一张图——这是与港大方案的分界

LightRAG / RAG-Anything 把跨文档的同名实体**合并成同一个节点**。那正是图能保持小、
能做全局摘要的原因，也是我们不能照抄的原因：

**合并之后，"这条知识来自哪份文档"就没有了。** 而我们整条授权链建立在它之上——
向量库里存的 ACL 只是上次索引时的副本，每个候选在成为上下文之前都要拿 PostgreSQL
按 `document_id` 重新核对（`application/retrieval.py` 的第一句话）。一个由文档 A 和
文档 B 共同构成的实体节点，无法回答"只能读 A 的人能不能看这一条"。

所以本项目的实体**在 KB 内按（规范名 + 类型）合并以获得跨文档连接**，但每一条知识
经由 `kg_mentions` 逐条锚回具体的 `(document_id, document_version, chunk_id)`。
合并的是**索引入口**，不是**证据**。检索时提名的永远是 chunk，授权永远按 chunk 的
文档走。

这不是"我们做不到他们那样"，是**两种不同的取舍**：他们买到了全局摘要能力，
代价是没有 per-principal 的授权落脚点；我们买到了授权，代价是回答不了
"整个语料的主题是什么"这类问题。后者本项目从未承诺过。

### 2.3 融合仍然只有一次，且是同一次

四条臂——dense、sparse、entity、relation——进**同一个** `fused()`，一次 RRF。
不是"hybrid 融合一次、再和图谱结果融合一次"：那会造出一个没有任何检索器产生过的
排序，正是 `VectorIndexPort` 用文字禁止的事。

B1 已经把 `ranked`/`fused`/`RRF_K` 从 Qdrant 适配器下沉到 `adapters/vector/fusion.py`，
并把裸的 sparse 臂开成公开方法（`search_hybrid` 返回的是**已经融合过的**列表，
从它出发再融合就是融合两次）。`fused` 本来就是变参的——两条臂是 hybrid 的形状，
不是 RRF 的形状。

`k` 仍取 2，理由与 ADR-033 同：换 k 会改变每一个融合分数，把评测基线悄悄作废。

### 2.4 查询侧不做 LLM 关键词抽取（v1）

LightRAG 每个 query 先让模型抽一遍关键词。本版**不做**：那是每次检索多一次模型
调用、多一处不确定性、多一个无法离线复现的环节。先用纯 embedding 的双层
（实体名 / 关系描述）量出收益，值不值得再说。

### 2.5 身份纪律：图谱有自己的 identity，不符就静默停用

`graph_identity = 抽取模型 + prompt 版本 + embedder.identity`，写进每一行实体与关系。
与 `index_identity` 同一套哲学（`application/ingestion.py`）：换了抽取模型的图谱和
旧图谱不是同一个东西，混在一起提名会让一次重建索引静默改变检索结果。

不匹配时图谱臂**停用**而不是报错——检索必须继续可用，少两条臂是降级，不是故障。
降级要被记录（`mode` 报 `hybrid` 而不是 `hybrid+graph`），否则一次消融会把
"图谱没跑"读成"图谱没用"。

### 2.6 抽取挂在 ingestion 的第二遍，不挤占第一遍

向量索引提交后，在写 `last_applied_revision` 的**同一个事务**里投一条
`graph_extraction_requested` 进 outbox；抽取作为同一个 worker drain 循环的新
kind 分支执行，**免费获得**现有的 claim / lease / heartbeat / 重试语义
（`ports/outbox.py`）。

不直接在第一遍里做，理由是失败域：抽取要调模型，慢且会失败，而
`last_applied_revision` 之前的那段持有文档锁。一个模型超时不应该让文档索引本身
回滚。

## 3. 后果

- 跨文档题第一次有机会被答全，而这件事有 B0 的基线可比；
- **代价一：索引成本**。每个 chunk 一次 LLM 抽取，成本随语料 × 模型走，且**不确定**
  ——同一份语料抽两次不会得到同一张图。这是与 chunk id 可复现（`ingestion.py`）
  相反的性质，必须写在能力表里，不能含糊成"图谱增强"；
- **代价二：两个新 collection 与三张新表要一起维护**，且它们的生命周期必须跟着
  文档版本走——文档删除时 mention 必须跟着走，否则图谱会提名已经不存在的 chunk；
- **不解决**：全局摘要、跨 KB 的图、查询侧的实体消歧、多跳超过一跳的推理。

## 4. 备选方案

**照搬合并知识图谱（LightRAG / RAG-Anything）。** 见 §2.2：合并擦掉了 per-document
的授权落脚点，而那是本项目的脊椎。

**只加一条"实体"臂，不要关系臂。** 更省事，但 B0 的失败里有相当一部分是关系型的
（"X 写入的存储保留多久"要的是 X→存储这条边）。先都做，让消融说哪条臂在起作用。

**把跨文档问题交给 rerank。** reranker 只能对已经召回的候选重排——桥接文档根本
没进候选集，重排够不着它。这正是 `full_coverage@3` 而不是 MRR 在下降的原因。

**加大 top_k。** 能提高 coverage，但把无关文档一起塞进上下文，且 ADR-035 之后
答案长度上界是真实约束。这是用上下文预算换召回，不是解决定位问题。
