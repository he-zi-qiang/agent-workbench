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
| [配置管理契约](./configuration.md) | schema 1.14 | 定义配置来源、密钥规则、快照语义和跨域校验 |
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
- [已知缺口](./known-gaps.md)：按“拒绝／未接线／未实现／口径不实”四类记录**没有做**
  的部分，每条附仓库位置、不做的理由和“做完”的判据。要判断某项能力是否存在，
  这份和[实施状态](./status.md)一起读；
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

**截至 2026-08-12，基线为 `main@e3281b4`（PR #113），配置 schema `1.14`，
Alembic 单一 head 为 `0025_agent_invocation_count`（共 25 个迁移）。**
门禁按 CI 的四个 job 分别记，不合成一个总数——两个后端 job 的跳过集互相覆盖，
把它们相加会数重：确定性测试 **2050 passed / 719 skipped**（不起外部服务，
跳过的即下一行那些）；真实服务套件 **1012 passed / 2 skipped**（PostgreSQL +
Qdrant，两条跳过分别是 PostgreSQL 专属的非锁定恢复读契约，和需要 `embedding`
extra 与本地权重的真实 reranker 契约）；前端 **171 项** passed（22 个文件）
外加浏览器冒烟 **4 项**；ruff / pyright strict / tsc / eslint 全绿。
数字取自 PR #116 的 CI 运行，不是本机跑的。

此后仍未做的部分，按“拒绝／未接线／未实现／口径不实”分类记在
[已知缺口](./known-gaps.md)，每条附仓库位置与“做完”的判据；
本节以下是**到 PR #87 为止的历史叙述**，保留其原有时点，不再随主线刷新。

截至 2026-08-09（`main@a4dea2b`，PR #87），A–F 汇合增量、OTel/LangChain 工具互操作
（PR #67）、三处围栏修复（PR #68）、React Chat/Work 控制台（PR #69）、LlamaIndex 检索
Adapter 与路由阈值评测（PR #72、#73）以及 Chat 联网搜索与工具额度语义（PR #74）都已经
在 `main` 上。随之落地的五条 ADR——无接地对话形态（ADR-018）、运行步骤透明度（ADR-019）、
外部检索（ADR-020）、Chat 兜底分支联网（ADR-021）和工具额度语义（ADR-022）——
把配置 schema 从 `1.2` 推到 `1.6`。此后 ADR-023 把联网从兜底分支扩到自由回答，
并把两条"无证据作答"路径合并成一个实现；它没有再动 schema。

此后的四个工作包把 schema 推到 `1.10`：WP14-01 的 MCP Adapter（ADR-025，`1.7`→`1.8`）、
WP15 阶段一的任务工作区（ADR-028）与阶段二的一次性沙箱（ADR-029，`1.8`→`1.9`）、
阶段三的只读取用外部世界（ADR-027，`1.9`→`1.10`，PR #83–#86）。再之后三条 ADR 把它
推到 `1.13`：工作节点受成本约束（ADR-030，`1.10`→`1.11`）、triage 决定形态
（ADR-036，`1.11`→`1.12`）、图谱只提名 chunk（ADR-037，`1.12`→`1.13`）。
第四次抬版方向相反：ADR-039（`1.13`→`1.14`）让 `evaluation` 一节不能再声称
代码没有的能力，于是停止加载的是**旧文件**——写着 `ragas_enabled = true`
或 1.13 那份 19 条默认指标名的配置现在校验失败，而不是躺在那里没人读。
四次抬版的完整理由见[配置管理契约](./configuration.md)。PR #87 随后修掉两处
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
（ADR-017 第 3 步）。**注意这条理由已经变过一次**：度量做不出来的根因（并列项造成的
检索不可复现）已由 ADR-033 修掉，但**那次评测还没有在可复现的检索器上重跑**，所以现在
缺的是证据本身，不是通往证据的路。
旧 Qdrant Point 物理清理与历史 token window/compaction 仍未形成完整产品切片；
EventLog upcaster/poison-row 隔离已落地，但生产 upcaster 注册表仍是空的，且界面上
只有 Work 时间线会披露被跳过的位点，Chat 那一半仍然沉默。

实测门禁（2026-08-12）：真实 PostgreSQL + Qdrant
`2758 passed / 11 skipped`；不起任何外部服务 `2065 passed / 704 skipped`；前端
Vitest `171 passed`（CI）、Playwright `4 passed`。`ruff format --check .`（493 files）、
`ruff check src tests`、Pyright `0 errors / 0 warnings / 0 informations`、ESLint
`--max-warnings 0`、`tsc -b` 与 production build 均通过；Alembic 唯一 head 为
`0025_agent_invocation_count`。**真实服务证据由 CI 每个 PR 提供**：
`Migrations, PostgreSQL and Qdrant-backed stores` job 对着真实 PostgreSQL 16 与
Qdrant 跑 `tests/contracts tests/persistence tests/api tests/vector`；同一条命令在本机
对真实服务跑出来是 `1012 passed / 2 skipped`。它不覆盖 `tests/e2e` 与需要模型
Provider 的路径。此处此前写着这个 job "会因并列分数次序不确定而偶发一条失败"——
**那条缺陷已由 ADR-033 修掉，那句话不再成立**。
这一节不再钉具体 commit：基线一往前走，hash 就变成一句要读者自己去核的旧话。
**不同环境的数字只能分别引用，不能相加**。当前开发
身份解析器仍信任请求头，API 和 Compose 只允许在 loopback 的受控本机环境使用。完整证据与已知问题见
[实施状态](./status.md)；A–F 修复前的问题原始快照见
[2026-07-29 仓库审计](./repository-audit-2026-07-29.md)。
