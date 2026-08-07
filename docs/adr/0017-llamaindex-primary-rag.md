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

### 迁移进度（2026-08-03 更新）

| 步骤 | 状态 | 证据 |
|---|---|---|
| 1 检索 Adapter + contract tests | **完成（仅检索）** | `adapters/llama_index/`；`tests/vector/test_authorized_retrieval.py` 按 `CandidateRetrieverPort` 参数化，两条路径跑同一套 ACL/revision/引用断言 |
| 2 同 gold set 等价评测 | **已执行，未通过——测量分辨不了** | dense 臂两条路径逐位相同；hybrid 臂**无法判定**：并列融合分数次序不稳定，每个检索器与自己不一致 9-10/38 题，宽于路径间差异。报告见 `evals/rag/reports/<arm>-<path>.json` |
| 3 默认流量切换 + reference 降级 | **未做，且被第 2 条挡住** | `rag.llama_index.enabled = false`；reference 仍是默认路径 |
| 4 RAGAS 离线 runner | 未开始 | — |
| 5 能力表口径 | **整体仍是 Planned** | 适配器存在不等于框架集成完成；见 README 能力边界 |

**第 2 条挡住第 3 条，这正是这些规则存在的理由。** 结论不是"两条路径不一致"，而是
"这套测量装置分辨不了它们"——并列名次没有定义好的次序，于是同一个检索器重复同一个查询
都会给出不同排列。要让第 2 条能给出结论，必须先让并列项有确定性次序（按
`(-score, chunk_id)` 在适配器边界定序），那是一项独立的行为变化。在那之前，
第 3 条不能靠"看起来差不多"推进。

**步骤 1 只覆盖了检索。** ADR 的决策段把 ingestion 和 retrieval 一起交给 LlamaIndex，
本轮只做了后者：Document/Node 映射与 Retriever Adapter 已经存在，
`IngestionPipeline` 没有。理由是可测量性——检索换框架可以用同一个索引、同一份 gold set
对照，ingestion 换框架会改变索引里的内容，两侧再没有共同的基准。因此
`PortBackedVectorStore` 的 `add`/`delete` 明确拒绝：一条没有对照的第二写入路径，正是本
ADR 第 3 条要防的东西。

**`fusion_enabled` 等三个 `Literal[False]` 字段没有进投影。** 它们是单值类型，进程侧
检查只能拿常量和自己比。真正约束它们的是结构：架构守卫拒绝任何模块 import LlamaIndex
的 agent/QueryEngine 机制（也拒绝 `.as_query_engine()` 这种不需要新 import 的调用），
而"不做第二次融合"由 adapter contract test 钉住索引返回的次序——重排正是第二次融合从
外部看到的样子。

## 后果

- ADR-003 恢复为有效边界，ADR-016 保留但标为已取代；
- `llama_index` 继续禁止进入 domain/runtime 核心层，但允许且要求出现在 adapters 层；
- 简历叙事是“自研 Agent Runtime + LangGraph Task 控制面 + LlamaIndex RAG + RAGAS
  离线评测”，不是“所有层都自研”；
- 现有自研 RAG 代码和成绩仍可作为迁移基准，但不能代替 LlamaIndex/RAGAS 的实现证据。
