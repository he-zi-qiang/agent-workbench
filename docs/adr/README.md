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
