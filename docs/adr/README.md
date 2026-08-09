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
