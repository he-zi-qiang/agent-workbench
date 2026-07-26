# Agent Workbench 文档索引

本文档目录区分“已经实现的工程事实”和“尚待实现的目标设计”。架构或计划中
出现某项能力，不代表仓库已经具备该能力；实际完成度以
[实施状态](./status.md)和可复现测试证据为准。

## 核心基线

| 文档 | 版本 | 用途 |
|---|---:|---|
| [架构与技术选型基线](./architecture-baseline.md) | v1.3 | 锁定产品边界、分层、组件职责、可靠性协议和技术选型 |
| [代码实施计划](./implementation-plan.md) | v1.0 | 将目标架构拆成工作包、PR、迁移、测试门禁和证据包 |
| [配置管理契约](./configuration.md) | schema 1.2 | 定义配置来源、密钥规则、快照语义和跨域校验 |

## 决策记录

- [决策记录索引](./adr/)：实施过程中做出的 ADR，编号接续基线第 14 节。

## 项目治理

- [实施状态](./status.md)：记录已经实现、测试或演示的能力；
- [2026-07-25 仓库核验报告](./repository-audit-2026-07-25.md)：记录当前门禁、
  缺陷、能力边界和建议修复顺序；
- [Clean-room 与合规说明](./compliance.md)：说明来源边界和可公开表述；
- [仓库 NOTICE](../NOTICE.md)：声明本项目不包含来源不明的私有实现。

## 阅读顺序

1. 先读架构基线第 1–3 节，理解 Chat、Task 与自研 Runtime 的职责边界；
2. 再读代码实施计划第 3、6、10 节，了解依赖图、工作包和 PR 顺序；
3. 实现任何工作包前核对配置契约与对应验收测试；
4. 合并后只按实施状态和 evidence 证据更新 README 或简历表述。

## 当前事实

截至 2026-07-25，PR-001～PR-015 与 ADR-012 已合并。当前已经实现并测试：

- 框架无关的领域契约、Ports、Fake Adapter 和可复现 CLI；
- 自研 Runtime 的串行 Tool Loop、Policy/Tool Gateway、预算与取消、并行只读
  调度、exclusive 屏障和 Hook Bus；
- DeepSeek OpenAI-compatible 流式协议 Adapter 的离线 contract tests；
- PostgreSQL ConversationStore、文档/版本/ACL、事务 Outbox 与竞争领取；
- Local ArtifactStore，以及 Upload / Artifact / Health FastAPI 路由。

当前仍未实现 Chat RAG、LlamaIndex/Qdrant、LangGraph Task、Task Registry、
lease/fencing/checkpoint/LISTEN、Multi-Agent、生产身份认证、S3、UI 和完整部署。
DeepSeek Adapter 尚未接入 Bootstrap/API/CLI，也没有真实服务 E2E。

安全边界：当前开发身份解析器信任请求头。监听地址已强制为 loopback（默认
`127.0.0.1`，Settings 与装配层双重校验，并有真实 socket 测试），但生产身份认证
仍未实现，因此 API 只能在受控本机环境使用。完整证据与已知问题见
[实施状态](./status.md)。
