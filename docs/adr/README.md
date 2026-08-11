# 决策记录

架构基线第 14 节的 ADR-001～011 定义了基线本身：它们是 v1.3 成文时就已经确定
的边界。这个目录放的是**实施过程中**做出的决定——计划里排期的决策检查点，以及
任何改变事实源、控制平面、Runtime owner、fusion owner 或恢复语义的选择。

两者编号连续，不重开一套。基线 ADR 留在基线里，因为它们是基线的组成部分；
新决定放在这里，因为它们各自有自己的触发时机和重审条件。

| ADR | 决策点 | 状态 |
|---|---|---|
| [ADR-012 身份边界](./0012-identity-boundary.md) | D0（WP04 前） | 接受 |
| [ADR-013 BGE-M3 sparse 必须来自 FlagEmbedding](./0013-bge-m3-sparse-encoder.md) | WP05-01 SparseEncoderPort | 接受 |
| [ADR-014 自研 PostgreSQL checkpointer](./0014-own-postgres-checkpointer.md) | WP06-06 checkpointer | 接受 |
| [ADR-015 唯一写节点的授权上限](./0015-export-authorization.md) | WP10-07 `export_artifact` | 接受 |
| [ADR-016 自研 ingestion 与 retrieval](./0016-self-built-retrieval.md) | 复核 ADR-003 与实现的差异 | 已被 ADR-017 取代 |
| [ADR-017 LlamaIndex 主 RAG + RAGAS 离线评测](./0017-llamaindex-primary-rag.md) | 恢复作品集 RAG 技术路线 | 接受，重新确认 ADR-003 |
| [ADR-018 无接地对话是显式形态](./0018-ungrounded-chat-shape.md) | Chat 交互形态；`chat.retrieval_shape` 的取值集合 | 接受 |
| [ADR-019 提示词与工具参数记进事件流](./0019-run-step-transparency.md) | 运行步骤的可观察内容；`runtime.record_step_inputs` 的引入 | 接受 |
| [ADR-020 DeepSeek `web_search` 接上外部检索](./0020-external-web-search.md) | `ExternalSearchPort` 的真实实现；Task 授权信封是否放行 `external_search` | 接受 |
| [ADR-021 Chat 的联网搜索只出现在兜底分支](./0021-chat-web-search.md) | Chat 要不要联网；"要不要联网"由谁判断；用了网页的回答算不算接地 | 接受 |
| [ADR-022 工具额度用尽是收走工具](./0022-tool-ceiling-closes-the-toolbox.md) | `max_tool_calls` 用尽时 run 应该怎么办；`max_tool_calls < max_steps` 是不是配置错误 | 接受 |
| [ADR-023 无接地作答只有一个实现](./0023-direct-chat-reaches-the-web.md) | `direct` 形态能不能联网；"无证据作答"由几份代码实现 | 接受，扩展 ADR-021 并消耗 ADR-018 的重审条件 |
| [ADR-024 一个 Worker 进程可以同时跑多个 Task](./0024-task-worker-lanes.md) | Task 并发执行的拦路石是什么；`worker_concurrency` 还该不该被钉死在 1 | 接受 |
| [ADR-025 MCP 工具在启动时冻结成本地绑定](./0025-mcp-adapter.md) | `optional_labs.mcp_adapter` 的真实实现；第三方 schema 不合规时是放宽校验还是丢掉工具 | 接受 |
| [ADR-026 Word 文档是 MCP 返回的不可变 Artifact](./0026-word-docx-is-an-mcp-artifact.md) | 项目自有 Word Server 如何保留 Gateway、scope、事件和 Artifact 所有权边界 | 接受（本地 Optional Lab） |
| [ADR-027 只读取外部世界，写只写进自己的 artifact](./0027-read-outward-write-inward.md) | Task 能不能自己取页面、下载文件、生成 Office 文档；这三件事的共同边界 | 接受 |
| [ADR-028 任务工作区是可变的名字压在不可变的字节上](./0028-task-workspace.md) | Agent 能不能在一个 Task 内积累并加工产物；可变状态与"节点整体重放"怎么共存 | 接受 |
| [ADR-029 沙箱是纯函数，断网是它保持纯的原因](./0029-ephemeral-sandbox.md) | Agent 能不能跑代码；跑代码怎么不把前面几条 ADR 的重放保证作废 | 接受 |
| [ADR-030 会干活的节点由成本和时限管](./0030-working-nodes-are-governed-by-cost.md) | 带工具迭代的 run 该由什么约束；`max_steps` 域上限 100 还合不合适；整文件覆写够不够 | 接受 |
| [ADR-031 通用任务走第二张图](./0031-a-second-graph.md) | 不是"写调研报告"的任务该走什么形状；模型能不能决定自己的步骤顺序 | 接受 |
| [ADR-032 外部研究节点在拿到工具时是一个 Agent](./0032-the-external-researcher-is-an-agent.md) | `research_external` 跑什么；ADR-027 给它的动态工具怎样才真的到得了模型 | 接受，兑现 ADR-027 §3.3 |
| [ADR-033 融合仍然只发生一次，但那一次归我们做](./0033-fusion-ranks-are-ours.md) | 混合检索为什么跨重建索引不可复现；RRF 的臂内名次该由谁决定 | 接受，取代 ADR-016 中"融合只在 Qdrant 里"一条 |
| [ADR-034 读不出来的时候再问一次](./0034-a-structured-node-asks-once-more.md) | 答案外面裹了一句话时结构化节点该怎么办；ADR-032 §3.3 的严格严在哪一件事上 | 接受，收窄并兑现 ADR-032 §3.3 |
| [ADR-035 答案不是摘要](./0035-an-answer-is-not-a-preview.md) | 一个 run 的答案该有多大；ADR-019 那个 4096 管的是什么 | 接受，收窄 ADR-019 "有界"的适用范围 |
| [ADR-036 提交前的预判决定形态](./0036-triage-decides-the-shape.md) | graph 与 wants_report 这两个提交时决定由谁做出 | 接受，取代 ADR-031 §2.3 |
| [ADR-037 图谱只提名 chunk](./0037-the-graph-nominates-chunks.md) | 跨文档检索怎么做；「抽实体关系合并成一张图」要不要照搬 | 接受（已实现）；四轮消融未达标，`rag.graph.enabled` 保持关闭 |
| [ADR-038 导出闸门守的是一份清单](./0038-the-export-gate-guards-a-list-not-a-boundary.md) | 导出必须经人工审批吗；它算不算 ADR-031 §2.4 说的那种"边界" | 接受，收窄 ADR-031 §2.4；移走 ADR-015 推理的一个前提 |
| [ADR-039 配置里的一个指标名字是一句承诺](./0039-a-metric-name-is-a-promise.md) | 配置声明的评测能力和实现对不上时以哪个为准；`[evaluation]` 该不该承载路线图 | 接受，配置 schema `1.13` → `1.14`；`ragas_enabled` 写 `true` 改为加载失败，`rag_metrics` 只接受注册表里算得出来的名字 |
