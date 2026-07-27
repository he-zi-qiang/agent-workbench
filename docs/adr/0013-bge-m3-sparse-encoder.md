# ADR-013：BGE-M3 的 sparse 必须来自 FlagEmbedding

- 决策点：WP05-01 SparseEncoderPort
- 状态：**接受**
- 日期：2026-07-27

## 背景

架构基线 8.1 写过一句警告：

> 普通 `HuggingFaceEmbedding` 不能自动得到 BGE-M3 sparse lexical weights，
> 因此必须实现一个常驻 `BgeM3EncoderAdapter`，同批次输出 1024 维 dense vector
> 与 sparse `indices/values`。

实现 WP05-01 时，`sentence-transformers` 5.x 已经提供了 `SparseEncoder`，看起来
可以直接用——项目已经依赖它，不必再引一个库。**这条捷径是错的**，而且错得很安静。

## 证据

```text
BGE-M3 词表大小:        250002
SparseEncoder 输出维度:   4096
模块链:  Transformer → Pooling → Normalize → SparseAutoEncoder
```

BGE-M3 的 sparse 表示是**词表上的 lexical weights**：每个 token 一个权重，维度等于
词表大小。而 `SparseEncoder` 在模型卡未声明稀疏头时，会接一个 `SparseAutoEncoder`——
把 1024 维 dense 向量压缩成 4096 维稀疏码。

**那是对 dense 向量的一次有损重编码，不是词汇匹配。**

## 为什么这个错误危险

它不会报错。Qdrant 存得下 4096 维稀疏向量，RRF 融得了两路结果，评测也会输出一个
数字。整条链路「能跑」。

失败方式是：那条 sparse 支路**根本不做 term matching**。混合检索相对纯 dense 拿不到
它应有的收益，而现象只是「hybrid 好像没什么用」——一个会被归因为「这个语料上 dense
已经够好了」的结论，而不是被归因为「sparse 那一路是假的」。

一个能跑、能出数、且结论看起来合理的错误实现，比一个崩溃的实现难发现得多。

## 决策

**BGE-M3 的 sparse 只能来自 `FlagEmbedding`**，即 BGE 官方库。它在一次前向里同时
输出 dense、sparse lexical weights 与 colbert 向量，文档与模型是同一批作者维护的。

加入既有的 `embedding` 可选依赖组，与 dense 同一档：CI 不装，真实数字来自本地运行。

## 代价

- `FlagEmbedding` 的维护活跃度与接口稳定性都不如 `sentence-transformers`；
- 两个库会同时出现在可选组里。dense 目前走 `sentence-transformers`，
  sparse 走 `FlagEmbedding`——**这是一个已知的重复，不是设计**。若
  `FlagEmbedding` 的 dense 输出被验证与现有结果一致，dense 应当合并过去，
  由一次前向同时产出两种表示（基线要求的正是这个）。在验证之前不合并：
  换掉 embedder 会改变 index identity，那是一次全量重建索引。

## 怎么验证它没被绕过

sparse adapter 的契约测试必须断言**输出维度等于分词器词表大小**。这一条只有真权重
在场时才跑得了，但它是唯一能区分「真 lexical weights」与「某个稀疏头」的断言——
维度对不上，就说明接的不是 BGE-M3 的 sparse。
