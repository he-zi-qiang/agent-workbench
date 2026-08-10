# Agent Workbench 文档索引

本文档目录区分“已经实现的工程事实”和“尚待实现的目标设计”。架构或计划中
出现某项能力，不代表仓库已经具备该能力；实际完成度以
[实施状态](./status.md)和可复现测试证据为准。

第一次读这个项目，从 [十分钟版本](./HIGHLIGHTS.md) 开始：它不引入任何新主张，
每条陈述都能在下列文档或代码里找到出处。

## 核心基线

| 文档 | 版本 | 用途 |
|---|---:|---|
| [架构与技术选型基线](./architecture-baseline.md) | v1.3 | 锁定产品边界、分层、组件职责、可靠性协议和技术选型 |
| [代码实施计划](./implementation-plan.md) | v1.0 | 将目标架构拆成工作包、PR、迁移、测试门禁和证据包 |
| [配置管理契约](./configuration.md) | schema 1.10 | 定义配置来源、密钥规则、快照语义和跨域校验 |
| [本机 Compose 部署](./deployment.md) | local demo | 定义可复现容器拓扑、端口边界与 demo worker 限制 |

## 决策记录

- [决策记录索引](./adr/)：实施过程中做出的 ADR，编号接续基线第 14 节。

## 项目治理

- [实施状态](./status.md)：记录已经实现、测试或演示的能力；
- [2026-07-25 仓库核验报告](./repository-audit-2026-07-25.md)：记录当前门禁、
  缺陷、能力边界和建议修复顺序；
- [2026-07-27 仓库复核报告](./repository-audit-2026-07-27.md)：记录摄取 Worker、
  `knowledge_search` 与 Chat/RAG 安全边界的最新复核；
- [2026-07-29 当前实现完成度与缺陷报告](./repository-audit-2026-07-29.md)：
  按“已完成、未完成、已有缺陷”重新盘点当前 Task 持久化分支，并给出后续 PR 顺序；
- [Clean-room 与合规说明](./compliance.md)：说明来源边界和可公开表述；
- [仓库 NOTICE](../NOTICE.md)：声明本项目不包含来源不明的私有实现。

## 阅读顺序

1. 先读架构基线第 1–3 节，理解 Chat、Task 与自研 Runtime 的职责边界；
2. 再读代码实施计划第 3、6、10 节，了解依赖图、工作包和 PR 顺序；
3. 实现任何工作包前核对配置契约与对应验收测试；
4. 合并后只按实施状态和 evidence 证据更新 README 或简历表述。

前端的产品结构、协议边界、响应式策略和验收门禁见
[前端设计与实现基线](./frontend-design.md)。

## 当前事实

截至 2026-08-09（`main@a4dea2b`，PR #87），A–F 汇合增量、OTel/LangChain 工具互操作
（PR #67）、三处围栏修复（PR #68）、React Chat/Work 控制台（PR #69）、LlamaIndex 检索
Adapter 与路由阈值评测（PR #72、#73）以及 Chat 联网搜索与工具额度语义（PR #74）都已经
在 `main` 上。随之落地的五条 ADR——无接地对话形态（ADR-018）、运行步骤透明度（ADR-019）、
外部检索（ADR-020）、Chat 兜底分支联网（ADR-021）和工具额度语义（ADR-022）——
把配置 schema 从 `1.2` 推到 `1.6`。此后 ADR-023 把联网从兜底分支扩到自由回答，
并把两条"无证据作答"路径合并成一个实现；它没有再动 schema。

此后的四个工作包把 schema 推到 `1.10`：WP14-01 的 MCP Adapter（ADR-025，`1.7`→`1.8`）、
WP15 阶段一的任务工作区（ADR-028）与阶段二的一次性沙箱（ADR-029，`1.8`→`1.9`）、
阶段三的只读取用外部世界（ADR-027，`1.9`→`1.10`，PR #83–#86）。PR #87 随后修掉两处
**同一类**缺陷：能力在组合根装齐了，真正跑的那条分支没接上——`researcher_external` 从不
调用模型（ADR-032），`synthesize` 从不进入工作区会话，因此那三个工作区工具在生产路径上
每一次都失败而 run 仍报告成功。两处都有真实 Task 的事件流验收，详见[实施状态](./status.md)。

已有的 Runtime、Chat/Dense RAG、安全发布与恢复能力继续成立；
A–F 修复的当前状态如下：

| 修复组 | 状态 | 当前事实 |
|---|---|---|
| A | **完成** | Task 工作流显式区分成功/失败，revision 预算与 critic 拒绝终态已订正 |
| B | **完成** | tenant-scoped 提交幂等、输入 fingerprint、owner/tenant 查询隔离已落地 |
| C | **完成** | TaskInput Artifact、Task API/CLI、独立 Worker 入口与单 Worker 纵向切片已落地 |
| D | **完成** | 真实 handlers、内部研究/evidence、结构化 plan/critic 与 Task 授权上下文已接入；真实外部搜索 Provider 已接入（ADR-020，DeepSeek 服务端 `web_search`），`research.enabled` 默认关闭，因为它同时决定 Task 授权信封的宽度 |
| E | **主体完成并通过状态测试** | claim、lease/heartbeat/epoch、stale reclaim、retry/dead-letter、execution guard、fenced checkpointer 与确定性 failpoint 已接入；PR #68 后，图节点在**领取时的** lease 下写入，跨 epoch 的遗留 intent 转人工核对 |
| F | **主体完成** | Qdrant 启动校验、常驻摄取、HITL、OTel、React 控制台、生命周期时间线和本机 Compose 已落地 |

当前明确未完成：Langfuse、CrewAI 对比、动态 Multi-Agent、生产身份认证和生产部署；
RAGAS runner 仍是 Planned（仓库里既没有 runner，`pyproject.toml` 里也没有这个依赖）。
WP15 阶段四（成本与时限、`workspace_edit`、`workspace_grep`）与阶段五（第二张图
`v2_general`，提交时选图并冻结，CLI 与 web 控制台均可选）代码与测试已落地；v2 的
真实模型端到端已有首次成功（带 `workspace:write` scope 的提交，ADR-034 纠正轮次
实战命中一次），证据与仍存的 scope 默认值问题见 status.md。
LlamaIndex retrieval Adapter 已经建成并通过契约测试，但 `rag.llama_index.enabled`
默认为 `false`——缺的不是实现，是一份能把两条检索路径区分开的等价性度量
（ADR-017 第 3 步）。
旧 Qdrant Point 物理清理、历史 token window/compaction 与 EventLog
upcaster/poison-row 隔离仍未形成完整产品切片。

`main@a4dea2b` 的实测门禁：无外部服务 `--ignore=tests/e2e` 为 `1784 passed / 597 skipped`，
`tests/e2e` 为 `3 passed / 11 skipped`，架构与配置 `114 passed`，Ruff format/lint 通过
（421 files），Pyright `0 errors / 0 warnings`；前端 45 个单元测试、2 个桌面/移动浏览器
冒烟测试和 production build 已在 CI 通过。这一组在本机没有外部服务时跑，597/11 项环境跳过
只能报告为跳过。**真实服务证据由 CI 每个 PR 提供**：`Migrations, PostgreSQL and
Qdrant-backed stores` job 对着真实 PostgreSQL 16 与 Qdrant 跑 `tests/contracts
tests/persistence tests/api tests/vector`，共 920 项、2 项环境跳过；它不覆盖
`tests/e2e` 与需要模型 Provider 的路径，且**会因并列分数次序不确定而偶发一条失败**
（详见[实施状态](./status.md)）。最近一次**本机**真实 PostgreSQL + Qdrant 全量记录
仍是前端增量当时的 `1821 passed / 11 skipped`（11 项需要 BGE 权重），那是一棵更早的工作树；
**不同环境、不同工作树的数字只能分别引用，不能相加**。当前开发
身份解析器仍信任请求头，API 和 Compose 只允许在 loopback 的受控本机环境使用。完整证据与已知问题见
[实施状态](./status.md)；A–F 修复前的问题原始快照见
[2026-07-29 仓库审计](./repository-audit-2026-07-29.md)。
