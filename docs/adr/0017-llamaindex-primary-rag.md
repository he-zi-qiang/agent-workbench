# ADR-017：LlamaIndex 是主 RAG 框架，RAGAS 是离线评测基线

- 决策点：恢复作品集基线中的 RAG 技术路线
- 状态：**接受**，取代 [ADR-016](./0016-self-built-retrieval.md)，重新确认 ADR-003
- 日期：2026-08-02

## 背景

ADR-016 根据当时已经完成的代码，把“当前自研实现”提升成了“最终架构决定”。这混淆了
两个不同问题：当前代码怎样工作，以及这个校招作品集想展示怎样的框架集成能力。

项目目标一直包含 LlamaIndex、LangGraph 与自研 Runtime 的清晰分工，也需要 RAGAS
覆盖生成质量评测。是否保留一条可运行的自研实现，不应反过来取消已经明确的目标。

## 决策

**LlamaIndex 是 Chat/RAG 的主 ingestion 与 retrieval 框架。** 它负责 Document/Node
映射、ingestion pipeline 和 Retriever Adapter；不使用其 Agent executor 或
QueryEngine 接管最终回答。核心 Runtime 仍只消费框架无关的 `ContextPacket`、
`Citation` 和 Tool 协议。

**Qdrant 仍是唯一 hybrid fusion owner。** BGE-M3 dense/sparse 进入 Qdrant Query API
做一次 RRF；LlamaIndex Adapter 只负责调用与结果映射，不做第二次分数融合。

**授权与答案发布仍由应用层控制。** PostgreSQL ACL 复核、source revision fence、
`AnswerCommitted/AnswerWithheld` 都是安全边界，不委托给检索框架。这层 policy facade
不是另一套自研 retriever，不能在简历中表述为“自研 Retrieval 替代 LlamaIndex”。

**RAGAS 作为离线 LLM-judge 辅助。** 它覆盖 faithfulness、answer relevance 等生成
质量指标；带 relevant IDs 的 gold set 继续负责 Recall@K、MRR、citation
precision/recall 等确定性指标。RAGAS 不进入在线请求，也不成为 CI 的联网依赖。

## 增量迁移规则

1. 先增加 `llama_index` Adapter 与 contract tests，保持 `/v1/search`、Chat citation、
   ACL 和发布门外部契约不变；
2. 用相同 gold set 对当前路径与 LlamaIndex 路径做等价/差异评测；
3. 默认流量切到 LlamaIndex 后，现有 parser/retrieval 实现降为明确命名的 reference
   adapter 或删除，不能长期保留两条都自称 production 的路径；
4. 接入 RAGAS 离线 runner，固定 judge provider、model revision、prompt、temperature
   和数据集 revision，并记录与人工评分的一致性；
5. 在上述证据落地前，能力表只能写 Planned，不能写 Implemented。

## 后果

- ADR-003 恢复为有效边界，ADR-016 保留但标为已取代；
- `llama_index` 继续禁止进入 domain/runtime 核心层，但允许且要求出现在 adapters 层；
- 简历叙事是“自研 Agent Runtime + LangGraph Task 控制面 + LlamaIndex RAG + RAGAS
  离线评测”，不是“所有层都自研”；
- 现有自研 RAG 代码和成绩仍可作为迁移基准，但不能代替 LlamaIndex/RAGAS 的实现证据。
